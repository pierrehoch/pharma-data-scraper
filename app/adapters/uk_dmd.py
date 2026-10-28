"""UK – NHSBSA dm+d via the TRUD API (item 24, weekly XML release).

Requires TRUD_API_KEY (account + subscription to item 24 + licence acceptance on the TRUD website).
Two record types are produced:
  * VMPP (virtual pack)  → Drug Tariff price (pence → GBP), identifier SNOMED_VMPP (shared with uk_drug_tariff)
  * AMPP (actual pack)   → NHS indicative list price, identifier SNOMED_AMPP
The XML layout follows the dm+d data model (f_vtm2/f_vmp2/f_vmpp2/f_amp2/f_ampp2/f_lookup2/f_ingredient2).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from app.adapters._common import join_notes, pack_price, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import ErrorType, PriceBasis, PriceStatus, ProductLevel, Severity
from app.core.errors import DiscoveryError, ParserError
from app.core.parsing import clean_str, parse_date, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, IngredientComponent, Issue, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.packs import Pack, canonical_pack_unit
from app.observability import get_logger

log = get_logger(__name__)

TRUD_RELEASES = "https://isd.digital.nhs.uk/trud/api/v1/keys/{key}/items/{item}/releases?latest"
FILE_PREFIXES = ("f_vtm", "f_vmp", "f_vmpp", "f_amp", "f_ampp", "f_lookup", "f_ingredient")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_elements(data: bytes, wanted: str) -> Iterator[dict[str, str]]:
    """Stream <wanted> elements as {child_tag: text} dicts (namespaces ignored)."""
    for _, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
        if _local(elem.tag) == wanted:
            yield {_local(c.tag): (c.text or "").strip() for c in elem}
            elem.clear()


class UkDmdAdapter(SourceAdapter):
    adapter_id = "uk_dmd"
    parser_type = "zip/xml"
    parser_version = "1.0"
    limitations = "XML layout implemented from the dm+d data model; live validation requires a TRUD API key."

    def check_configuration(self) -> list[Issue]:
        if not self.ctx.settings.trud_api_key:
            return [
                Issue(
                    ErrorType.MISSING_CONFIGURATION,
                    Severity.ERROR,
                    "TRUD_API_KEY is not set: create a TRUD account, subscribe to item 24 (NHSBSA dm+d), accept its licence, "
                    "then set TRUD_API_KEY in the environment.",
                    stage="discovery",
                )
            ]
        return []

    def discover(self) -> SourceArtifact:
        url = TRUD_RELEASES.format(key=self.ctx.settings.trud_api_key, item=self.option("trud_item", 24))
        try:
            payload = self.ctx.http.get_json(url)
        except Exception as exc:  # noqa: BLE001
            raise DiscoveryError(
                f"TRUD API call failed ({exc}). Check that the key is valid, that you are subscribed to item "
                f"{self.option('trud_item', 24)} and that its licence has been accepted on the TRUD website."
            ) from exc
        releases = payload.get("releases") or []
        if not releases:
            raise DiscoveryError("TRUD API returned no release for dm+d", details={"message": payload.get("message")})
        rel = releases[0]
        published = parse_date(rel.get("releaseDate"), ["%Y-%m-%d"])
        return SourceArtifact(
            self.source_id,
            [ArtifactFile(url=rel["archiveFileUrl"], filename=rel.get("archiveFileName") or "dmd.zip", role="main", published_at=published)],
            published_at=published,
            edition=rel.get("name") or rel.get("id"),
            discovered_from="TRUD API (item releases?latest)",
            notes=[f"TRUD sha256: {rel.get('archiveFileSha256')}"] if rel.get("archiveFileSha256") else [],
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        cols = ["id", "name", "vpid", "qty", "qty_uom", "price_pence", "price_date"]
        return [ExpectedSchema("vmpp", cols), ExpectedSchema("ampp", cols + ["supplier"])]

    @staticmethod
    def _members(path: Path) -> dict[str, bytes]:
        out: dict[str, bytes] = {}

        def visit(zf: zipfile.ZipFile) -> None:
            for name in zf.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                if base.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(zf.read(name))) as inner:
                        visit(inner)
                    continue
                if not base.endswith(".xml"):
                    continue
                m = re.match(r"(f_[a-z]+)\d*_", base)
                if m and m.group(1) in FILE_PREFIXES and m.group(1) not in out:
                    out[m.group(1)] = zf.read(name)

        try:
            with zipfile.ZipFile(path) as zf:
                visit(zf)
        except zipfile.BadZipFile as exc:
            raise ParserError(f"{path.name} is not a valid ZIP archive") from exc
        missing = [p for p in ("f_vmp", "f_vmpp", "f_ampp", "f_lookup") if p not in out]
        if missing:
            raise ParserError(f"dm+d release is missing XML files {missing}", details={"found": sorted(out)})
        return out

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        files = self._members(snapshot.file("main").path)
        log.info("dm+d release: parsing %s", ", ".join(f"{k} ({len(v) / 1e6:.1f} MB)" for k, v in sorted(files.items())))
        lookup: dict[str, dict[str, str]] = defaultdict(dict)
        for section in ("UNIT_OF_MEASURE", "FORM", "ROUTE", "SUPPLIER"):
            for _, elem in ET.iterparse(io.BytesIO(files["f_lookup"]), events=("end",)):
                if _local(elem.tag) == section:
                    for info in elem:
                        vals = {_local(c.tag): (c.text or "").strip() for c in info}
                        if vals.get("CD"):
                            lookup[section][vals["CD"]] = vals.get("DESC", "")
                    elem.clear()
        ingredients = {i["ISID"]: i.get("NM") for i in _iter_elements(files.get("f_ingredient", b"<x/>"), "ING")}
        vtms = {v["VTMID"]: v.get("NM") for v in _iter_elements(files.get("f_vtm", b"<x/>"), "VTM")}
        vmps = {v["VPID"]: v for v in _iter_elements(files["f_vmp"], "VMP")}
        vpi: dict[str, list[dict]] = defaultdict(list)
        for v in _iter_elements(files["f_vmp"], "VPI"):
            vpi[v["VPID"]].append(v)
        forms = {v["VPID"]: lookup["FORM"].get(v.get("FORMCD", ""), "") for v in _iter_elements(files["f_vmp"], "DFORM")}
        routes: dict[str, list[str]] = defaultdict(list)
        for v in _iter_elements(files["f_vmp"], "DROUTE"):
            routes[v["VPID"]].append(lookup["ROUTE"].get(v.get("ROUTECD", ""), ""))
        dtinfo = {v["VPPID"]: v for v in _iter_elements(files["f_vmpp"], "DTINFO")}
        amps = {a["APID"]: a for a in _iter_elements(files.get("f_amp", b"<x/>"), "AMP")}
        prices = {p["APPID"]: p for p in _iter_elements(files["f_ampp"], "PRICE_INFO")}
        log.info("dm+d reference tables loaded: %s VMP, %s DT prices, %s AMPP prices", f"{len(vmps):,}", f"{len(dtinfo):,}", f"{len(prices):,}")

        def vmp_context(vpid: str) -> dict:
            vmp = vmps.get(vpid, {})
            ings = vpi.get(vpid, [])
            first = ings[0] if ings else {}
            num_uom = lookup["UNIT_OF_MEASURE"].get(first.get("STRNT_NMRTR_UOMCD", ""), "")
            den_uom = lookup["UNIT_OF_MEASURE"].get(first.get("STRNT_DNMTR_UOMCD", ""), "")
            strength = None
            if first.get("STRNT_NMRTR_VAL"):
                strength = f"{first['STRNT_NMRTR_VAL']} {num_uom}"
                if first.get("STRNT_DNMTR_VAL"):
                    strength += f"/{first['STRNT_DNMTR_VAL']} {den_uom}"
            return {
                "vmp_name": vmp.get("NM"),
                "vtm": vtms.get(vmp.get("VTMID", "")),
                "ingredients": [ingredients.get(i.get("ISID", ""), "") for i in ings],
                "ingredient_components": [
                    {"name": ingredients.get(i.get("ISID", ""), ""), "namespace": "uk_dmd_ingredient", "identifier": i.get("ISID")}
                    for i in ings
                ],
                "strength": strength,
                "form": forms.get(vpid),
                "routes": routes.get(vpid, []),
            }

        records: list[ParsedRecord] = []
        vmpp_rows, ampp_rows = [], []
        stop = self.hints.early_stop_at
        truncated = False
        for v in _iter_elements(files["f_vmpp"], "VMPP"):
            if stop is not None and len(vmpp_rows) >= stop:
                truncated = True
                break
            dt = dtinfo.get(v["VPPID"], {})
            row = {
                "id": v["VPPID"],
                "name": v.get("NM"),
                "vpid": v.get("VPID"),
                "qty": v.get("QTYVAL"),
                "qty_uom": lookup["UNIT_OF_MEASURE"].get(v.get("QTY_UOMCD", ""), v.get("QTY_UOMCD")),
                "price_pence": dt.get("PRICE"),
                "price_date": dt.get("DT"),
                "pay_category": dt.get("PAY_CATCD"),
                "invalid": v.get("INVALID"),
                **vmp_context(v.get("VPID", "")),
            }
            vmpp_rows.append(row)
            records.append(ParsedRecord(row_ref=f"f_vmpp:{v['VPPID']}", key=v["VPPID"], data=row, role="vmpp"))
        vmpp_by_id = {r["id"]: r for r in vmpp_rows}
        for a in _iter_elements(files["f_ampp"], "AMPP"):
            if stop is not None and len(ampp_rows) >= stop:
                truncated = True
                break
            price = prices.get(a["APPID"], {})
            amp = amps.get(a.get("APID", ""), {})
            parent = vmpp_by_id.get(a.get("VPPID", ""), {})
            row = {
                "id": a["APPID"],
                "name": a.get("NM"),
                "vpid": parent.get("vpid") or amp.get("VPID"),
                "vppid": a.get("VPPID"),
                "qty": parent.get("qty"),
                "qty_uom": parent.get("qty_uom"),
                "price_pence": price.get("PRICE"),
                "price_date": price.get("PRICEDT"),
                "price_basis_code": price.get("PRICE_BASISCD"),
                "supplier": lookup["SUPPLIER"].get(amp.get("SUPPCD", ""), amp.get("SUPPCD")),
                "invalid": a.get("INVALID"),
                **(vmp_context(parent.get("vpid") or amp.get("VPID", "")) if (parent or amp) else {}),
            }
            ampp_rows.append(row)
            records.append(ParsedRecord(row_ref=f"f_ampp:{a['APPID']}", key=a["APPID"], data=row, role="ampp"))
        base_cols = ["id", "name", "vpid", "qty", "qty_uom", "price_pence", "price_date"]
        profiles = [
            profile_rows("vmpp", base_cols, vmpp_rows, row_count=len(vmpp_rows)),
            profile_rows("ampp", base_cols + ["supplier"], ampp_rows, row_count=len(ampp_rows)),
        ]
        return ParsedDataset(self.source_id, snapshot, records, profiles, truncated=truncated, total_rows=len(records))

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        qty = Decimal(d["qty"]) if clean_str(d.get("qty")) else None
        unit = canonical_pack_unit(d.get("qty_uom")) or clean_str(d.get("qty_uom"))
        is_vmpp = record.role == "vmpp"
        product = SourceProductData(
            product_code=d["id"],
            code_type="SNOMED_VMPP" if is_vmpp else "SNOMED_AMPP",
            product_level=ProductLevel.PRESENTATION,
            name=clean_str(d.get("name")),
            ingredient=" + ".join(i for i in d.get("ingredients") or [] if i) or d.get("vtm"),
            strength_text=d.get("strength"),
            dosage_form=d.get("form") or None,
            route=", ".join(r for r in d.get("routes") or [] if r) or None,
            pack_text=clean_str(f"{d.get('qty') or ''} {d.get('qty_uom') or ''}"),
            manufacturer=d.get("supplier"),
            group_code=d.get("vpid"),
            group_name=d.get("vmp_name"),
            reimbursement_category=f"Drug Tariff pay category {d['pay_category']}" if d.get("pay_category") else None,
            marketing_status="invalid" if d.get("invalid") == "1" else None,
            notes=join_notes(
                f"VMPP {d['vppid']}" if d.get("vppid") else None,
                f"VTM {d['vtm']}" if d.get("vtm") else None,
            ),
            **parse_fields(
                strength_text=d.get("strength") or d.get("vmp_name"),
                pack=Pack(qty, unit, f"{d.get('qty')} {d.get('qty_uom')}") if qty and unit else None,
            ),
        )
        pence = Decimal(d["price_pence"]) if clean_str(d.get("price_pence")) else None
        price_date: date | None = parse_date(d.get("price_date"), ["%Y-%m-%d"]) if d.get("price_date") else None
        price = pack_price(
            pence / 100 if pence is not None else None,
            currency="GBP",
            price_type=PriceBasis.REIMBURSEMENT if is_vmpp else PriceBasis.WHOLESALE,
            status=PriceStatus.REGULATED if is_vmpp else PriceStatus.OBSERVED,
            product=product,
            vat_included=False,
            valid_from=price_date,
            edition_date=dataset.snapshot.published_at,
            coverage_scope="region:England+Wales" if is_vmpp else self.config.coverage_scope,
            variant="drug_tariff_basic_price" if is_vmpp else "nhs_indicative_price",
            unit_text="pack (published in pence)",
            notes=f"price basis code {d['price_basis_code']}" if d.get("price_basis_code") else None,
        )
        return NormalizedRecord(
            record.row_ref, product, [price] if price else [],
            ingredient_components=[IngredientComponent(**item) for item in d.get("ingredient_components") or []] or None,
        )
