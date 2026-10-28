"""Spanish CIPM disclosures: proposed industrial prices, not effective retail prices."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from app.adapters._common import parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, ErrorType, PriceBasis, PriceStatus, Severity
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError
from app.core.parsing import html_links, month_number, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, PriceFact, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.text import fold
from app.observability import Progress, get_logger

PAGE = "https://www.sanidad.gob.es/areas/farmacia/precios/comisionInteministerial/acuerdosNotasInformativas/home.htm"
PREFIX = "/areas/farmacia/precios/comisionInteministerial/acuerdosNotasInformativas/"
COLUMNS = ["cn", "name", "manufacturer", "pack", "amount", "meeting_date", "session", "decision"]
DATE_RE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+(?:de\s+)?(\d{4})", re.I)
AMOUNT_RE = re.compile(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,4})?")
log = get_logger(__name__)
_NON_INGREDIENT_LABELS = {"hemofiltrados", "combinaciones"}


def _ingredient(value: str | None) -> tuple[str | None, str | None]:
    """Remove the source's ATC-like code; retain broad classifications as provenance, not ingredients."""
    if not value:
        return None, None
    match = re.fullmatch(r"\s*([A-Z]\d{2}[A-Z0-9]{2,4})\s*[-–]\s*(.+?)\s*", value, re.IGNORECASE)
    if not match:
        return value, None
    code, label = match.groups()
    if fold(label) in _NON_INGREDIENT_LABELS:
        return None, f"CIPM active-ingredient classification: {code} – {label}"
    return label, None


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _meeting(text: str) -> tuple[str, date]:
    session = re.search(r"sesi[oó]n\s+(\d+)\s+de\s+" + DATE_RE.pattern, text, re.I)
    if not session:
        raise SchemaChangedError("CIPM: no explicit session number and meeting date")
    number, day, month, year = session.groups()
    month_value = month_number(month)
    if not month_value:
        raise SchemaChangedError(f"CIPM: unrecognized meeting month {month!r}")
    return number, date(int(year), month_value, int(day))


def _price(value: str) -> Decimal:
    value = _clean(value).replace("€", "").strip()
    if re.fullmatch(r"\d+\.\d{1,2}", value):
        amount = Decimal(value)
    elif AMOUNT_RE.fullmatch(value):
        amount = Decimal(value.replace(".", "").replace(",", "."))
    else:
        raise ParserError(f"CIPM: invalid/ambiguous published price {value!r}")
    if amount <= 0:
        raise ParserError(f"CIPM: non-positive published price {value!r}")
    return amount


def _headers(rows: list[list[str | None]]) -> dict[str, int] | None:
    for row_no, row in enumerate(rows[:5]):
        cells = [_clean(cell).upper() for cell in row]
        names = {"name": "MEDICAMENTO", "manufacturer": "LABORATORIO", "pack": "FORMATO", "cn": "CN"}
        if not all(value in cells for value in names.values()):
            continue
        header_rows = [
            header for header in rows[row_no:row_no + 5]
            if not any(re.fullmatch(r"\d{6}", _clean(cell)) for cell in header)
        ]
        combined = [
            _clean(" ".join(dict.fromkeys(_clean(header[index]) for header in header_rows if len(header) > index))).upper()
            for index in range(len(cells))
        ]
        labels = list(dict.fromkeys(_clean(cell.replace("€", "")) for cell in combined if "PRECIO" in cell))
        prices = [index for index, cell in enumerate(combined) if "PRECIO" in cell]
        if not prices:
            return None
        if len(labels) > 1:
            choices = [index for index, label in enumerate(labels) if re.search(r"NUEVO|PROPUESTO", label)]
            if len(choices) != 1 or any(re.search(r"PARTIR|\d{2}/\d{2}/", label) for label in labels):
                raise SchemaChangedError("CIPM: ambiguous price columns (no unique new-price column)")
            price_choice = choices[0]
        else:
            price_choice = 0
        return {**{key: cells.index(value) for key, value in names.items()},
                "amount": prices[0], "price_start": max(cells.index(value) for value in names.values()) + 1,
                "price_end": next((index for index, cell in enumerate(combined) if "CRITERIOS" in cell), len(cells)),
                "price_count": len(labels), "price_choice": price_choice,
                **({"ingredient": cells.index("PRINCIPIO ACTIVO")} if "PRINCIPIO ACTIVO" in cells else {})}
    return None


def _rows_with_spans(table) -> list[list[str | None]]:
    rows = table.extract()
    for row_no, row in enumerate(rows):
        boxes = table.rows[row_no].cells
        top = min(box[1] for box in boxes if box is not None)
        for column, value in enumerate(row):
            if value is not None:
                continue
            for previous in range(row_no - 1, -1, -1):
                box = table.rows[previous].cells[column]
                if box is not None:
                    if box[3] > top + 1:
                        row[column] = rows[previous][column]
                    break
    return rows


