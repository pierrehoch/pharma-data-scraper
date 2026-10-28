"""Adapter registry. Adding a source = add a module here + an entry in app/registry/sources.yaml."""

from __future__ import annotations

import importlib

from app.core.adapter import SourceAdapter

_ADAPTERS: dict[str, str] = {
    "us_nadac": "app.adapters.us_nadac:UsNadacAdapter",
    "us_asp": "app.adapters.us_asp:UsAspAdapter",
    "fr_bdpm": "app.adapters.fr_bdpm:FrBdpmAdapter",
    "de_festbetrag": "app.adapters.de_festbetrag:DeFestbetragAdapter",
    "de_amnog": "app.adapters.de_amnog:DeAmnogAdapter",
    "de_gba": "app.adapters.de_gba:DeGbaAdapter",
    "it_aifa": "app.adapters.it_aifa:ItAifaAdapter",
    "es_nomenclator": "app.adapters.es_nomenclator:EsNomenclatorAdapter",
    "es_cima": "app.adapters.es_cima:EsCimaAdapter",
    "es_cipm": "app.adapters.es_cipm:EsCipmAdapter",
    "uk_dmd": "app.adapters.uk_dmd:UkDmdAdapter",
    "uk_drug_tariff": "app.adapters.uk_drug_tariff:UkDrugTariffAdapter",
    "jp_nhi": "app.adapters.jp_nhi:JpNhiAdapter",
    "br_cmed": "app.adapters.br_cmed:BrCmedAdapter",
    "ca_on_odb": "app.adapters.ca_on_odb:CaOnOdbAdapter",
    "ca_bc_pharmacare": "app.adapters.ca_bc_pharmacare:CaBcPharmacareAdapter",
    "ca_hc_dpd": "app.adapters.ca_hc_dpd:CaHcDpdAdapter",
    "cn_nhsa": "app.adapters.cn_nhsa:CnNhsaAdapter",
    "cn_vbp": "app.adapters.cn_vbp:CnVbpAdapter",
    "cn_gdmede": "app.adapters.cn_gdmede:CnGdmedeAdapter",
    "cn_hubei": "app.adapters.cn_hubei:CnHubeiAdapter",
}


def get_adapter_class(name: str) -> type[SourceAdapter]:
    try:
        target = _ADAPTERS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown adapter '{name}'. Available: {sorted(_ADAPTERS)}") from exc
    module_name, cls_name = target.split(":")
    return getattr(importlib.import_module(module_name), cls_name)


def adapter_ids() -> list[str]:
    return list(_ADAPTERS)
