"""Japan – MHLW NHI drug price list (薬価基準収載品目リスト): oral, injectable, topical, dental Excel files.

Discovery: index page → yearly list page (URL changes every April) → latest Excel per category
(file names embed the listing date: tpYYYYMMDD-01_0N.xlsx). Prices are per official unit (1錠, 1g, 1mL, 1管…).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal

from app.adapters._common import join_notes, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError
from app.core.parsing import clean_str, dedupe_headers, find_header_row, html_links, iter_xlsx_rows, parse_decimal, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import (
    ArtifactFile,
    NormalizedRecord,
    ParsedDataset,
    ParsedRecord,
    PriceFact,
    RawSnapshot,
    SourceArtifact,
    SourceProductData,
)
from app.observability import get_logger
from app.normalization.text import extract_brand

log = get_logger(__name__)

INDEX = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000078916.html"
LIST_RE = re.compile(r"/topics/(\d{4})/\d{2}/tp(\d{8})-01\.html$")
XLSX_RE = re.compile(r"/tp(\d{8})-01_0([1-4])\.xlsx$")
CATEGORIES = {"1": "oral", "2": "injection", "3": "topical", "4": "dental"}
HEADER = ["区分", "薬価基準収載医薬品コード", "成分名", "規格", "品名", "メーカー名", "薬価"]
COL = {
    "kubun": "区分",
    "code": "薬価基準収載医薬品コード",
    "ingredient": "成分名",
    "spec": "規格",
    "name": "品名",
    "maker": "メーカー名",
    "price": "薬価",
    "generic_flag": "診療報酬において加算等の算定対象となる後発医薬品",
    "brand_flag": "先発医薬品",
    "brand_with_generic": "同一剤形_規格の後発医薬品がある先発医薬品",
    "expiry": "経過措置による使用期限",
    "remarks": "備考",
}
PRODUCT_TYPE_FLAGS = [("generic_flag", "後発品"), ("brand_flag", "先発品"), ("brand_with_generic", "後発品のある先発品")]
ERA_RE = re.compile(r"(?P<era>[RH令平])(?:和|成)?\s*(?P<y>\d+)[.年/](?P<m>\d+)[.月/](?P<d>\d+)")
ERA_OFFSET = {"R": 2018, "令": 2018, "H": 1988, "平": 1988}


def _era_date(text: str | None) -> date | None:
    """'R9.3.31まで' (Reiwa 9) → 2027-03-31."""
    m = ERA_RE.search(unicodedata.normalize("NFKC", text or ""))
    if not m:
        return None
    try:
        return date(ERA_OFFSET[m.group("era")] + int(m.group("y")), int(m.group("m")), int(m.group("d")))
    except ValueError:
        return None
UNIT_RE = re.compile(r"(?P<qty>\d+(?:\.\d+)?)(?P<unit>錠|カプセル|g|mL|ml|L|管|瓶|枚|包|キット|個|本|袋|筒|シリンジ|バイアル|cm|cm2|滴|組|噴霧|ブリスター)$")
JP_UNITS = {
    "錠": "tablet", "カプセル": "capsule", "g": "g", "mL": "ml", "ml": "ml", "L": "l", "管": "ampoule", "瓶": "vial",
    "バイアル": "vial", "枚": "sheet", "包": "sachet", "キット": "kit", "個": "unit", "本": "unit", "袋": "bag",
    "筒": "syringe", "シリンジ": "syringe", "cm": "cm", "cm2": "cm2", "滴": "drop", "組": "set", "噴霧": "spray",
    "ブリスター": "blister",
}


class JpNhiAdapter(SourceAdapter):
    adapter_id = "jp_nhi"
    parser_type = "xlsx"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        index = self.ctx.http.get_text(INDEX)
        pages = []
        for url, text in html_links(index.text, INDEX):
            m = LIST_RE.search(url)
            if m:
                pages.append((m.group(2), url, text))
        if not pages:
            raise DiscoveryError("MHLW index: no link to a 薬価基準収載品目リスト page (tpYYYYMMDD-01.html) found", details={"index": INDEX})
        _, list_url, label = max(pages)
        listing = self.ctx.http.get_text(list_url)
        latest: dict[str, tuple[str, str]] = {}
        for url, _ in html_links(listing.text, list_url):
            m = XLSX_RE.search(url)
            if m:
                date_s, cat = m.groups()
                if cat not in latest or date_s > latest[cat][0]:
                    latest[cat] = (date_s, url)
        missing = [CATEGORIES[c] for c in CATEGORIES if c not in latest]
        if missing:
            raise DiscoveryError(f"MHLW list page {list_url}: Excel file missing for categories {missing}")
        files = [
            ArtifactFile(
                url=url,
                filename=url.rsplit("/", 1)[-1],
                role=CATEGORIES[cat],
                published_at=datetime.strptime(date_s, "%Y%m%d").date(),
            )
            for cat, (date_s, url) in sorted(latest.items())
        ]
        published = max(f.published_at for f in files)
        return SourceArtifact(self.source_id, files, published_at=published, edition=label or None, discovered_from=list_url)

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [
            ExpectedSchema(role, [COL[k] for k in ("kubun", "code", "ingredient", "spec", "name", "maker", "price")], typed_columns=[COL["code"], COL["price"]])
            for role in CATEGORIES.values()
        ]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        records, profiles, total = [], [], 0
        stop = self.hints.early_stop_at
        truncated = False
        for role in CATEGORIES.values():
            f = snapshot.file(role)
            rows = iter_xlsx_rows(f.path)
            head = []
            for row in rows:
                head.append(row)
                if len(head) >= 10:
                    break
            idx, raw_header = find_header_row(head, HEADER)
            header = dedupe_headers(raw_header)
            dict_rows = []
            count = 0
            for i, row in enumerate([*head[idx + 1 :], *rows]):
                if not any(v not in (None, "") for v in row):
                    continue
                count += 1
                if stop is not None and len(dict_rows) >= stop:
                    truncated = True
                    break
                data = dict(zip(header, row))
                dict_rows.append(data)
                records.append(
                    ParsedRecord(row_ref=f"{f.filename}:{idx + 2 + i}", key=clean_str(data.get(COL["code"])), data=data, role=role)
                )
            total += count
            log.info("Read %s (%s): %s rows", f.filename, role, f"{count:,}")
            profiles.append(profile_rows(role, header, dict_rows, row_count=count))
        return ParsedDataset(self.source_id, snapshot, records, profiles, truncated=truncated, total_rows=total)

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        spec_raw = clean_str(d.get(COL["spec"])) or ""
        spec = unicodedata.normalize("NFKC", spec_raw)
        name = clean_str(d.get(COL["name"])) or ""
        kubun = clean_str(d.get(COL["kubun"])) or ""
        unit_m = UNIT_RE.search(spec)
        expiry = clean_str(d.get(COL["expiry"]))
        expiry_date = _era_date(expiry)
        product = SourceProductData(
            product_code=clean_str(d.get(COL["code"])) or "",
            code_type="YJ_CODE",
            product_level=ProductLevel.PRODUCT,
            name=name,
            brand_name=extract_brand(name),
            ingredient=clean_str(d.get(COL["ingredient"])),
            strength_text=spec_raw or None,
            route=kubun or None,
            manufacturer=clean_str(d.get(COL["maker"])),
            therapeutic_class=record.role,
            product_type=join_notes(*(label for col, label in PRODUCT_TYPE_FLAGS if clean_str(d.get(COL[col])))),
            reimbursement_category="NHI price list (薬価基準)",
            marketing_end_date=expiry_date,
            notes=join_notes(
                "局方" if clean_str(d.get("col_4")) else None,
                f"経過措置 {expiry}" if expiry and not expiry_date else None,
                clean_str(d.get(COL["remarks"])),
            ),
            **parse_fields(strength_text=spec),
        )
        amount = parse_decimal(d.get(COL["price"]), decimal_sep=".")
        prices = []
        if amount is not None:
            unit_label = unit_m.group(0) if unit_m else spec
            unit = JP_UNITS.get(unit_m.group("unit"), "unit") if unit_m else "unit"
            qty = Decimal(unit_m.group("qty")) if unit_m else None
            if qty is not None and qty != 1:
                unit = f"{unit_m.group('qty')} {unit}"
            edition = self._edition_for(record, dataset)
            prices.append(
                PriceFact(
                    amount=amount,
                    currency="JPY",
                    price_type=PriceBasis.REIMBURSEMENT,
                    price_status=PriceStatus.REGULATED,
                    price_unit=unit,
                    unit_text=unit_label,
                    vat_included=True,
                    valid_from=edition,
                    date_basis="edition_date",
                    coverage_scope=self.config.coverage_scope,
                    notes="NHI price (薬価) per official unit, consumption tax included",
                )
            )
        return NormalizedRecord(record.row_ref, product, prices)

    @staticmethod
    def _edition_for(record: ParsedRecord, dataset: ParsedDataset):  # noqa: ANN205
        m = re.match(r"tp(\d{8})-01_0\d", record.row_ref)
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
        return dataset.snapshot.published_at