def _decision(text: str, current: str) -> str:
    for line in text.splitlines():
        if re.search(r"\.{3,}", line):
            continue
        if re.search(r"^\s*\d+[).\-]?\s*ACUERDOS.*(?:DENEGATOR|DESFAVORABLE)", line, re.I):
            current = "denied"
        elif re.search(r"^\s*1[).\-]?\s*ACUERDOS.*(?:PRECIO|FAVORABLE)", line, re.I):
            current = "favourable_proposal"
    return current


def _between(page, top: float, bottom: float) -> str:
    return (page.crop((0, top, page.width, bottom)).extract_text() or "") if bottom > top else ""


class EsCipmAdapter(SourceAdapter):
    adapter_id = "es_cipm"
    parser_type = "pdf"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    limitations = (
        "Full linked CIPM archive; explicit table prices are proposed maximum industrial disclosures, not "
        "final financing decisions, retail prices or proof of effective validity. Unsupported pages and "
        "unpriced notes are reported. No confidential discounts inferred."
    )

    def discover(self) -> SourceArtifact:
        response = self.ctx.http.get_text(PAGE)
        files = {}
        for url, _ in html_links(response.text, PAGE):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "www.sanidad.gob.es" or not parsed.path.startswith(PREFIX) or not parsed.path.lower().endswith(".pdf"):
                continue
            filename = unquote(PurePosixPath(parsed.path).name)
            files[url] = ArtifactFile(url, filename, role="document")
        if not files:
            raise DiscoveryError("CIPM index: no official linked PDF documents found", details={"page": PAGE})
        return SourceArtifact(
            self.source_id, list(files.values()), discovered_from=PAGE,
            edition=f"{len(files)} linked CIPM disclosures",
            notes=["All distinct PDFs linked from the official index, including unpriced informational notes. Prices remain proposed/reference, with no inferred effective date."],
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("prices", COLUMNS)]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        import pdfplumber
        from pdfplumber.utils.exceptions import PdfminerException

        records, issues, excluded = {}, [], []
        excluded_rows = []

        def reject(message: str, filename: str, url: str, row_ref: str) -> None:
            log.warning("CIPM excluded %s: %s", row_ref, message)
            excluded_rows.append({"filename": filename, "url": url, "row_ref": row_ref, "reason": message})
            issues.append(self.issue(ErrorType.PARSER_ERROR, message, stage="parse", row_ref=row_ref, url=url))

        parsed_documents = 0
        progress = Progress("CIPM PDFs", total=len(snapshot.files), unit="documents", check_every=1)
        for file in snapshot.files:
            document_records = {}
            try:
                with pdfplumber.open(file.path) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages[:2])
                    session, meeting_date = _meeting(text)
                    decision = "unknown"
                    document_candidates = 0
                    for page_no, page in enumerate(pdf.pages, start=1):
                        page_text = page.extract_text() or ""
                        if not re.search(r"PRECIO|Precio|precio", page_text):
                            decision = _decision(page_text, decision)
                            continue
                        tables = page.find_tables()
                        previous_bottom = 0
                        for table_no, table in enumerate(tables):
                            rows = _rows_with_spans(table)
                            try:
                                headers = _headers(rows)
                            except SchemaChangedError as exc:
                                reject(str(exc), file.filename, file.source_url, f"{file.filename}:p{page_no}:table{table_no}")
                                continue
                            if headers is None:
                                continue
                            before = _between(page, previous_bottom, table.bbox[1])
                            decision = _decision(before, decision)
                            previous_bottom = table.bbox[3]
                            next_top = next((
                                t.bbox[1] for t in tables[table_no + 1:]
                                if t.bbox[1] >= table.bbox[3]
                                and all(label in str(t.extract()[:5]).upper() for label in ("MEDICAMENTO", "CN"))
                            ), page.height)
                            after = _between(page, table.bbox[3], next_top)
                            ingredient_match = re.search(r"Principio[s]? activo[s]?\s*:\s*([^\n]+)", after, re.I)
                            ingredient = _clean(ingredient_match[1]) if ingredient_match else None
                            for row_no, cells in enumerate(rows):
                                row_ref = f"{file.filename}:p{page_no}:table{table_no}:row{row_no}"
                                if len(cells) <= max(headers[key] for key in ("cn", "name", "manufacturer", "pack", "amount")):
                                    reject("Incomplete product row", file.filename, file.source_url, row_ref)
                                    continue
                                cn = _clean(cells[headers["cn"]])
                                if not cn or cn.upper() == "CN":
                                    continue
                                cn = cn.replace(" ", "")
                                if not re.fullmatch(r"\d{6}", cn):
                                    reject(f"Ambiguous national code {cn!r}", file.filename, file.source_url, row_ref)
                                    continue
                                document_candidates += 1
                                if decision == "denied":
                                    continue
                                row = {key: _clean(cells[headers[key]]) for key in ("name", "manufacturer", "pack")}
                                row["cn"] = cn
                                values = [_clean(cell).replace("€", "").strip()
                                          for cell in cells[headers["price_start"]:headers["price_end"]]
                                          if re.fullmatch(r"[\d.,]+\s*€?", _clean(cell))]
                                if len(values) != headers["price_count"]:
                                    reject(f"CN {cn}: no unique numeric price for each labelled column; unpublished or unsupported price/unit",
                                           file.filename, file.source_url, row_ref)
                                    continue
                                if not all(row[key] for key in ("name", "manufacturer", "pack")):
                                    reject(f"CN {cn}: missing product/pack fields", file.filename, file.source_url, row_ref)
                                    continue
                                try:
                                    row["amount"] = str(_price(values[headers["price_choice"]]))
                                except ParserError as exc:
                                    reject(str(exc), file.filename, file.source_url, row_ref)
                                    continue
                                row.update(session=session, meeting_date=meeting_date, decision=decision,
                                           ingredient=_clean(cells[headers["ingredient"]]) if "ingredient" in headers else ingredient,
                                           document_url=file.source_url, page=page_no)
                                # Each meeting/table remains a separate disclosure, never a fabricated current price series.
                                identity = f"{urlparse(file.source_url).path}|{page_no}|{table_no}|{cn}"
                                key = hashlib.sha256(identity.encode()).hexdigest()[:32]
                                if key in document_records and document_records[key].data != row:
                                    raise SchemaChangedError(f"CIPM conflicting rows for CN {cn}")
                                document_records[key] = ParsedRecord(
                                    f"{file.filename}:p{page_no}:table{table_no}:row{row_no}", key, row, role="prices",
                                )
                        decision = _decision(_between(page, previous_bottom, page.height), decision)
                    if not document_records:
                        raise ParserError(f"No supported positive-price disclosures ({document_candidates} candidate rows); informational/non-price or denied-only document")
            except (SchemaChangedError, ParserError, PdfminerException, ValueError, OSError) as exc:
                message = f"CIPM excluded {file.filename}: {exc}"
                log.warning("%s", message)
                excluded.append({"filename": file.filename, "url": file.source_url, "reason": str(exc)})
                issues.append(self.issue(ErrorType.PARSER_ERROR, message, stage="parse", row_ref=file.filename, url=file.source_url))
            else:
                records.update(document_records)
                parsed_documents += 1
            progress.update(parsed=parsed_documents, excluded=len(excluded), records=len(records))
        progress.finish(parsed=parsed_documents, excluded=len(excluded), records=len(records))
        if not records:
            raise ParserError("CIPM archive contains no supported explicit positive-price disclosures")
        issues.append(self.issue(
            ErrorType.UNSUPPORTED_SOURCE,
            f"CIPM document coverage: {parsed_documents}/{len(snapshot.files)} PDFs yielded price disclosures; {len(excluded)} excluded. Prices are proposed industrial amounts, not final/effective financing decisions.",
            severity=Severity.INFO, stage="parse", excluded_documents=excluded, excluded_rows=excluded_rows,
        ))
        rows = list(records.values())
        return ParsedDataset(
            self.source_id, snapshot, rows,
            [profile_rows("prices", COLUMNS, [record.data for record in rows], row_count=len(rows))],
            total_rows=len(rows), issues=issues,
            context={"documents_parsed": parsed_documents, "documents_excluded": excluded, "rows_excluded": excluded_rows},
        )

    def record_matches_product_ids(self, record: ParsedRecord, product_ids: set[str]) -> bool:
        return record.key in product_ids or record.data["cn"] in product_ids

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord:
        row = record.data
        ingredient, ingredient_note = _ingredient(row["ingredient"])
        note = (
            f"PROPOSED maximum industrial price disclosure; CIPM session {row['session']}, meeting {row['meeting_date']}; "
            f"decision context {row['decision']}. Informational agreement is not definitive or effective until the final "
            "resolution and Nomenclator inclusion. No final reimbursement entitlement, effective date, retail VAT or "
            f"confidential net price inferred. Source p{row['page']}: {row['document_url']}"
        )
        if ingredient_note:
            note += f"; {ingredient_note}"
        product = SourceProductData(
            product_code=record.key or "", code_type="CIPM_DISCLOSURE", name=row["name"],
            ingredient=ingredient, manufacturer=row["manufacturer"], pack_text=row["pack"],
            group_code=row["cn"], group_name="CODIGO_NACIONAL",
            reimbursement_category=f"CIPM {row['session']}: {row['decision']}",
            detail_url=row["document_url"], notes=note + f"; Spanish national code {row['cn']}.",
            **parse_fields(strength_text=row["name"], pack_text=row["pack"], pack_decimal_sep=","),
        )
        price = PriceFact(
            amount=Decimal(row["amount"]),
            currency="EUR", price_type=PriceBasis.EX_FACTORY, price_status=PriceStatus.REFERENCE,
            price_variant=f"cipm_{row['session']}:proposed_industrial", price_unit="pack", unit_text=row["pack"],
            pack_quantity=product.pack_quantity, pack_unit=product.pack_unit, coverage_scope=self.config.coverage_scope,
            published_at=row["meeting_date"], date_basis="meeting_date", notes=note,
        )
        return NormalizedRecord(record.row_ref, product, [price])
