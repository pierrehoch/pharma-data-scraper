from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace

import pdfplumber
import pytest

from app.adapters.de_festbetrag import LINE_RE
from app.adapters.de_gba import _pack_rows, _reference_date
from app.core.enums import PriceBasis, PriceStatus
from app.core.errors import DiscoveryError, SchemaChangedError
from tests.conftest import fx
from tests.test_adapters import make_adapter, run_adapter


def test_gba_explicit_pack_prices_and_clinic_continuation(run_adapter):
    _, artifact, _, dataset, normalized, report = run_adapter("de_gba")
    assert len(artifact.files) == 2
    assert all("_EN" not in f.filename for f in artifact.files)
    assert normalized.failed_rows == 0
    assert not report.errors
    dupilumab = [r for r in normalized.records if r.product.ingredient == "Dupilumab"]
    assert len(dupilumab) == 1
    product = dupilumab[0].product
    assert product.code_type == "GBA_DISCLOSURE"
    assert product.brand_name is None and product.manufacturer is None
    assert product.pack_quantity == Decimal(6) and product.pack_unit == "ILO"
    gross, net = dupilumab[0].prices
    assert gross.amount == Decimal("3908.39")
    assert net.amount == Decimal("3686.47")
    assert gross.price_type == PriceBasis.RETAIL
    assert net.price_type == PriceBasis.RETAIL and net.statutory_reduction_included is True
    assert gross.price_status == PriceStatus.REFERENCE
    assert gross.valid_from == date(2025, 1, 1)
    assert gross.date_basis == "reference_date" and gross.published_at == date(2025, 2, 6)
    assert "not evidence of validity" in gross.notes
    clinic = next(r for r in normalized.records if r.product.ingredient == "Lisocabtagen maraleucel")
    assert clinic.product.pack_text == "1 Einzelinfusionsbeutel"
    assert clinic.prices[0].amount == Decimal("227500.00")
    assert clinic.prices[0].price_type == PriceBasis.WHOLESALE
    # These three lines continue onto the following page without another header.
    assert any(r.product.ingredient == "Zanubrutinib" and r.prices[0].amount == Decimal("5479.32") for r in normalized.records)
    assert any(r.product.ingredient == "Rituximab" and r.product.strength_text == "100 mg" for r in normalized.records)
    assert dataset.total_rows == len(normalized.records)


def test_gba_excludes_annual_costs_and_requires_reference_date():
    with pdfplumber.open(fx("de_gba", "2025-02-06_AM-RL-XII_Dupilumab_D-1086_TrG.pdf")) as pdf:
        assert len(_pack_rows(pdf)) == 23
        # Annual treatment-cost tables have no pack-price header.
        assert _pack_rows(SimpleNamespace(pages=pdf.pages[:24])) == []
    with pytest.raises(SchemaChangedError, match="no explicit"):
        _reference_date("Annual therapy cost 3908.39 EUR; decision 6 February 2025")
    with pytest.raises(SchemaChangedError, match="conflicting"):
        _reference_date("Stand Lauer-Taxe: 1. Januar 2025\nStand Lauer-Taxe: 1. Februar 2025")


@pytest.mark.parametrize("suffix", ["TrG_EN.pdf", "EN_TrG.pdf"])
def test_gba_english_fallback_preserves_currency_and_wrapped_pack(settings, http, registry, suffix):
    name = f"2025-10-02_AM-RL-XII_Lisocabtagene-maraleucel_D-1196_{suffix}"
    http.override(r"g-ba\.de/bewertungsverfahren/nutzenbewertung/1200/$",
                  f'<a href="/downloads/40-1465-11937/{name}">Justification</a>'.encode())
    http.override(r"(?:_TrG_EN|_EN_TrG)\.pdf$", fx("de_gba", "2025-10-02_AM-RL-XII_Lisocabtagene-maraleucel_D-1196_TrG_EN.pdf"))
    adapter = make_adapter("de_gba", settings, http, registry)
    adapter.config.options["procedure_urls"] = ["https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/1200/"]
    artifact = adapter.discover()
    assert len(artifact.files) == 1 and artifact.files[0].filename == name
    normalized = adapter.normalize(adapter.parse(adapter.download(artifact)))
    assert len(normalized.records) == 13 and normalized.failed_rows == 0
    clinic = next(r for r in normalized.records if r.product.ingredient == "Lisocabtagene maraleucel")
    assert clinic.product.pack_text == "1 single infusion bag"
    assert clinic.prices[0].amount == Decimal("227500.00")
    retail = next(r for r in normalized.records if r.product.ingredient == "Mosunetuzumab" and r.product.strength_text == "30 mg")
    assert [p.amount for p in retail.prices] == [Decimal("7751.61"), Decimal("7310.44")]


