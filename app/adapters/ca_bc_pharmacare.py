"""BC PharmaCare benefit prices, per official unit and provincial plan.

Only records effective in the downloaded edition are retained. Zero price fields
are placeholders, not free medicines. DIN/PIN identifiers are preserved as text.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime

from app.adapters._common import join_notes, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError
from app.core.parsing import clean_str, html_links, parse_date, parse_decimal, profile_rows, slug
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, PriceFact, RawSnapshot, SourceArtifact, SourceProductData
from app.observability import Progress

PAGE = "https://www2.gov.bc.ca/gov/content/health/practitioner-professional-resources/pharmacare/health-industry-professionals/downloadable-drug-data-files"
REQUIRED = ["din_pin", "plan", "rec_eff_date", "rec_end_date", "brand_nm", "generic_nm", "dosage_form", "max_price", "lca_price", "rdp_price"]
PRICE_FIELDS = ("max_price", "lca_price", "rdp_price")


class CaBcPharmacareAdapter(SourceAdapter):
    adapter_id = "ca_bc_pharmacare"
    parser_type = "zip/csv"
    limitations = "BC benefit prices only, not Canadian retail prices. PINs are not necessarily DINs."

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        urls = sorted({url for url, _ in html_links(page.text, PAGE) if url.lower().endswith("/pddf.zip")})
        if len(urls) != 1:
            raise DiscoveryError(f"BC PharmaCare: expected one pddf.zip link, found {len(urls)}")
        return SourceArtifact(self.source_id, [ArtifactFile(urls[0], "pddf.zip", "main")], discovered_from=PAGE)

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", REQUIRED, typed_columns=["din_pin", "max_price"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        products: dict[str, dict] = {}
        total = 0
        with zipfile.ZipFile(snapshot.file().path) as archive:
            members = [name for name in archive.namelist() if re.search(r"pddf_\d{4}_\d{2}_\d{2}\.csv$", name)]
            if len(members) != 1:
                raise ParserError("BC PharmaCare ZIP must contain one dated pddf_YYYY_MM_DD.csv")
            member = members[0]
            match = re.search(r"pddf_(\d{4}_\d{2}_\d{2})\.csv$", member)
            if match is None:
                raise ParserError("BC PharmaCare CSV has no edition date")
            edition = datetime.strptime(match.group(1), "%Y_%m_%d").date()
            as_of = edition.strftime("%Y%m%d")
            with archive.open(member) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
                header = [slug(value) for value in (reader.fieldnames or [])]
                missing = set(REQUIRED) - set(header)
                if missing:
                    raise SchemaChangedError(f"BC PharmaCare CSV missing columns: {sorted(missing)}")
                progress = Progress("read BC PharmaCare", unit="rows", check_every=2000)
                for line_no, values in enumerate(reader, start=2):
                    total += 1
                    progress.update(products=len(products))
                    if None in values:
                        raise ParserError(f"BC PharmaCare row {line_no}: unexpected CSV width")
                    row = {slug(key): (value or "").strip() for key, value in values.items()}
                    if row["plan"] == "NB":
                        continue
                    for column in ("rec_eff_date", "rec_end_date"):
                        if not re.fullmatch(r"\d{8}", row[column]) or parse_date(row[column], ["%Y%m%d"]) is None:
                            raise ParserError(f"BC PharmaCare row {line_no}: invalid {column}")
                    if not (row["rec_eff_date"] <= as_of <= row["rec_end_date"]):
                        continue
                    din = row["din_pin"]
                    if not re.fullmatch(r"\d{8}", din):
                        raise ParserError(f"BC PharmaCare row {line_no}: invalid DIN/PIN {din!r}")
                    item = products.setdefault(din, {"product": row, "prices": {}, "row_ref": f"{member}:{line_no}"})
                    if row["rec_eff_date"] > item["product"]["rec_eff_date"]:
                        item["product"] = row
                    for column in PRICE_FIELDS:
                        if column == "rdp_price" and row["plan"] in re.split(r"[\s,]+", row.get("rdp_excl_plans", "")):
                            continue
                        if not row[column]:
                            continue
                        amount = parse_decimal(row[column], decimal_sep=".")
                        if amount is None or amount < 0:
                            raise ParserError(f"BC PharmaCare row {line_no}: invalid {column} {row[column]!r}")
                        if amount == 0:
                            continue
                        key = (column, row[column], row["rec_eff_date"], row["rec_end_date"])
                        price = item["prices"].setdefault(key, {"field": column, "amount": row[column], "from": row["rec_eff_date"], "to": row["rec_end_date"], "plans": set()})
                        price["plans"].add(row["plan"])
        if not products:
            raise ParserError("BC PharmaCare: no benefit records effective in the file edition")
        records = []
        for din, item in products.items():
            prices = [{**price, "plans": sorted(price["plans"])} for price in item["prices"].values()]
            records.append(ParsedRecord(item["row_ref"], din, {**item["product"], "prices": prices}))
        profile = profile_rows("main", header, [record.data for record in records], row_count=len(records))
        return ParsedDataset(self.source_id, snapshot, records, [profile], total_rows=len(records), context={"edition_date": edition, "raw_rows": total})

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord:
        row = record.data
        product = SourceProductData(
            product_code=row["din_pin"],
            code_type="DIN_PIN",
            product_level=ProductLevel.PRODUCT,
            name=clean_str(row["brand_nm"]),
            brand_name=clean_str(row["brand_nm"]),
            ingredient=clean_str(row["generic_nm"]),
            dosage_form=clean_str(row["dosage_form"]),
            reimbursement_category="BC PharmaCare",
            reimbursement_conditions=clean_str(row.get("pcare_plan_desc")),
            notes=join_notes(f"Manufacturer code: {row['manuf']}" if row.get("manuf") else None, "DIN/PIN: may be a provincial PIN rather than a Health Canada DIN"),
            **parse_fields(strength_text=row["generic_nm"]),
        )
        prices = []
        for item in row["prices"]:
            end = parse_date(item["to"], ["%Y%m%d"])
            amount = parse_decimal(item["amount"], decimal_sep=".")
            if amount is None or amount <= 0:
                raise ParserError(f"BC PharmaCare {record.key}: invalid retained price")
            prices.append(PriceFact(
                amount=amount,
                currency="CAD",
                price_type=PriceBasis.REIMBURSEMENT,
                price_status=PriceStatus.CEILING,
                price_unit="unit",
                unit_text="PharmaCare official pricing unit (not a pack)",
                price_variant=f"{item['field']}:plans:{','.join(item['plans'])}",
                valid_from=parse_date(item["from"], ["%Y%m%d"]),
                valid_to=end if end and end.year < 4000 else None,
                date_basis="explicit",
                published_at=dataset.context["edition_date"],
                coverage_scope=self.config.coverage_scope,
                notes=f"{item['field']}; plans: {', '.join(item['plans'])}; edition {dataset.context['edition_date']}; zero fields excluded",
            ))
        return NormalizedRecord(record.row_ref, product, prices)
