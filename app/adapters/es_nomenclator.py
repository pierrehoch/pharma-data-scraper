"""Spain – Ministerio de Sanidad, Nomenclátor de Facturación (complete CSV export).

The CSV export link is discovered on the page (displaytag export parameter); the edition month is read
from the page title ("Nomenclátor de Facturación de OCTUBRE -2026") and the file name (YYYYMMDD_...).
Documentary status: informative (only texts signed by the DGFyPS are official).
"""

from __future__ import annotations

import re
from datetime import date, datetime

from app.adapters._common import join_notes, pack_price, parse_fields, read_table
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError
from app.core.parsing import clean_str, html_links, month_number, parse_date, parse_decimal
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.text import extract_brand

PAGE = "https://www.sanidad.gob.es/profesionales/nomenclator.do"
EDITION_RE = re.compile(r"Nomencl[áa]tor de Facturaci[óo]n de\s+([A-Za-zÁÉÍÓÚáéíóú]+)\s*-\s*(\d{4})", re.IGNORECASE)
REQUIRED = [
    "codigo_nacional",
    "nombre_del_producto_farmaceutico",
    "tipo_de_farmaco",
    "nombre_del_laboratorio_ofertante",
    "estado",
    "principio_activo_o_asociacion_de_principios_activos",
    "precio_venta_al_publico_con_iva",
    "precio_de_referencia",
]
ALIASES = {
    r"menor_precio_de_la_agrupacion_homogenea.*": "menor_precio_agrupacion",
    r"codigo_de_la_agrupacion_homogenea.*": "codigo_agrupacion",
    r"nombre_de_la_agrupacion_homogenea.*": "nombre_agrupacion",
}
FLAGS = {
    "diagnostico_hospitalario": "diagnóstico hospitalario",
    "tratamiento_de_larga_duracion": "tratamiento de larga duración",
    "especial_control_medico": "especial control médico",
}


class EsNomenclatorAdapter(SourceAdapter):
    adapter_id = "es_nomenclator"
    parser_type = "csv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        csv_url = None
        others = []
        for url, _ in html_links(page.text, PAGE):
            if "metodo=buscarProductos" in url and re.search(r"d-\d+-e=1", url):
                csv_url = csv_url or url
            elif "metodo=nomenclatorExcel" in url or "metodo=nomenclatorRDF" in url:
                others.append(url)
        if not csv_url:
            raise DiscoveryError("Nomenclátor page: CSV export link (metodo=buscarProductos&…-e=1) not found", details={"page": PAGE})
        m = EDITION_RE.search(page.text)
        published = None
        edition = None
        if m and month_number(m.group(1)):
            published = date(int(m.group(2)), month_number(m.group(1)), 1)
            edition = f"{m.group(1).upper()} {m.group(2)}"
        notes = [f"Other formats available (not downloaded): {others}"] if others else []
        return SourceArtifact(
            self.source_id,
            [ArtifactFile(url=csv_url, filename="nomenclator.csv", role="main", published_at=published)],
            published_at=published,
            edition=edition,
            discovered_from=PAGE,
            notes=notes,
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", REQUIRED, typed_columns=["codigo_nacional", "precio_venta_al_publico_con_iva", "precio_de_referencia"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        f = snapshot.file("main")
        records, profile, meta = read_table(
            f.path,
            role="main",
            delimiter=",",
            header_required=["Código Nacional", "Precio venta al público con IVA"],
            column_aliases=ALIASES,
            key_column="codigo_nacional",
        )
        context = {}
        m = re.match(r"(\d{8})_", f.filename)
        if m:
            context["file_date"] = datetime.strptime(m.group(1), "%Y%m%d").date()
        return ParsedDataset(self.source_id, snapshot, records, [profile], total_rows=meta["total_rows"], row_errors=meta["row_errors"], context=context)

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        name = clean_str(d.get("nombre_del_producto_farmaceutico")) or ""
        drug_type = clean_str(d.get("tipo_de_farmaco"))
        level = ProductLevel.PRESENTATION if drug_type else ProductLevel.DEVICE
        # "DEPAKINE 500 mg comprimidos gastrorresistentes, 20 comprimidos": pack after the last comma
        pack_text = name.rsplit(",", 1)[1] if "," in name else name
        edition = dataset.snapshot.published_at or dataset.context.get("file_date")
        orphan = (clean_str(d.get("medicamento_huerfano")) or "").upper()
        product = SourceProductData(
            product_code=clean_str(d.get("codigo_nacional")) or "",
            code_type="CODIGO_NACIONAL",
            product_level=level,
            name=name,
            brand_name=extract_brand(name) if drug_type else None,
            ingredient=clean_str(d.get("principio_activo_o_asociacion_de_principios_activos")) or clean_str(d.get("nombre_generico_efecto_y_accesorio")),
            pack_text=pack_text.strip() or None,
            manufacturer=clean_str(d.get("nombre_del_laboratorio_ofertante")),
            product_type=drug_type,
            group_code=clean_str(d.get("codigo_agrupacion")),
            group_name=clean_str(d.get("nombre_agrupacion")),
            marketing_status=clean_str(d.get("estado")),
            marketing_start_date=parse_date(d.get("fecha_de_alta_en_el_nomenclator"), ["%d/%m/%Y"]),
            marketing_end_date=parse_date(d.get("fecha_de_baja_en_el_nomenclator"), ["%d/%m/%Y"]),
            reimbursement_category=clean_str(d.get("aportacion_del_beneficiario")),
            restrictions=join_notes(*(label for col, label in FLAGS.items() if (clean_str(d.get(col)) or "").upper() in ("SI", "SÍ"))),
            orphan={"SI": True, "SÍ": True, "NO": False}.get(orphan),
            notes=f"código laboratorio {d['codigo_del_laboratorio_ofertante']}" if clean_str(d.get("codigo_del_laboratorio_ofertante")) else None,
            **parse_fields(strength_text=name, form_texts=[name], pack_text=pack_text, pack_decimal_sep=","),
        )
        common = dict(currency="EUR", product=product, edition_date=edition, coverage_scope=self.config.coverage_scope)
        prices = [
            pack_price(
                parse_decimal(d.get("precio_venta_al_publico_con_iva"), decimal_sep="."),
                price_type=PriceBasis.RETAIL,
                status=PriceStatus.REGULATED,
                vat_included=True,
                **common,
            ),
            pack_price(
                parse_decimal(d.get("precio_de_referencia"), decimal_sep="."),
                price_type=PriceBasis.REIMBURSEMENT,
                status=PriceStatus.CEILING,
                vat_included=True,
                variant="precio_de_referencia",
                **common,
            ),
            pack_price(
                parse_decimal(d.get("menor_precio_agrupacion"), decimal_sep="."),
                price_type=PriceBasis.REIMBURSEMENT,
                status=PriceStatus.CEILING,
                vat_included=True,
                variant="menor_precio_agrupacion_homogenea",
                **common,
            ),
        ]
        return NormalizedRecord(record.row_ref, product, [p for p in prices if p is not None])
