"""Canada – Health Canada Drug Product Database (DPD) bulk extract (allfiles.zip, header-less CSV files).

Reference product master (DIN, ingredients, strengths, forms, routes, ATC). Contains NO prices.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict

from app.adapters._common import join_notes, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import ProductLevel
from app.core.errors import ParserError
from app.core.parsing import clean_str, decode_bytes, html_links, parse_date, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, IngredientComponent, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.observability import get_logger

log = get_logger(__name__)

PAGE = "https://www.canada.ca/en/health-canada/services/drugs-health-products/drug-products/drug-product-database/what-data-extract-drug-product-database.html"
FALLBACK_URL = "https://www.canada.ca/content/dam/hc-sc/documents/services/drug-product-database/allfiles.zip"
LAYOUT = {
    "drug": ["drug_code", "product_categorization", "class", "drug_identification_number", "brand_name", "descriptor",
             "pediatric_flag", "accession_number", "number_of_ais", "last_update_date", "ai_group_no", "class_f",
             "brand_name_f", "descriptor_f"],
    "ingred": ["drug_code", "active_ingredient_code", "ingredient", "ingredient_supplied_ind", "strength", "strength_unit",
               "strength_type", "dosage_value", "base", "dosage_unit", "notes", "ingredient_f", "strength_unit_f",
               "strength_type_f", "dosage_unit_f"],
    "form": ["drug_code", "pharm_form_code", "pharmaceutical_form", "pharmaceutical_form_f"],
    "route": ["drug_code", "route_of_administration_code", "route_of_administration", "route_of_administration_f"],
    "ther": ["drug_code", "tc_atc_number", "tc_atc", "tc_atc_f"],
    "status": ["drug_code", "current_status_flag", "status", "history_date", "status_f", "lot_number", "expiration_date"],
}


class CaHcDpdAdapter(SourceAdapter):
    adapter_id = "ca_hc_dpd"
    parser_type = "zip/csv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        url = FALLBACK_URL
        notes = []
        try:
            page = self.ctx.http.get_text(PAGE)
            links = [u for u, _ in html_links(page.text, PAGE) if u.lower().endswith("/allfiles.zip")]
            if links:
                url = links[0]
            else:
                notes.append("allfiles.zip link not found on the read-me page: using the documented URL")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Read-me page unavailable ({exc}); using the documented URL")
        return SourceArtifact(self.source_id, [ArtifactFile(url=url, filename="allfiles.zip", role="main")], discovered_from=PAGE, notes=notes)

    def download(self, artifact: SourceArtifact) -> RawSnapshot:
        snapshot = super().download(artifact)
        # The extract carries no edition date: use the newest member timestamp inside the archive.
        try:
            with zipfile.ZipFile(snapshot.file("main").path) as zf:
                stamps = [zi.date_time for zi in zf.infolist()]
            if stamps:
                from datetime import date

                y, m, d = max(stamps)[:3]
                snapshot.published_at = date(y, m, d)
        except zipfile.BadZipFile:
            pass
        return snapshot

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema(name, cols, expected_column_count=len(cols)) for name, cols in LAYOUT.items() if name in ("drug", "ingred", "form", "route", "ther")]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        path = snapshot.file("main").path
        tables: dict[str, list[dict]] = {}
        profiles = []
        row_errors = 0
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ParserError(f"{path.name} is not a valid ZIP archive") from exc
        with zf:
            names = {n.lower(): n for n in zf.namelist()}
            for table, cols in LAYOUT.items():
                member = names.get(f"{table}.txt")
                if member is None:
                    if table in ("drug", "ingred"):
                        raise ParserError(f"DPD extract has no {table}.txt", details={"members": list(names)})
                    continue
                text, _ = decode_bytes(zf.read(member))
                rows = []
                widths = set()
                for raw in csv.reader(io.StringIO(text)):
                    if not raw:
                        continue
                    widths.add(len(raw))
                    if len(raw) < len(cols):
                        raw = raw + [""] * (len(cols) - len(raw))
                    elif len(raw) > len(cols):
                        row_errors += 1
                        continue
                    rows.append(dict(zip(cols, (c.strip() for c in raw))))
                tables[table] = rows
                log.info("Read %s: %s rows", member, f"{len(rows):,}")
                observed = list(cols) if max(widths, default=len(cols)) <= len(cols) else list(cols) + [f"col_{i}" for i in range(len(cols), max(widths))]
                profiles.append(profile_rows(table, observed, rows, row_count=len(rows)))
        by_code: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for table in ("ingred", "form", "route", "ther", "status"):
            for r in tables.get(table, []):
                by_code[r["drug_code"]][table].append(r)
        records = []
        for i, drug in enumerate(tables["drug"]):
            if drug.get("class", "").lower() != "human":
                continue
            rel = by_code.get(drug["drug_code"], {})
            data = {**drug, **{f"_{k}": v for k, v in rel.items()}}
            records.append(ParsedRecord(row_ref=f"drug.txt:{i + 1}", key=clean_str(drug.get("drug_identification_number")), data=data))
        return ParsedDataset(self.source_id, snapshot, records, profiles, total_rows=len(tables["drug"]), row_errors=row_errors)

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        ingreds = d.get("_ingred") or []
        first = ingreds[0] if ingreds else {}
        strength = f"{first.get('strength')} {first.get('strength_unit')}".strip() if first else None
        if first.get("dosage_value") and first.get("dosage_unit"):
            strength = f"{strength}/{first['dosage_value']} {first['dosage_unit']}"
        forms = [f.get("pharmaceutical_form") for f in d.get("_form") or []]
        routes = [r.get("route_of_administration") for r in d.get("_route") or []]
        ther = next((t for t in d.get("_ther") or [] if t.get("tc_atc_number")), {})
        atc = ther.get("tc_atc_number")
        current = next((s for s in d.get("_status") or [] if s.get("current_status_flag") == "Y"), {})
        ingredient = " + ".join(i.get("ingredient", "") for i in ingreds) or None
        product = SourceProductData(
            product_code=clean_str(d.get("drug_identification_number")) or d["drug_code"],
            code_type="DIN",
            product_level=ProductLevel.PRODUCT,
            name=clean_str(d.get("brand_name")),
            brand_name=clean_str(d.get("brand_name")),
            ingredient=ingredient,
            strength_text=strength,
            dosage_form=", ".join(f for f in forms if f) or None,
            route=", ".join(r for r in routes if r) or None,
            atc_code=atc,
            therapeutic_class=clean_str(ther.get("tc_atc")),
            product_type=join_notes(clean_str(d.get("class")), clean_str(d.get("product_categorization"))),
            group_code=clean_str(d.get("drug_code")),
            marketing_status=clean_str(current.get("status")),
            marketing_start_date=parse_date(current.get("history_date"), ["%d-%b-%Y", "%Y-%m-%d"]) if clean_str(current.get("history_date")) else None,
            notes=join_notes(
                clean_str(d.get("descriptor")),
                f"{d['number_of_ais']} active ingredient(s)" if clean_str(d.get("number_of_ais")) else None,
                "pediatric" if d.get("pediatric_flag") == "Y" else None,
            ),
            **parse_fields(strength_text=strength),
        )
        return NormalizedRecord(record.row_ref, product, [], ingredient_components=[
            IngredientComponent(i.get("ingredient", ""), "ca_dpd_ingredient", i.get("active_ingredient_code"))
            for i in ingreds
        ] or None)
