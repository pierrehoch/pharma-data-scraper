"""Reusable building blocks for adapters (tabular reading, strength/pack parsing, price facts)."""

from __future__ import annotations

import csv
import io
import itertools
import re
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.core.enums import PriceBasis, PriceStatus
from app.core.errors import ParserError
from app.core.parsing import (
    clean_str,
    decode_bytes,
    dedupe_headers,
    find_header_row,
    profile_rows,
    sniff_delimiter,
)
from app.core.types import ParsedRecord, PriceFact, SourceProductData, TableProfile
from app.normalization.forms import normalize_form
from app.normalization.packs import FORM_TO_PACK_UNIT, Pack, parse_pack
from app.normalization.strength import parse_strength
from app.observability import Progress, get_logger

log = get_logger(__name__)


def read_table(
    path: Path,
    *,
    role: str,
    delimiter: str | None = None,
    header_required: Sequence[str] | None = None,
    columns: Sequence[str] | None = None,
    column_aliases: dict[str, str] | None = None,
    key_column: str | None = None,
    declared_encoding: str | None = None,
    stop_after: int | None = None,
    row_filter=None,  # noqa: ANN001 - callable(dict) -> bool, applied while reading
    quoting: int = csv.QUOTE_MINIMAL,
) -> tuple[list[ParsedRecord], TableProfile, dict[str, Any]]:
    """Read a delimited text file with validated encoding/delimiter (streamed row by row).

    * `columns`: names for header-less files (column count is then checked strictly per row)
    * `header_required`: locate the header row (skipping preambles) using these column names
    """
    text, encoding = decode_bytes(path.read_bytes(), declared=declared_encoding)
    delim = sniff_delimiter(text, expected=delimiter)
    reader = csv.reader(io.StringIO(text), delimiter=delim, quoting=quoting)
    head = list(itertools.islice(reader, 200))
    meta: dict[str, Any] = {"encoding": encoding, "delimiter": delim, "preamble": []}
    row_errors = 0
    if columns is not None:
        header = list(columns)
        sample = head
        data_rows = itertools.chain(head, reader)
        start = 0
    else:
        idx, raw_header = find_header_row(head, header_required or [])
        meta["preamble"] = [" ".join(c for c in r if c).strip() for r in head[:idx] if any(c.strip() for c in r)]
        header = dedupe_headers(raw_header)
        if column_aliases:
            header = [_alias(h, column_aliases) for h in header]
        sample = head[idx + 1 :]
        data_rows = itertools.chain(sample, reader)
        start = idx + 1
    # Drop trailing empty columns produced by trailing delimiters (e.g. 'a,b,,')
    while header and header[-1].startswith("col_") and all(len(r) < len(header) or not r[len(header) - 1].strip() for r in sample):
        header.pop()
    records: list[ParsedRecord] = []
    dict_rows: list[dict[str, Any]] = []
    total = 0
    widths: Counter[int] = Counter()
    progress = Progress(f"read {path.name}", unit="rows", check_every=2000)
    for offset, row in enumerate(data_rows):
        progress.update(kept=len(records))
        if not any(c.strip() for c in row):
            continue
        total += 1
        values = [c.strip() for c in row]
        if columns is not None:
            # effective width ignores trailing empty cells (trailing delimiters)
            width = len(values)
            while width > len(header) and not values[width - 1]:
                width -= 1
            widths[width] += 1
        if stop_after is not None and len(records) >= stop_after:
            continue
        if columns is not None and len(values) != len(header):
            # Header-less files: tolerate only trailing empty cells, otherwise the row is malformed.
            if len(values) > len(header) and not any(values[len(header) :]):
                values = values[: len(header)]
            else:
                row_errors += 1
                continue
        if len(values) < len(header):
            values += [""] * (len(header) - len(values))
        data = dict(zip(header, values[: len(header)]))
        if row_filter is not None and not row_filter(data):
            continue
        line_no = start + offset + 1
        key = clean_str(data.get(key_column)) if key_column else None
        records.append(ParsedRecord(row_ref=f"{path.name}:{line_no}", key=key, data=data, role=role))
        dict_rows.append(data)
    meta["row_errors"] = row_errors
    meta["total_rows"] = total
    log.info(
        "Read %s: %s rows (%s kept, %s malformed) · encoding=%s, delimiter=%r",
        path.name, f"{total:,}", f"{len(records):,}", row_errors, encoding, delim,
    )
    profile_columns = list(header)
    if columns is not None and widths:
        # Header-less files: if most rows do not have the declared width, report the observed layout so that
        # schema detection flags it (instead of silently counting every row as malformed).
        dominant = widths.most_common(1)[0][0]
        meta["observed_widths"] = dict(widths)
        if dominant != len(header):
            profile_columns = header[:dominant] + [f"col_{i}" for i in range(len(header), dominant)]
    profile = profile_rows(role, profile_columns, dict_rows, row_count=total)
    return records, profile, meta


