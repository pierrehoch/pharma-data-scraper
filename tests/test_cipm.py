from datetime import date
from decimal import Decimal

import pytest

from app.adapters.es_cipm import PAGE, _between, _decision, _headers, _ingredient, _meeting, _price
from app.core.enums import PriceBasis, PriceStatus
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError
from app.core.pipeline import RunOptions, Selection
from tests.conftest import fx
from tests.test_adapters import make_adapter, run_adapter


@pytest.mark.parametrize("text,expected", [
    ("1.735", "1735"), ("839,87", "839.87"), ("6.986,3", "6986.3"),
    ("14000.00", "14000"), ("2536.10", "2536.10"), ("135.600", "135600"), ("498", "498"),
])
def test_cipm_price_separators(text, expected):
    assert _price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "confidencial", "0", "-1", "1.2.3", "10-20", "1 735"])
def test_cipm_invalid_prices_are_not_fabricated(text):
    with pytest.raises(ParserError):
        _price(text)


def test_cipm_meeting_date_is_not_effective_date():
    assert _meeting("Sesión 273 de 2 de septiembre de 2026") == ("273", date(2026, 9, 2))
    with pytest.raises(SchemaChangedError):
        _meeting("CIPM 273, publication 2026")


def test_cipm_ingredient_code_is_not_reported_as_ingredient():
    assert _ingredient("S01AX24 - Polihexanida") == ("Polihexanida", None)
    ingredient, note = _ingredient("B05ZB91 – Hemofiltrados")
    assert ingredient is None
    assert note == "CIPM active-ingredient classification: B05ZB91 – Hemofiltrados"


def test_cipm_all_linked_pdfs_are_discovered_and_deduplicated(run_adapter):
    _, artifact, snapshot, dataset, normalized, report = run_adapter("es_cipm")
    assert len(artifact.files) == len(snapshot.files) == 4
    assert artifact.published_at is None
    assert normalized.failed_rows == 0 and not report.errors
    assert dataset.context["documents_parsed"] >= 1
    assert len(dataset.context["documents_excluded"]) >= 2
    assert any("177" in row["filename"] for row in dataset.context["documents_excluded"])
    assert any("Octubre_2026" in row["filename"] for row in dataset.context["documents_excluded"])
    assert len({r.product.product_code for r in normalized.records}) == len(normalized.records)
    kesimpta = next(r for r in normalized.records if r.product.group_code == "730797")
    assert "KESIMPTA" in kesimpta.product.name
    assert kesimpta.product.code_type == "CIPM_DISCLOSURE"
    assert kesimpta.product.group_name == "CODIGO_NACIONAL"
    assert "Ofatumumab" in kesimpta.product.ingredient
    price = kesimpta.prices[0]
    assert price.amount == Decimal("1735")
    assert price.currency == "EUR" and price.price_unit == "pack"
    assert price.price_type == PriceBasis.EX_FACTORY
    assert price.price_status == PriceStatus.REFERENCE
    assert price.price_variant == "cipm_273:proposed_industrial"
    assert price.published_at == date(2026, 9, 2) and price.date_basis == "meeting_date"
    assert price.valid_from is None and price.valid_to is None
    assert "not definitive or effective" in price.notes
    assert price.vat_included is None
    velsipity = next(r for r in normalized.records if r.product.group_code == "764422")
    assert velsipity.prices[0].amount == Decimal("839.87")
    assert not any(r.data["decision"] == "denied" for r in dataset.records)
    assert dataset.context["rows_excluded"], "unsupported rows must not discard the whole CIPM 250 document"


def test_cipm_discovery_layout_change_is_explicit(settings, http, registry):
    http.override(r"acuerdosNotasInformativas/home\.htm$", b"<html>No archive</html>")
    with pytest.raises(DiscoveryError, match="no official linked PDF"):
        make_adapter("es_cipm", settings, http, registry).discover()


def test_cipm_no_price_document_fails_when_alone(settings, http, registry):
    http.override(r"acuerdosNotasInformativas/home\.htm$",
                  b'<a href="docs/ACUERDOS_DE_LA_CIPM_177_web.pdf">Archive</a>')
    adapter = make_adapter("es_cipm", settings, http, registry)
    with pytest.raises(ParserError, match="no supported explicit"):
        adapter.parse(adapter.download(adapter.discover()))


def test_cipm_official_urls_only(settings, http, registry):
    http.override(r"acuerdosNotasInformativas/home\.htm$", (
        '<a href="https://unrelated.example/prices.pdf">Untrusted</a>'
        '<a href="docs/Acuerdos_CIPM_273.pdf">Official</a>'
    ).encode())
    artifact = make_adapter("es_cipm", settings, http, registry).discover()
    assert len(artifact.files) == 1
    assert artifact.files[0].url.startswith(PAGE.rsplit("/", 1)[0])


