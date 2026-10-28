"""France – Base de Données Publique des Médicaments (BDPM).

Files (tab-separated, no header, encodings differ per file and are detected):
  CIS_bdpm.txt (12 cols), CIS_CIP_bdpm.txt (13 cols, prices), CIS_COMPO_bdpm.txt (8 cols).
Licence: do not alter the data; cite source + update date (kept in provenance).
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import datetime

from app.adapters._common import join_notes, pack_price, parse_fields, read_table
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError
from app.core.parsing import clean_str, html_links, parse_date, parse_decimal
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, IngredientComponent, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.text import extract_brand

PAGE = "https://base-donnees-publique.medicaments.gouv.fr/telechargement"

CIS_COLUMNS = [
    "cis", "denomination", "forme_pharmaceutique", "voies_administration", "statut_amm", "type_procedure",
    "etat_commercialisation", "date_amm", "statut_bdm", "numero_autorisation_europeenne", "titulaires",
    "surveillance_renforcee",
]
CIP_COLUMNS = [
    "cis", "cip7", "libelle_presentation", "statut_administratif", "etat_commercialisation",
    "date_declaration_commercialisation", "cip13", "agrement_collectivites", "taux_remboursement",
    "prix_hors_honoraires", "prix_public", "honoraires", "indications_remboursement",
]
COMPO_COLUMNS = [
    "cis", "designation_element", "code_substance", "denomination_substance", "dosage", "reference_dosage",
    "nature_composant", "numero_liaison",
]
FILES = {"cis": "CIS_bdpm.txt", "cip": "CIS_CIP_bdpm.txt", "compo": "CIS_COMPO_bdpm.txt"}


class FrBdpmAdapter(SourceAdapter):
    adapter_id = "fr_bdpm"
    parser_type = "tsv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        m = re.search(r"mise à jour le\s*(\d{2}/\d{2}/\d{4})", page.text, re.IGNORECASE)
        published = datetime.strptime(m.group(1), "%d/%m/%Y").date() if m else None
        links = {url.rsplit("/", 1)[-1]: url for url, _ in html_links(page.text, PAGE) if "/download/file/" in url}
        files = []
        for role, name in FILES.items():
            if name not in links:
                raise DiscoveryError(
                    f"BDPM download page no longer links to {name} – page structure changed",
                    details={"found": sorted(links)},
                )
            files.append(ArtifactFile(url=links[name], filename=name, role=role, published_at=published))
        notes = [] if published else ["Update date ('Dernière mise à jour le …') not found on the page"]
        return SourceArtifact(self.source_id, files, published_at=published, edition=published and published.isoformat(), discovered_from=PAGE, notes=notes)

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [
            ExpectedSchema("cis", CIS_COLUMNS, expected_column_count=12, min_rows=1),
            ExpectedSchema("cip", CIP_COLUMNS, expected_column_count=13, min_rows=1,
                           typed_columns=["cis", "cip7", "cip13", "prix_hors_honoraires", "prix_public", "honoraires"]),
            ExpectedSchema("compo", COMPO_COLUMNS, expected_column_count=8, min_rows=1),
        ]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        cis_recs, cis_prof, cis_meta = read_table(snapshot.file("cis").path, role="cis", delimiter="\t", quoting=csv.QUOTE_NONE, columns=CIS_COLUMNS, key_column="cis")
        cip_recs, cip_prof, cip_meta = read_table(snapshot.file("cip").path, role="cip", delimiter="\t", quoting=csv.QUOTE_NONE, columns=CIP_COLUMNS, key_column="cip13")
        compo_recs, compo_prof, compo_meta = read_table(snapshot.file("compo").path, role="compo", delimiter="\t", quoting=csv.QUOTE_NONE, columns=COMPO_COLUMNS)
        cis_by_id = {r.data["cis"]: r.data for r in cis_recs}
        compo_by_cis: dict[str, list[dict]] = defaultdict(list)
        for r in compo_recs:
            compo_by_cis[r.data["cis"]].append(r.data)
        for rec in cip_recs:
            rec.data = {**rec.data, "_cis": cis_by_id.get(rec.data["cis"]), "_compo": compo_by_cis.get(rec.data["cis"], [])}
        return ParsedDataset(
            source_id=self.source_id,
            snapshot=snapshot,
            records=cip_recs,
            profiles=[cis_prof, cip_prof, compo_prof],
            total_rows=cip_meta["total_rows"],
            row_errors=cip_meta["row_errors"] + cis_meta["row_errors"] + compo_meta["row_errors"],
            context={"encodings": {"cis": cis_meta["encoding"], "cip": cip_meta["encoding"], "compo": compo_meta["encoding"]}},
        )

    def record_matches_product_ids(self, record: ParsedRecord, product_ids: set[str]) -> bool:
        return record.data.get("cip13") in product_ids or record.data.get("cip7") in product_ids or record.data.get("cis") in product_ids

    @staticmethod
    def _ingredients(compo: list[dict]) -> tuple[list[str], list[str], str | None]:
        """Active moieties (FT when given, else SA), all SA names, and the first dosage."""
        by_link: dict[str, dict[str, dict]] = defaultdict(dict)
        for c in compo:
            by_link[c["numero_liaison"]][c["nature_composant"]] = c
        names, salts, dosage = [], [], None
        for link in sorted(by_link, key=lambda x: int(x) if x.isdigit() else 0):
            parts = by_link[link]
            chosen = parts.get("FT") or parts.get("SA")
            if not chosen:
                continue
            names.append(chosen["denomination_substance"])
            if "SA" in parts:
                salts.append(parts["SA"]["denomination_substance"])
            if dosage is None:
                dosage = clean_str(chosen.get("dosage"))
        return names, salts, dosage

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        cis = d.get("_cis") or {}
        compo = d.get("_compo") or []
        names, salts, dosage = self._ingredients(compo)
        published = dataset.snapshot.published_at
        denomination = clean_str(cis.get("denomination"))
        pack_text = clean_str(d.get("libelle_presentation"))
        product = SourceProductData(
            product_code=d["cip13"],
            code_type="CIP13",
            product_level=ProductLevel.PRESENTATION,
            name=clean_str(f"{denomination or ''} – {pack_text or ''}"),
            brand_name=extract_brand(denomination),
            ingredient=" + ".join(salts or names) or None,
            strength_text=dosage,
            dosage_form=clean_str(cis.get("forme_pharmaceutique")),
            route=clean_str(cis.get("voies_administration")),
            pack_text=pack_text,
            manufacturer=clean_str(cis.get("titulaires")),
            group_code=clean_str(d.get("cis")),
            group_name=denomination,
            authorisation_number=clean_str(cis.get("numero_autorisation_europeenne")),
            authorisation_status=clean_str(cis.get("statut_amm")),
            marketing_status=join_notes(clean_str(d.get("statut_administratif")), clean_str(d.get("etat_commercialisation"))),
            authorisation_date=parse_date(cis.get("date_amm"), ["%d/%m/%Y"]),
            marketing_start_date=parse_date(d.get("date_declaration_commercialisation"), ["%d/%m/%Y"]),
            reimbursement_rate=clean_str(d.get("taux_remboursement")),
            reimbursement_conditions=clean_str(re.sub(r"<br\s*/?>", "\n", d.get("indications_remboursement") or "")),
            restrictions=join_notes(
                "agrément collectivités" if (d.get("agrement_collectivites") or "").lower() == "oui" else None,
                "surveillance renforcée" if (cis.get("surveillance_renforcee") or "").lower() == "oui" else None,
            ),
            notes=join_notes(
                f"CIP7 {d['cip7']}" if d.get("cip7") else None,
                f"procédure: {cis['type_procedure']}" if cis.get("type_procedure") else None,
                f"statut BDM: {cis['statut_bdm']}" if clean_str(cis.get("statut_bdm")) else None,
            ),
            **parse_fields(
                strength_text=dosage or denomination,
                form_texts=[cis.get("forme_pharmaceutique"), pack_text],
                pack_text=pack_text,
            ),
        )
        common = dict(currency="EUR", product=product, vat_included=True, edition_date=published, coverage_scope=self.config.coverage_scope)
        prices = [
            pack_price(
                parse_decimal(d.get("prix_public"), decimal_sep=","),
                price_type=PriceBasis.RETAIL,
                status=PriceStatus.REGULATED,
                variant="incl_dispensing_fee",
                dispensing_fee=parse_decimal(d.get("honoraires"), decimal_sep=","),
                **common,
            ),
            pack_price(
                parse_decimal(d.get("prix_hors_honoraires"), decimal_sep=","),
                price_type=PriceBasis.RETAIL,
                status=PriceStatus.REGULATED,
                variant="excl_dispensing_fee",
                **common,
            ),
        ]
        components = [
            IngredientComponent(c["denomination_substance"], "fr_bdpm_substance", c.get("code_substance"))
            for c in compo if c.get("nature_composant") == "SA"
        ]
        return NormalizedRecord(
            record.row_ref, product, [p for p in prices if p is not None], ingredient_components=components or None,
        )