def _alias(name: str, aliases: dict[str, str]) -> str:
    for pattern, target in aliases.items():
        if re.fullmatch(pattern, name):
            return target
    return name


def parse_fields(
    *,
    strength_text: str | None = None,
    form_texts: Iterable[str | None] = (),
    pack_text: str | None = None,
    pack: Pack | None = None,
    pack_decimal_sep: str | None = None,
    extra_forms=None,  # noqa: ANN001
    form: str | None = None,
) -> dict[str, Any]:
    """Parse structured strength and pack size; source locale can specify the pack decimal separator."""
    strength = parse_strength(strength_text)
    if pack is None and pack_text:
        form = form or normalize_form(*form_texts, extra=extra_forms)
        pack = parse_pack(pack_text, default_unit=FORM_TO_PACK_UNIT.get(form or ""), decimal_sep=pack_decimal_sep)
    return {
        "strength_value": strength.value if strength else None,
        "strength_unit": strength.unit if strength else None,
        "pack_quantity": pack.quantity if pack else None,
        "pack_unit": pack.unit if pack else None,
    }


def pack_price(
    amount: Decimal | None,
    *,
    currency: str,
    price_type: PriceBasis,
    status: PriceStatus,
    product: SourceProductData,
    vat_included: bool | None,
    statutory_reduction_included: bool | None = None,
    edition_date: date | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    coverage_scope: str = "national",
    variant: str = "default",
    unit_text: str | None = "pack",
    tax_rate: Decimal | None = None,
    dispensing_fee: Decimal | None = None,
    notes: str | None = None,
) -> PriceFact | None:
    """Price for a whole pack. When the source gives no price date, the edition date is used and flagged."""
    if amount is None:
        return None
    date_basis = "explicit" if valid_from else None
    if valid_from is None and edition_date is not None:
        valid_from, date_basis = edition_date, "edition_date"
    return PriceFact(
        amount=amount,
        currency=currency,
        price_type=price_type,
        price_status=status,
        price_unit="pack",
        unit_text=unit_text,
        pack_quantity=product.pack_quantity,
        pack_unit=product.pack_unit,
        vat_included=vat_included,
        tax_rate=tax_rate,
        dispensing_fee=dispensing_fee,
        statutory_reduction_included=statutory_reduction_included,
        valid_from=valid_from,
        valid_to=valid_to,
        date_basis=date_basis,
        coverage_scope=coverage_scope,
        price_variant=variant,
        notes=notes,
    )


def join_notes(*parts: str | None) -> str | None:
    """'label: value' fragments joined with '; ', empty parts dropped."""
    text = "; ".join(p for p in parts if p)
    return text or None


def latest_by_date(items: Iterable[tuple[date, Any]]) -> tuple[date, Any] | None:
    items = list(items)
    if not items:
        return None
    return max(items, key=lambda x: x[0])


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ParserError(message)