def test_cipm_repeat_ingestion_is_idempotent(pipeline, session_factory):
    from sqlalchemy import func, select
    from app.db.models import Price

    selection = Selection(source_ids=["es_cipm"])
    first = pipeline.run(selection, RunOptions())
    second = pipeline.run(selection, RunOptions(force_download=True))
    assert first.jobs[0].metrics["prices_published"] > 0
    assert second.jobs[0].metrics["prices_published"] == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Price).where(Price.source_id == "es_cipm")) == first.jobs[0].metrics["prices_published"]


@pytest.fixture
def parse_pdf(settings, http, registry):
    def parse(filename):
        http.override(r"acuerdosNotasInformativas/home\.htm$", b'<a href="docs/regression.pdf">Agreement</a>')
        http.override(r"/regression\.pdf$", fx("es_cipm", filename))
        adapter = make_adapter("es_cipm", settings, http, registry)
        dataset = adapter.parse(adapter.download(adapter.discover()))
        normalized = adapter.normalize(dataset)
        assert normalized.failed_rows == 0
        return dataset, normalized.records
    return parse


def test_cipm_real_geometry_inherits_only_spanning_cells(parse_pdf):
    _, records = parse_pdf("Acuerdos_CIPM_272.pdf")
    doses = {r.product.group_code: r for r in records if r.product.group_code in {"770033", "770595"}}
    assert set(doses) == {"770033", "770595"}
    assert doses["770033"].prices[0].amount == Decimal("9653.95")
    assert doses["770595"].prices[0].amount == Decimal("6582.24")
    assert doses["770033"].product.manufacturer == doses["770595"].product.manufacturer == "JANSSEN CILAG SA"
    assert doses["770033"].product.name != doses["770595"].product.name
    assert doses["770033"].product.pack_text != doses["770595"].product.pack_text
    assert "Amivantamab" in doses["770595"].product.ingredient


def test_cipm_old_and_proposed_price_columns_use_new_not_old(parse_pdf):
    _, records = parse_pdf("ACUERDOS_CIPM_235.pdf")
    dolotren = next(r for r in records if r.product.group_code == "654658")
    assert dolotren.prices[0].amount == Decimal("1.66"), "previous 1.19 EUR must not become the proposed price"
    tavneos = next(r for r in records if r.product.group_code == "733268")
    assert tavneos.prices[0].amount == Decimal("6986.3")


def test_cipm_legacy_table_has_explicit_ingredient_and_price(parse_pdf):
    _, records = parse_pdf("NOTA_INFORMATIVA_DE_LA_CIPM_187_web.pdf")
    rydapt = next(r for r in records if r.product.group_code == "720071")
    assert rydapt.product.ingredient == "Midostaurina"
    assert rydapt.prices[0].amount == Decimal("6673.77")


def test_cipm_dupixent_four_presentations_are_proposed_not_effective(parse_pdf):
    _, records = parse_pdf("ACUERDOS_CIPM_245.pdf")
    matches = [r for r in records if "DUPIXENT" in r.product.name.upper()]
    assert {r.product.group_code for r in matches} == {"758027", "727309", "758028", "718735"}
    assert len(matches) == 4
    for record in matches:
        assert record.prices[0].amount == Decimal("1211.54")
        assert record.prices[0].published_at == date(2024, 3, 20)
        assert record.prices[0].valid_from is None
        assert record.prices[0].price_status == PriceStatus.REFERENCE


def test_cipm_section_state_ignores_contents_and_survives_pages():
    assert _decision("2) Acuerdos denegatorios ....... 30", "favourable_proposal") == "favourable_proposal"
    assert _decision("2) Acuerdos denegatorios de financiación", "favourable_proposal") == "denied"
    assert _decision("Continuation page", "denied") == "denied"
    assert _decision("1) Acuerdos de Precio y Financiación", "denied") == "favourable_proposal"


def test_cipm_ambiguous_future_price_schedules_are_explicitly_excluded():
    with pytest.raises(SchemaChangedError, match="ambiguous price columns"):
        _headers([["LABORATORIO", "MEDICAMENTO", "FORMATO", "CN", "Precio €",
                   "Precio € A partir 01/01/26", "Precio € A partir 01/01/27"]])


def test_cipm_adjacent_tables_have_no_intervening_text():
    assert _between(None, 300, 300) == ""
