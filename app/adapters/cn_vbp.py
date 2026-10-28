"""Public procurement PDF imports with explicit proposed/final document status."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urlparse

from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError
from app.core.parsing import parse_decimal, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, PriceFact, RawSnapshot, SourceArtifact, SourceProductData
from app.observability import Progress

DEFAULT_DOCUMENTS = [{
    "url": "https://www.nhsa.gov.cn/module/download/downfile.jsp?classid=0&filename=859b442f26cd4a7497dd2d2020ed73cb.pdf",
    "batch": "GY-YD2024-2", "status": "proposed",
}]
COLUMNS = ["batch", "status", "ingredient", "dosage_form", "pack_text", "pricing_unit", "manufacturer", "price"]
UNITS = {"盒": "box", "瓶": "bottle", "瓶/盒": "bottle_or_box", "支": "unit", "袋": "bag", "板": "blister", "包": "sachet", "片": "tablet", "粒": "capsule"}


class CnVbpAdapter(SourceAdapter):
    adapter_id = "cn_vbp"
    parser_type = "pdf"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    limitations = "Bounded public procurement documents, not comprehensive Chinese market prices. Default GY-YD2024-2 document is historical and proposed, not final; effective date unknown."

    def _documents(self) -> list[dict]:
        documents = self.option("documents", DEFAULT_DOCUMENTS)
        if not isinstance(documents, list) or not documents or len(documents) > 50:
            raise DiscoveryError("China VBP documents must contain 1-50 document definitions")
        for item in documents:
            if not isinstance(item, dict) or not item.get("batch") or item.get("status") not in {"proposed", "final"}:
                raise DiscoveryError("China VBP document requires batch and proposed/final status")
            url = urlparse(item.get("url", ""))
            if url.scheme != "https" or url.hostname != "www.nhsa.gov.cn":
                raise DiscoveryError("China VBP requires a public official HTTPS NHSA document")
            for column in ("published_at", "effective_from", "effective_to"):
                if item.get(column):
                    try:
                        date.fromisoformat(item[column])
                    except (ValueError, TypeError) as exc:
                        raise DiscoveryError(f"China VBP invalid {column}") from exc
            if item["status"] == "proposed" and (item.get("effective_from") or item.get("effective_to")):
                raise DiscoveryError("Proposed procurement documents cannot establish effective price validity")
        return documents

    def discover(self) -> SourceArtifact:
        files = [
            ArtifactFile(item["url"], f"procurement_{index:03d}.pdf", role=f"document_{index:03d}",
                         published_at=date.fromisoformat(item["published_at"]) if item.get("published_at") else None)
            for index, item in enumerate(self._documents())
        ]
        return SourceArtifact(self.source_id, files, discovered_from=self.config.source_url,
                              notes=["Configured public PDFs only; proposed amounts are not final awards or current market prices."])

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("prices", COLUMNS)]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        import pdfplumber

        records = {}
        for index, metadata in enumerate(self._documents()):
            file = snapshot.file(f"document_{index:03d}")
            count = 0
            with pdfplumber.open(file.path) as pdf:
                progress = Progress(f"read {metadata['batch']}", total=len(pdf.pages), unit="pages", check_every=1)
                for page_no, page in enumerate(pdf.pages, start=1):
                    progress.update(records=count)
                    text = page.extract_text() or ""
                    if page_no == 1:
                        batch = re.search(r"GY-YD\d{4}-\d+", text)
                        if not batch or batch.group() != metadata["batch"]:
                            raise SchemaChangedError("China VBP document batch does not match configured metadata")
                        proposed = "拟中选" in text
                        if proposed != (metadata["status"] == "proposed"):
                            raise SchemaChangedError("China VBP proposed/final document status contradicts metadata")
                        if "价格" not in text:
                            raise SchemaChangedError("China VBP document has no price columns; supplier-only results are not prices")
                    tables = page.extract_tables()
                    if not tables:
                        raise SchemaChangedError(f"China VBP page {page_no}: no structured price table")
                    page_rows = 0
                    for table in tables:
                        header = [re.sub(r"\s+", "", cell or "") for cell in table[0]]
                        price_header = "拟中选价格（元）" if metadata["status"] == "proposed" else "中选价格（元）"
                        expected_header = ["品种序号", "药品通用名", "剂型", "规格包装", "计价单位",
                                           "拟中选企业" if metadata["status"] == "proposed" else "中选企业", price_header, "备注"]
                        if header != expected_header:
                            raise SchemaChangedError(f"China VBP p{page_no}: unexpected price-table headers {header}")
                        for cells in table:
                            if cells == table[0]:
                                continue
                            if not cells or not (cells[0] or "").strip().isdigit():
                                raise SchemaChangedError(f"China VBP p{page_no}: unrecognized table row {cells}")
                            if len(cells) != 8:
                                raise SchemaChangedError(f"China VBP p{page_no}: expected 8 table columns")
                            values = [re.sub(r"\s+", "", cell or "") for cell in cells]
                            ordinal, ingredient, form, pack, unit, company, price, remarks = values
                            amount = parse_decimal(price, decimal_sep=".")
                            if not all([ingredient, form, pack, unit, company]) or amount is None or amount <= 0:
                                raise ParserError(f"China VBP p{page_no}: invalid product or price row {values}")
                            if unit not in UNITS:
                                raise ParserError(f"China VBP p{page_no}: unknown pricing unit {unit!r}")
                            row = {"batch": metadata["batch"], "status": metadata["status"], "ingredient": ingredient,
                                   "dosage_form": form, "pack_text": pack, "pricing_unit": unit,
                                   "manufacturer": company, "price": str(amount), "remarks": remarks,
                                   "metadata": metadata}
                            key = hashlib.sha256("|".join([metadata["batch"], metadata["status"], ingredient, form, pack, unit, company]).encode()).hexdigest()[:32]
                            if key in records and records[key].data != row:
                                raise ParserError(f"China VBP conflicting price rows for {key}")
                            records[key] = ParsedRecord(f"{file.filename}:p{page_no}:item{ordinal}:{key}", key, row, role="prices")
                            count += 1
                            page_rows += 1
                    if not page_rows:
                        raise SchemaChangedError(f"China VBP p{page_no}: no price rows parsed")
            if not count:
                raise ParserError(f"China VBP no price records in {file.filename}")
        rows = list(records.values())
        return ParsedDataset(self.source_id, snapshot, rows, [profile_rows("prices", COLUMNS, [r.data for r in rows], row_count=len(rows))], total_rows=len(rows))

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord:
        row, metadata = record.data, record.data["metadata"]
        amount = parse_decimal(row["price"], decimal_sep=".")
        if amount is None or amount <= 0:
            raise ParserError(f"China VBP invalid normalized price for {record.key}")
        note = f"{row['status'].upper()} procurement disclosure, batch {row['batch']}; not a retail price or evidence of a current final award. Effective dates are unknown unless explicitly configured from an official final notice. Pricing unit: {row['pricing_unit']}; {metadata['url']}"
        product = SourceProductData(
            product_code=record.key or "", code_type="VBP_DISCLOSURE", product_level=ProductLevel.PRESENTATION,
            name=f"{row['ingredient']} {row['pack_text']}", ingredient=row["ingredient"],
            dosage_form=row["dosage_form"], pack_text=row["pack_text"], manufacturer=row["manufacturer"],
            reimbursement_category=f"VBP {row['batch']} ({row['status']})", detail_url=metadata["url"], notes=note,
        )
        fact = PriceFact(
            amount=amount, currency="CNY", price_type=PriceBasis.PUBLIC_PROCUREMENT,
            price_status=PriceStatus.REFERENCE if row["status"] == "proposed" else PriceStatus.OBSERVED,
            price_variant=f"{row['batch']}:{row['status']}", price_unit=UNITS[row["pricing_unit"]],
            unit_text=row["pricing_unit"], coverage_scope=self.config.coverage_scope,
            valid_from=date.fromisoformat(metadata["effective_from"]) if metadata.get("effective_from") else None,
            valid_to=date.fromisoformat(metadata["effective_to"]) if metadata.get("effective_to") else None,
            published_at=date.fromisoformat(metadata["published_at"]) if metadata.get("published_at") else None,
            date_basis="explicit" if metadata.get("effective_from") else "unknown",
            notes=note,
        )
        return NormalizedRecord(record.row_ref, product, [fact])
