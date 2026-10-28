"""UK – NHSBSA Drug Tariff Part VIIIA (England & Wales) monthly CSV.

Latest monthly file discovered on the Part VIII page ('Part VIIIA <Mon> <YY|YYYY>.csv').
Basic prices are in pence and exclude VAT. VMPP SNOMED codes are shared with dm+d.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

from app.adapters._common import pack_price, parse_fields, read_table
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError
from app.core.parsing import clean_str, html_links, month_number, parse_decimal
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.packs import Pack, canonical_pack_unit

PAGE = "https://www.nhsbsa.nhs.uk/pharmacies-gp-practices-and-appliance-contractors/drug-tariff/drug-tariff-part-viii"
FILE_RE = re.compile(r"Part VIIIA (?P<mon>[A-Za-z]+) (?P<year>\d{2,4})(?:\.xls)?\.csv$", re.IGNORECASE)
ALIASES = {r"col_2": "pack_unit"}
REQUIRED = ["medicine", "pack_size", "pack_unit", "vmp_snomed_code", "vmpp_snomed_code", "drug_tariff_category", "basic_price"]


class UkDrugTariffAdapter(SourceAdapter):
    adapter_id = "uk_drug_tariff"
    parser_type = "csv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        found = []
        for url, _ in html_links(page.text, PAGE):
            m = FILE_RE.search(unquote(url))
            if not m or not month_number(m.group("mon")):
                continue
            year = int(m.group("year"))
            year = year + 2000 if year < 100 else year
            found.append((date(year, month_number(m.group("mon")), 1), url))
        if not found:
            raise DiscoveryError("Drug Tariff Part VIII page: no 'Part VIIIA <month> <year>.csv' link found", details={"page": PAGE})
        month, url = max(found)
        return SourceArtifact(
            self.source_id,
            [ArtifactFile(url=url, filename=unquote(url.rsplit("/", 1)[-1]), role="main", published_at=month)],
            published_at=month,
            edition=f"Drug Tariff {month:%B %Y}",
            discovered_from=PAGE,
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", REQUIRED, typed_columns=["pack_size", "vmpp_snomed_code", "basic_price"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        records, profile, meta = read_table(
            snapshot.file("main").path,
            role="main",
            delimiter=",",
            header_required=["Medicine", "Pack size", "VMPP Snomed Code", "Basic Price"],
            column_aliases=ALIASES,
            key_column="vmpp_snomed_code",
        )
        return ParsedDataset(self.source_id, snapshot, records, [profile], total_rows=meta["total_rows"], row_errors=meta["row_errors"], context={"preamble": meta["preamble"]})

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        medicine = clean_str(d.get("medicine")) or ""
        qty = parse_decimal(d.get("pack_size"), decimal_sep=".")
        unit_raw = clean_str(d.get("pack_unit"))
        unit = canonical_pack_unit(unit_raw) or (unit_raw or None)
        ingredient = re.split(r"\s\d", medicine, maxsplit=1)[0]
        category = clean_str(d.get("drug_tariff_category"))
        product = SourceProductData(
            product_code=clean_str(d.get("vmpp_snomed_code")) or "",
            code_type="SNOMED_VMPP",
            product_level=ProductLevel.PRESENTATION,
            name=medicine,
            ingredient=ingredient or None,
            pack_text=f"{d.get('pack_size')} {unit_raw or ''}".strip(),
            group_code=clean_str(d.get("vmp_snomed_code")),
            reimbursement_category=f"Drug Tariff {category}" if category else None,
            **parse_fields(strength_text=medicine, pack=Pack(qty, unit, f"{d.get('pack_size')} {unit_raw}") if qty and unit else None),
        )
        pence = parse_decimal(d.get("basic_price"), decimal_sep=".")
        price = pack_price(
            pence / Decimal(100) if pence is not None else None,
            currency="GBP",
            price_type=PriceBasis.REIMBURSEMENT,
            status=PriceStatus.REGULATED,
            product=product,
            vat_included=False,
            edition_date=dataset.snapshot.published_at,
            coverage_scope=self.config.coverage_scope,
            variant="drug_tariff_basic_price",
            unit_text="pack (published in pence)",
        )
        return NormalizedRecord(record.row_ref, product, [price] if price else [])