@pytest.mark.parametrize("line,prices", [
    ("Nilotinib 150 0,2 392 KAPS 16.617,67 5.125,9711.491,70 TASIGNA 150MG 10918437", ("16.617,67", "5.125,97", "11.491,70")),
    ("Nilotinib 200 0,3 392 KAPS 23.858,19 6.874,3816.983,81 TASIGNA 200MG 10918472", ("23.858,19", "6.874,38", "16.983,81")),
    ("360 360 300 FTBL 10.931,30 372,3710.558,93 EXJADE 360MG FTA 12868608", ("10.931,30", "372,37", "10.558,93")),
    ("400 400 90 FTBL 10.435,29 348,9110.086,38 GLIVEC 400MG FILMTABLETTEN 01755203", ("10.435,29", "348,91", "10.086,38")),
    ("25 25 21 KAPS 10.202,06 79,8510.122,21 REVLIMID 25MG HARTKAPSELN 01875284", ("10.202,06", "79,85", "10.122,21")),
    ("Nilotinib 150 0,2 392 KAPS 16.617,67 5.125,97 11.491,70 TASIGNA 150MG 10918437", ("16.617,67", "5.125,97", "11.491,70")),
])
def test_festbetrag_adjacent_price_columns(line, prices):
    match = LINE_RE.fullmatch(line)
    assert match
    assert tuple(match.group(field) for field in ("preis", "festbetrag", "diff")) == prices


def test_procurement_all_pages_exact_prices_and_proposed_semantics(run_adapter):
    _, _, _, dataset, normalized, report = run_adapter("cn_vbp")
    assert dataset.total_rows == 385
    assert len(normalized.records) == 385 and normalized.failed_rows == 0
    assert not report.errors
    first = normalized.records[0]
    assert first.product.ingredient == "阿普米司特片"
    assert first.prices[0].amount == Decimal("1.57")
    assert first.prices[0].price_unit == "box"
    assert any(r.prices[0].price_unit == "bottle_or_box" for r in normalized.records)
    for record in normalized.records:
        price = record.prices[0]
        assert record.product.code_type == "VBP_DISCLOSURE"
        assert price.currency == "CNY" and price.price_type == PriceBasis.PUBLIC_PROCUREMENT
        assert price.price_status == PriceStatus.REFERENCE and price.price_variant == "GY-YD2024-2:proposed"
        assert price.valid_from is None and price.published_at is None
        assert price.pack_quantity is None
        assert "PROPOSED" in price.notes


@pytest.mark.parametrize("filename", ["non-price-2025-a.pdf", "non-price-2025-b.pdf"])
def test_procurement_rejects_supplier_only_documents(settings, http, registry, filename):
    http.override(r"nhsa\.gov\.cn/module/download/downfile\.jsp", fx("cn_vbp", filename))
    adapter = make_adapter("cn_vbp", settings, http, registry)
    with pytest.raises(SchemaChangedError):
        adapter.parse(adapter.download(adapter.discover()))


def test_procurement_rejects_contradictory_status(settings, http, registry):
    adapter = make_adapter("cn_vbp", settings, http, registry)
    adapter.config.options["documents"][0]["status"] = "final"
    with pytest.raises(SchemaChangedError, match="status contradicts"):
        adapter.parse(adapter.download(adapter.discover()))


def test_cima_no_fabricated_prices_and_deduplicates_searches(run_adapter):
    _, artifact, _, _, normalized, report = run_adapter("es_cima")
    assert len(artifact.files) == 6  # five search snapshots, one shared medication
    assert len(normalized.records) == 1 and normalized.failed_rows == 0
    assert not report.errors
    record = normalized.records[0]
    assert record.product.product_code == "727309"
    assert record.product.code_type == "CODIGO_NACIONAL"
    assert record.product.ingredient == "DUPILUMAB"
    assert record.product.authorisation_number == "1171229010"
    assert record.product.marketing_status == "marketed"
    assert record.product.authorisation_date == date(2019, 12, 10)
    assert not record.prices


def test_cima_pagination_and_missing_cn(settings, http, registry):
    first = {"totalFilas": 2, "pagina": 1, "tamanioPagina": 1, "resultados": [{"nregistro": "1171229010"}]}
    second = {"totalFilas": 2, "pagina": 2, "tamanioPagina": 1, "resultados": [{"nregistro": "1171229011"}]}
    http.override(r"medicamentos\?.*pagina=1$", json.dumps(first).encode())
    http.override(r"medicamentos\?.*pagina=2$", json.dumps(second).encode())
    adapter = make_adapter("es_cima", settings, http, registry)
    adapter.config.options["medicine_names"] = ["dupixent"]
    artifact = adapter.discover()
    assert len([f for f in artifact.files if f.role == "search"]) == 2
    assert len([f for f in artifact.files if f.role == "medicine"]) == 2
    bad = json.loads(fx("es_cima", "medicine.json").read_text())
    del bad["presentaciones"][0]["cn"]
    http.override(r"medicamento\?", json.dumps(bad).encode())
    with pytest.raises(SchemaChangedError, match="national code"):
        adapter.parse(adapter.download(artifact))


def test_cima_empty_search_fails_explicitly(settings, http, registry):
    http.override(r"medicamentos\?", b'{"totalFilas":0,"tamanioPagina":200,"resultados":[]}')
    adapter = make_adapter("es_cima", settings, http, registry)
    with pytest.raises(DiscoveryError, match="no medicines"):
        adapter.discover()
