"""Reviewed provincial disclosures, never a complete/current Chinese market tariff."""

from __future__ import annotations

import json
import re
import unicodedata
from contextlib import closing
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

import openpyxl

from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, ErrorType, PriceBasis, PriceStatus, ProductLevel, Severity
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError, SourceUnsupportedError
from app.core.parsing import clean_str, parse_decimal, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, Issue, NormalizedRecord, ParsedDataset, ParsedRecord, PriceFact, RawSnapshot, SourceArtifact, SourceProductData

GD_COLUMNS = [
    "序号", "批次", "药品ID", "国家医保代码", "产品名称", "剂型名称", "具体规格", "包装规格",
    "包装材质", "生产企业/厂商", "申报企业名称", "批准文号/注册证号", "包装挂网价格（元）",
]
HB_ADMISSION_COLUMNS = [
    "序号", "流水号", "国家医保编码", "药品名称", "商品名称", "剂型", "规格", "最小包装数量",
    "最小包装单位", "最小制剂单位", "包装材质", "批准文号", "上市许可持有人", "生产企业名称",
    "统一社会信用代码", "申报企业名称", "申报类型", "质量层次", "价格形成方式", "备注", "挂网价",
]
HB_ADJUSTMENT_COLUMNS = [
    "序号", "流水号", "医保编码", "申报企业名称", "通用名", "剂型", "规格", "最小包装数量",
    "批准文号", "挂网类型", "上报类型", "降价前价格", "降价后价格", "备注",
]
HB_AWARD_COLUMNS = [
    "序号", "流水号", "药品编码", "申报企业名称", "通用名", "剂型", "规格", "包装数量",
    "批准文号", "挂网类型", "上报类型", "降价前价格", "降价后价格",
]
SCHEMAS = {
    "gd_listing": GD_COLUMNS, "hb_admission": HB_ADMISSION_COLUMNS,
    "hb_adjustment": HB_ADJUSTMENT_COLUMNS, "hb_award_adjustment": HB_AWARD_COLUMNS,
}
COLUMNS = ["code", "name", "form", "strength", "pack", "manufacturer", "authorisation", "amount"]
UNITS = {"片": "tablet", "粒": "unit", "丸": "pill", "袋": "bag", "支": "unit", "瓶": "bottle", "贴": "patch"}


def chinese_pack(text: str | None) -> tuple[Decimal | None, str | None]:
    # Only explicit simple content/container expressions establish pack contents.
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(片|粒|丸|袋|支|瓶|贴)\s*/\s*(盒|瓶|袋|支)\s*", text or "")
    if not match:
        return None, None
    quantity = Decimal(match[1])
    return (quantity, UNITS[match[2]]) if quantity > 0 else (None, None)


def chinese_form(text: str | None) -> str | None:
    for token, form in [
        ("缓释片", "prolonged-release tablet"), ("肠溶片", "gastro-resistant tablet"),
        ("注射", "injection"), ("片剂", "tablet"), ("胶囊", "capsule"), ("颗粒", "granules"),
        ("乳膏", "cream"), ("软膏", "ointment"), ("凝胶", "gel"), ("口服溶液", "oral solution"),
    ]:
        if token in (text or ""):
            return form
    return None


def positive_amount(value: object, *, row_ref: str) -> Decimal:
    if isinstance(value, bool):
        raise ParserError(f"{row_ref}: boolean is not a price")
    amount = parse_decimal(value, decimal_sep=".")
    if amount is None or not amount.is_finite() or amount <= 0:
        raise ParserError(f"{row_ref}: expected a positive numeric price, got {value!r}")
    return amount


