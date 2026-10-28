"""Italy – AIFA lists of Class A and Class H medicines ("per principio attivo" CSV files).

The latest edition is discovered on the AIFA page (file names embed the edition date, e.g.
Classe_A_per_principio_attivo_31-05-2026.csv). Files are ';'-separated, cp1252, decimal comma.
Prices are public prices *net of statutory reductions*.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.adapters._common import join_notes, pack_price, parse_fields, read_table
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError
from app.core.parsing import clean_str, html_links, parse_decimal
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.text import extract_brand

PAGE = "https://www.aifa.gov.it/liste-farmaci-a-h"
FILE_RE = re.compile(r"Classe_(?P<cls>[AH])_per_principio_attivo_(?P<date>\d{2}-\d{2}-\d{4})\.csv$", re.IGNORECASE)
ALIASES = {
    r"x_in_lista_di_trasparenza.*": "in_lista_trasparenza",
    r"codice_aic": "aic",
    r"prezzo_al_pubblico.*": "prezzo_al_pubblico",
    r"prezzo_ex_factory.*": "prezzo_ex_factory",
    r"prezzo_massimo_di_cessione.*": "prezzo_massimo_di_cessione",
    r"solo_in_lista_di_regione.*": "solo_in_lista_di_regione",
}
COMMON = ["principio_attivo", "descrizione_gruppo", "denominazione_e_confezione", "prezzo_al_pubblico", "titolare_aic", "aic", "codice_gruppo_equivalenza"]


class ItAifaAdapter(SourceAdapter):
    adapter_id = "it_aifa"
    parser_type = "csv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        latest: dict[str, tuple] = {}
        for url, _text in html_links(page.text, PAGE):
            m = FILE_RE.search(url)
            if not m:
                continue
            edition = datetime.strptime(m.group("date"), "%d-%m-%Y").date()
            cls = m.group("cls").upper()
            if cls not in latest or edition > latest[cls][0]:
                latest[cls] = (edition, url)
        if set(latest) != {"A", "H"}:
            raise DiscoveryError(f"AIFA page: expected Class A and H CSV links, found {sorted(latest)}", details={"page": PAGE})
        published = max(v[0] for v in latest.values())
        notes = []
        if latest["A"][0] != latest["H"][0]:
            notes.append(f"Class A ({latest['A'][0]}) and H ({latest['H'][0]}) editions differ")
        files = [
            ArtifactFile(url=url, filename=url.rsplit("/", 1)[-1], role=f"class_{cls.lower()}", published_at=edition)
            for cls, (edition, url) in sorted(latest.items())
        ]
        return SourceArtifact(self.source_id, files, published_at=published, edition=f"Aggiornamento del {published:%d/%m/%Y}", discovered_from=PAGE, notes=notes)

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [
            ExpectedSchema("class_a", COMMON, typed_columns=["aic", "prezzo_al_pubblico"]),
            ExpectedSchema("class_h", COMMON + ["prezzo_ex_factory", "prezzo_massimo_di_cessione"], typed_columns=["aic", "prezzo_al_pubblico"]),
        ]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        records, profiles, total, errors = [], [], 0, 0
        for role in ("class_a", "class_h"):
            recs, prof, meta = read_table(
                snapshot.file(role).path,
                role=role,
                delimiter=";",
                header_required=["Principio attivo", "Titolare AIC"],
                column_aliases=ALIASES,
                key_column="aic",
            )
            records += recs
            profiles.append(prof)
            total += meta["total_rows"]
            errors += meta["row_errors"]
        return ParsedDataset(self.source_id, snapshot, records, profiles, total_rows=total, row_errors=errors)

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        cls = "A" if record.role == "class_a" else "H"
        name = clean_str(d.get("denominazione_e_confezione")) or ""
        group = clean_str(d.get("descrizione_gruppo")) or ""
        pack_text = name.split("*", 1)[1] if "*" in name else name
        edition = dataset.snapshot.published_at
        product = SourceProductData(
            product_code=clean_str(d.get("aic")) or "",
            code_type="AIC",
            product_level=ProductLevel.PRESENTATION,
            name=name,
            brand_name=extract_brand(name),
            ingredient=clean_str(d.get("principio_attivo")),
            strength_text=group or None,
            route=(re.search(r"USO \w+", group) or [None])[0],
            pack_text=pack_text,
            manufacturer=clean_str(d.get("titolare_aic")),
            group_code=clean_str(d.get("codice_gruppo_equivalenza")),
            group_name=group or None,
            reimbursement_category=f"Classe {cls}",
            restrictions=join_notes(
                "lista di trasparenza" if clean_str(d.get("in_lista_trasparenza")) else None,
                f"solo in lista di regione: {d['solo_in_lista_di_regione']}" if clean_str(d.get("solo_in_lista_di_regione")) else None,
            ),
            **parse_fields(strength_text=group or name, form_texts=[pack_text, group], pack_text=pack_text),
        )
        common = dict(currency="EUR", product=product, edition_date=edition, coverage_scope=self.config.coverage_scope)
        prices = [
            pack_price(
                parse_decimal(d.get("prezzo_al_pubblico"), decimal_sep=","),
                price_type=PriceBasis.RETAIL,
                status=PriceStatus.REGULATED,
                vat_included=True,
                statutory_reduction_included=True,
                **common,
            )
        ]
        if cls == "H":
            prices.append(
                pack_price(
                    parse_decimal(d.get("prezzo_ex_factory"), decimal_sep=","),
                    price_type=PriceBasis.EX_FACTORY,
                    status=PriceStatus.REGULATED,
                    vat_included=False,
                    **common,
                )
            )
            prices.append(
                pack_price(
                    parse_decimal(d.get("prezzo_massimo_di_cessione"), decimal_sep=","),
                    price_type=PriceBasis.PUBLIC_PROCUREMENT,
                    status=PriceStatus.CEILING,
                    vat_included=None,
                    variant="prezzo_massimo_di_cessione",
                    notes="prezzo massimo di cessione al SSN",
                    **common,
                )
            )
        return NormalizedRecord(record.row_ref, product, [p for p in prices if p is not None])
