"""Bounded official CIMA presentation reconciliation, deliberately without prices."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from app.adapters._common import join_notes, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError
from app.core.parsing import clean_str, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData

API = "https://cima.aemps.es/cima/rest/"
COLUMNS = ["cn", "nombre", "nregistro", "ingredient", "comerc"]
DEFAULT_NAMES = ["dupixent", "keytruda", "opdivo", "humira", "lynparza"]


def _date(value: object):
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ParserError(f"CIMA invalid millisecond timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()


class EsCimaAdapter(SourceAdapter):
    adapter_id = "es_cima"
    parser_type = "json"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    limitations = "Targeted CIMA product reconciliation only; medicine_names defines scope. CIMA publishes no prices; national codes may be joined to Nomenclator, never inferred from names."

    def discover(self) -> SourceArtifact:
        names = self.option("medicine_names", DEFAULT_NAMES)
        if not isinstance(names, list) or not names or not all(isinstance(name, str) and name.strip() for name in names):
            raise DiscoveryError("CIMA medicine_names must be a non-empty list of names")
        files, registrations = [], set()
        for index, name in enumerate(names):
            page = 1
            while True:
                url = API + "medicamentos?" + urlencode({"nombre": name.strip(), "pagina": page})
                data = self.ctx.http.get_json(url)
                if not isinstance(data, dict) or not isinstance(data.get("resultados"), list) or not isinstance(data.get("totalFilas"), int):
                    raise DiscoveryError(f"CIMA invalid search response for {name!r}, page {page}")
                results = data["resultados"]
                if not results and data["totalFilas"]:
                    raise DiscoveryError(f"CIMA unexpectedly empty page {page} for {name!r}")
                files.append(ArtifactFile(url, f"search_{index}_{page}.json", role="search"))
                for item in results:
                    code = item.get("nregistro") if isinstance(item, dict) else None
                    if not isinstance(code, str) or not code:
                        raise DiscoveryError("CIMA medication has no registration number")
                    registrations.add(code)
                size = data.get("tamanioPagina")
                if not isinstance(size, int) or size <= 0:
                    raise DiscoveryError("CIMA search has no positive page size")
                if page * size >= data["totalFilas"]:
                    break
                page += 1
        if not registrations:
            raise DiscoveryError(f"CIMA no medicines found for configured names: {names}")
        for code in sorted(registrations):
            url = API + "medicamento?" + urlencode({"nregistro": code})
            files.append(ArtifactFile(url, f"medicine_{code}.json", role="medicine"))
        return SourceArtifact(self.source_id, files, discovered_from=API, notes=[f"Targeted names: {', '.join(names)}; product coverage only, no prices."])

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("presentations", COLUMNS)]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        import json

        records = {}
        for file in snapshot.files:
            if file.role != "medicine":
                continue
            try:
                medicine = json.loads(file.path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ParserError(f"CIMA invalid JSON: {file.filename}") from exc
            if not isinstance(medicine, dict) or not medicine.get("nregistro") or not isinstance(medicine.get("presentaciones"), list):
                raise SchemaChangedError(f"CIMA medication structure changed: {file.filename}")
            for presentation in medicine["presentaciones"]:
                if not isinstance(presentation, dict) or not str(presentation.get("cn", "")).isdigit() or not presentation.get("nombre"):
                    raise SchemaChangedError(f"CIMA presentation missing national code or name: {file.filename}")
                cn = str(presentation["cn"])
                ingredient = medicine.get("pactivos") or (medicine.get("vtm") or {}).get("nombre")
                row = {**presentation, "cn": cn, "nregistro": medicine["nregistro"], "ingredient": ingredient, "medicine": medicine}
                if cn in records and records[cn].data != row:
                    raise ParserError(f"CIMA conflicting presentations for national code {cn}")
                records[cn] = ParsedRecord(f"{file.filename}:cn={cn}", cn, row, role="presentations")
        if not records:
            raise ParserError("CIMA no national presentations found in downloaded medications")
        rows = list(records.values())
        return ParsedDataset(self.source_id, snapshot, rows, [profile_rows("presentations", COLUMNS, [r.data for r in rows], row_count=len(rows))], total_rows=len(rows))

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord:
        row, medicine = record.data, record.data["medicine"]
        state = row.get("estado") or {}
        marketed = row.get("comerc")
        if not isinstance(marketed, bool):
            raise ParserError(f"CIMA {record.key}: presentation marketing flag must be boolean")
        product = SourceProductData(
            product_code=row["cn"], code_type="CODIGO_NACIONAL", product_level=ProductLevel.PRESENTATION,
            name=row["nombre"].strip(), brand_name=clean_str(medicine.get("nombre")),
            ingredient=clean_str(row.get("ingredient")), strength_text=clean_str(medicine.get("dosis")),
            manufacturer=clean_str(medicine.get("labcomercializador")) or clean_str(medicine.get("labtitular")),
            dosage_form=clean_str((medicine.get("formaFarmaceutica") or {}).get("nombre")),
            route=join_notes(*(item.get("nombre") for item in medicine.get("viasAdministracion", []))),
            legal_status=clean_str(medicine.get("cpresc")), marketing_status="marketed" if marketed else "not_marketed",
            authorisation_number=medicine["nregistro"], authorisation_date=_date(state.get("aut")),
            marketing_end_date=_date(state.get("rev")), orphan=medicine.get("huerfano"),
            detail_url="https://cima.aemps.es/cima/publico/detalle.html?nregistro=" + medicine["nregistro"],
            notes="Targeted official product reconciliation. No price or financing entitlement is inferred; match other Spanish sources only by national code.",
            **parse_fields(strength_text=medicine.get("dosis"), pack_text=row["nombre"]),
        )
        return NormalizedRecord(record.row_ref, product, [])