class CnProvincialAdapter(SourceAdapter):
    parser_type = "xlsx (reviewed public documents)"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    host = ""
    province = ""
    default_documents: list[dict[str, object]] = []

    def check_configuration(self) -> list[Issue]:
        if self.option("internal_research_acknowledged") is not True:
            return [self.issue(
                ErrorType.MISSING_CONFIGURATION,
                "Provincial China sources require internal_research_acknowledged: true. "
                "Review reuse restrictions first; this is not commercial authorization.",
                severity=Severity.ERROR, stage="configuration",
            )]
        return []

    def _public_url(self, value: object) -> str:
        if not isinstance(value, str):
            raise DiscoveryError("Document and evidence URLs must be public official HTTPS URLs")
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc != self.host or parsed.username or parsed.fragment:
            raise DiscoveryError(f"Expected public HTTPS URL on {self.host}: {value!r}")
        return value

    def _documents(self, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list) or not value or len(value) > 100:
            raise DiscoveryError("documents must contain 1-100 reviewed public document definitions")
        documents: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise DiscoveryError("Each document must be a mapping")
            doc = dict(item)
            url = self._public_url(doc.get("url"))
            self._public_url(doc.get("notice_url"))
            if not urlparse(url).path.endswith(".xlsx") or url in seen:
                raise DiscoveryError("Documents must be distinct public XLSX attachments; supplier-only PDFs are not prices")
            seen.add(url)
            kind = doc.get("kind")
            if not isinstance(kind, str) or kind not in SCHEMAS or not kind.startswith("gd_" if self.province == "CN-GD" else "hb_"):
                raise DiscoveryError(f"Unsupported provincial document kind: {kind!r}")
            if doc.get("status") not in ("final", "proposed"):
                raise DiscoveryError("Document requires explicit final/proposed status")
            for field in ("published_at", "effective_from"):
                val = doc.get(field)
                if field == "published_at" and not val:
                    raise DiscoveryError("Document requires a reviewed publication date, not a date inferred from its URL")
                if val is not None:
                    if not isinstance(val, str):
                        raise DiscoveryError(f"Invalid document {field}")
                    try:
                        date.fromisoformat(val)
                    except ValueError as exc:
                        raise DiscoveryError(f"Invalid document {field}: {val}") from exc
            if doc["status"] == "proposed" and doc.get("effective_from"):
                raise DiscoveryError("A proposal cannot establish effective price validity")
            evidence = doc.get("cny_pack_evidence_url")
            if evidence is not None:
                self._public_url(evidence)
            documents.append(doc)
        return documents

    def discover(self) -> SourceArtifact:
        if self.check_configuration():
            raise SourceUnsupportedError(self.check_configuration()[0].message)
        if self.config.coverage_scope != f"province:{self.province}":
            raise DiscoveryError(f"Source coverage must remain province:{self.province}, never national")
        docs = self._documents(self.option("documents", self.default_documents))
        # Snapshot the reviewed interpretation too: resume must not reinterpret old bytes with new config.
        manifest = self.ctx.store.staging_dir(self.source_id) / "reviewed_documents.json"
        manifest.write_text(json.dumps(docs, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        files = [
            ArtifactFile(str(d["url"]), f"document_{i:03d}.xlsx", role=f"document_{i:03d}",
                         published_at=date.fromisoformat(str(d["published_at"])))
            for i, d in enumerate(docs)
        ]
        files.append(ArtifactFile(manifest.resolve().as_uri(), manifest.name, role="reviewed_metadata"))
        return SourceArtifact(
            self.source_id, files, discovered_from=self.config.source_url,
            notes=["Bounded reviewed attachments, not exhaustive discovery or a current national tariff.",
                   "Internal research only; reuse rights are not granted by public accessibility."],
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("presentations", COLUMNS, typed_columns=["code"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        metadata = snapshot.file("reviewed_metadata")
        try:
            documents = self._documents(json.loads(metadata.path.read_text(encoding="utf-8")))
        except (ValueError, UnicodeError) as exc:
            raise ParserError("Invalid snapshotted provincial document metadata") from exc
        records: list[ParsedRecord] = []
        issues: list[Issue] = []
        conflicts: dict[tuple[object, ...], dict[str, object]] = {}
        for index, doc in enumerate(documents):
            file = snapshot.file(f"document_{index:03d}")
            if (file.requested_url or file.source_url) != doc["url"]:
                raise ParserError("Attachment URL contradicts snapshotted document metadata")
            kind = str(doc["kind"])
            expected = SCHEMAS[kind]
            count = 0
            with closing(openpyxl.load_workbook(file.path, read_only=True, data_only=True)) as workbook:
                for sheet in workbook:
                    # Excel formatting can inflate a 13-column sheet to 16,383 columns.
                    width = min(sheet.max_column or 1, 256)
                    header_row = None
                    for row_number, cells in enumerate(sheet.iter_rows(max_row=10, max_col=width, values_only=True), 1):
                        header = [clean_str(c) for c in cells]
                        while header and header[-1] is None:
                            header.pop()
                        if not any(header):
                            continue
                        if header[0] != "序号":
                            if doc["status"] == "final" and any("拟" in str(c) or "公示" in str(c) for c in header if c):
                                raise SchemaChangedError(f"{file.filename}:{sheet.title}: proposed title contradicts final metadata")
                            continue
                        if header != expected:
                            raise SchemaChangedError(f"{file.filename}:{sheet.title}: unexpected headers {header}")
                        header_row = row_number
                        break
                    if header_row is None:
                        if sheet.max_row and sheet.max_row > 1:
                            raise SchemaChangedError(f"{file.filename}:{sheet.title}: no supported price-table header")
                        continue
                    for row_number, cells in enumerate(sheet.iter_rows(min_row=header_row + 1, max_col=width, values_only=True), header_row + 1):
                        if not any(v is not None for v in cells):
                            continue
                        ref = f"{file.filename}:{sheet.title}:r{row_number}"
                        if any(v is not None for v in cells[len(expected):]):
                            raise SchemaChangedError(f"{ref}: values beyond supported columns")
                        raw = dict(zip(expected, cells))
                        ordinal = clean_str(raw["序号"])
                        if not ordinal or not ordinal.isdigit():
                            raise SchemaChangedError(f"{ref}: unexpected non-product row")
                        data = self._row(raw, doc, ref)
                        key = (data["code"], kind, doc.get("effective_from"), doc["published_at"])
                        comparable = {k: v for k, v in data.items() if k not in {"raw", "document"}}
                        if key in conflicts and conflicts[key] != comparable:
                            raise ParserError(f"{ref}: conflicting rows for the same product and publication")
                        conflicts[key] = comparable
                        records.append(ParsedRecord(ref, str(data["code"]), data, role="presentations"))
                        count += 1
            if not count:
                raise ParserError(f"{file.filename}: no product rows in reviewed price document")
            reasons = []
            if doc["status"] != "final":
                reasons.append("proposed document")
            effective = doc.get("effective_from")
            if effective and date.fromisoformat(str(effective)) > date.today():
                reasons.append("future effective date")
            if date.fromisoformat(str(doc["published_at"])) > date.today():
                reasons.append("future publication date")
            if kind != "gd_listing" and not doc.get("cny_pack_evidence_url"):
                reasons.append("CNY/pack pricing convention has not been verified")
            if kind.endswith("adjustment") and not effective:
                reasons.append("adjustment effective date is ambiguous")
            if reasons:
                issues.append(self.issue(
                    ErrorType.VALIDATION_ERROR, f"{file.filename}: product-only; " + "; ".join(reasons),
                    stage="parsing", document_url=doc["url"],
                ))
            for record in records[-count:]:
                record.data["price_eligible"] = not reasons
        self._enrich(records)
        rows = [r.data for r in records]
        return ParsedDataset(
            self.source_id, snapshot, records,
            [profile_rows("presentations", COLUMNS, rows, row_count=len(rows))],
            total_rows=len(rows), issues=issues,
        )

    def _row(self, raw: dict[str, object], doc: dict[str, object], ref: str) -> dict[str, object]:
        kind = doc["kind"]
        gd = kind == "gd_listing"
        admission = kind == "hb_admission"
        code = clean_str(raw.get("国家医保代码") if gd else raw.get("国家医保编码") if admission else raw.get("医保编码", raw.get("药品编码")))
        name = clean_str(raw.get("产品名称") if gd else raw.get("药品名称") if admission else raw.get("通用名"))
        authorisation = clean_str(raw.get("批准文号/注册证号") if gd else raw.get("批准文号"))
        if not code or not re.fullmatch(r"[XZ][A-Z0-9]+", code) or not name or not authorisation:
            raise ParserError(f"{ref}: missing/invalid national code, name or authorisation")
        form = clean_str(raw.get("剂型名称") if gd else raw.get("剂型"))
        strength = clean_str(raw.get("具体规格") if gd else raw.get("规格"))
        pack = clean_str(raw.get("包装规格"))
        if gd and not pack:
            raise ParserError(f"{ref}: missing Guangdong priced pack")
        quantity, unit = chinese_pack(pack)
        container = None
        token = None
        if not gd:
            quantity = positive_amount(raw.get("最小包装数量", raw.get("包装数量")), row_ref=ref)
            token = clean_str(raw.get("最小制剂单位"))
            unit = UNITS.get(token or "")
            container = clean_str(raw.get("最小包装单位"))
            if token and container:
                pack = f"{quantity}{token}/{container}"
        if unit == "unit" and ("胶囊" in (form or "")) and ((gd and pack and "粒/" in pack) or (not gd and token == "粒")):
            unit = "capsule"
        amount = positive_amount(raw.get("包装挂网价格（元）") if gd else raw.get("挂网价") if admission else raw.get("降价后价格"), row_ref=ref)
        previous = None if gd or admission else positive_amount(raw.get("降价前价格"), row_ref=ref)
        return {
            "code": code, "name": name, "form": form,
            "strength": strength,
            "pack": pack, "quantity": str(quantity) if quantity is not None else None, "unit": unit,
            "container": container, "manufacturer": clean_str(raw.get("生产企业/厂商") if gd else raw.get("生产企业名称")),
            "authorisation": authorisation, "brand": clean_str(raw.get("商品名称")),
            "amount": str(amount), "previous_amount": str(previous) if previous is not None else None,
            "raw": raw, "document": doc,
        }

    @staticmethod
    def _enrich(records: list[ParsedRecord]) -> None:
        descriptions: dict[str, dict[str, object]] = {}
        for record in records:
            row = record.data
            target = descriptions.setdefault(str(row["code"]), {})
            for field in ("authorisation", "strength", "form", "quantity", "pack", "unit", "container", "manufacturer", "brand"):
                value = row.get(field)
                if value is None:
                    continue
                old = target.get(field)
                if old is not None and unicodedata.normalize("NFKC", str(old)) != unicodedata.normalize("NFKC", str(value)):
                    raise ParserError(f"{record.row_ref}: conflicting {field} for exact national code {row['code']}")
                target[field] = value
        for record in records:
            for field, value in descriptions[str(record.key)].items():
                if record.data.get(field) is None:
                    record.data[field] = value

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord:
        row = record.data
        doc = row["document"]
        code = str(row["code"])
        traditional = code.startswith("Z")
        product = SourceProductData(
            product_code=code, code_type="CN_NHSA_DRUG_CODE", product_level=ProductLevel.PRESENTATION,
            name=row["name"], brand_name=None if row["brand"] == "无" else row["brand"],
            ingredient=None if traditional else row["name"], dosage_form=row["form"], strength_text=row["strength"],
            pack_text=row["pack"], pack_quantity=Decimal(row["quantity"]) if row["quantity"] else None,
            pack_unit=row["unit"], manufacturer=row["manufacturer"], authorisation_number=row["authorisation"],
            product_type="traditional_chinese_medicine" if traditional else "chemical_or_biological",
            detail_url=str(doc["notice_url"]),
            notes=f"Provincial disclosure only; latest collected observation, not verified current market coverage. "
                  f"Source form: {row['form']}; canonical form: {chinese_form(row['form']) or 'unresolved'}. "
                  f"Attachment: {doc['url']}. Local ID and declaring enterprise retained in raw snapshot. "
                  "No reimbursement entitlement or actual paid transaction inferred.",
        )
        prices = []
        if row["price_eligible"]:
            previous = f" Previously published amount {row['previous_amount']}; start date unknown." if row["previous_amount"] else ""
            prices.append(PriceFact(
                amount=Decimal(row["amount"]), currency="CNY", price_type=PriceBasis.PUBLIC_PROCUREMENT,
                price_status=PriceStatus.REFERENCE, price_unit="pack",
                price_variant="awarded_pack_price" if doc["kind"] == "hb_award_adjustment" else "guawang_pack_listing",
                unit_text="包装挂网价格（元）" if doc["kind"] == "gd_listing" else "挂网价; reviewed CNY/pack",
                pack_quantity=product.pack_quantity, pack_unit=product.pack_unit,
                valid_from=date.fromisoformat(doc["effective_from"]) if doc.get("effective_from") else None,
                date_basis="explicit" if doc.get("effective_from") else None,
                published_at=date.fromisoformat(doc["published_at"]), coverage_scope=f"province:{self.province}",
                notes="Published provincial listing/award, not an observed transaction or reimbursement. "
                      f"Attachment: {doc['url']}; notice: {doc['notice_url']}. "
                      f"Unit evidence: {doc.get('cny_pack_evidence_url') or 'explicit price-column header'}." + previous,
            ))
        return NormalizedRecord(record.row_ref, product, prices)
