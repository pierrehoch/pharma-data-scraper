"""China – manual import only (adapter_status = manual_or_unsupported).

No documented public machine-readable source: the NHSA/price registration query interfaces are interactive
and protected; this adapter never tries to bypass anti-bot, CAPTCHA or authentication mechanisms.
Operators may drop CSV files of *public* volume-based-procurement (集采) results into
  $MANUAL_IMPORT_DIR/cn_nhsa/*.csv
following data/manual/cn_nhsa/TEMPLATE.csv.example. Without files, the job ends with status 'unsupported'.
"""

from __future__ import annotations

from app.adapters._common import pack_price, parse_fields, read_table
from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, PriceBasis, PriceStatus, ProductLevel
from app.core.errors import SchemaChangedError, SourceUnsupportedError
from app.core.parsing import clean_str, parse_date, parse_decimal
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData

COLUMNS = [
    "product_id", "generic_name", "brand_name", "strength", "dosage_form", "pack_quantity", "pack_unit", "manufacturer",
    "procurement_batch", "price", "currency", "price_unit", "effective_from", "source_document_url",
]


class CnNhsaAdapter(SourceAdapter):
    adapter_id = "cn_nhsa"
    parser_type = "csv (manual)"
    parser_version = "1.0"
    status = AdapterStatus.MANUAL_OR_UNSUPPORTED
    limitations = "No automatable public source; manual CSV import of public VBP results only."

    def _manual_dir(self):  # noqa: ANN202
        return self.ctx.settings.manual_import_dir / self.source_id

    def discover(self) -> SourceArtifact:
        folder = self._manual_dir()
        files = sorted(folder.glob("*.csv")) if folder.is_dir() else []
        if not files:
            raise SourceUnsupportedError(
                "China: no documented public machine-readable price source; automated access is not attempted "
                f"(no anti-bot/CAPTCHA circumvention). To import public VBP results, place CSV files in {folder} "
                "using the TEMPLATE.csv.example columns.",
                details={"manual_import_dir": str(folder)},
            )
        return SourceArtifact(
            self.source_id,
            [ArtifactFile(url=f.resolve().as_uri(), filename=f.name, role=f"manual_{i:03d}") for i, f in enumerate(files)],
            discovered_from=str(folder),
            notes=["manual import"],
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return []  # one file per operator drop (dynamic roles): the template is enforced in parse()

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        records, profiles, total, errors = [], [], 0, 0
        for f in snapshot.files_with_role_prefix("manual_"):
            recs, prof, meta = read_table(f.path, role=f.role, header_required=["product_id", "price", "currency"], key_column="product_id")
            missing = [c for c in COLUMNS if c not in prof.columns]
            if missing:
                raise SchemaChangedError(f"Manual file {f.filename} does not follow the template: missing {missing}")
            records += recs
            profiles.append(prof)
            total += meta["total_rows"]
            errors += meta["row_errors"]
        return ParsedDataset(self.source_id, snapshot, records, profiles, total_rows=total, row_errors=errors)

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        pack_text = f"{d.get('pack_quantity')} {d.get('pack_unit')}"
        batch = clean_str(d.get("procurement_batch"))
        product = SourceProductData(
            product_code=clean_str(d.get("product_id")) or "",
            code_type="CN_MANUAL",
            product_level=ProductLevel.PRESENTATION,
            name=clean_str(d.get("brand_name")) or clean_str(d.get("generic_name")),
            brand_name=clean_str(d.get("brand_name")),
            ingredient=clean_str(d.get("generic_name")),
            strength_text=clean_str(d.get("strength")),
            dosage_form=clean_str(d.get("dosage_form")),
            pack_text=pack_text,
            manufacturer=clean_str(d.get("manufacturer")),
            reimbursement_category=f"VBP {batch}" if batch else None,
            detail_url=clean_str(d.get("source_document_url")),
            **parse_fields(strength_text=d.get("strength"), form_texts=[d.get("dosage_form")], pack_text=pack_text),
        )
        price = pack_price(
            parse_decimal(d.get("price")),
            currency=clean_str(d.get("currency")) or "CNY",
            price_type=PriceBasis.PUBLIC_PROCUREMENT,
            status=PriceStatus.OBSERVED,
            product=product,
            vat_included=None,
            valid_from=parse_date(d.get("effective_from"), ["%Y-%m-%d"]),
            coverage_scope=self.config.coverage_scope,
            unit_text=clean_str(d.get("price_unit")) or "pack",
            notes=f"procurement batch {batch}" if batch else None,
        )
        return NormalizedRecord(record.row_ref, product, [price] if price else [])
