from decimal import Decimal

import pytest

from app.core.enums import PriceBasis, PriceStatus
from app.core.errors import DiscoveryError, ParserError
from tests.test_adapters import make_adapter, run_adapter


def test_ontario_modern_hierarchy_and_payment_fields(settings, http, registry):
    xml = b"""<extract createDate="2026-09-22">
    <manufacturerList><manufacturer id="SAV">Sanofi</manufacturer></manufacturerList>
    <genericName><name>DUPILUMAB</name><pcg9><strength>300mg/2mL</strength><dosageForm>Inj</dosageForm>
    <drug id="02470365" notABenefit="Y"><name>Dupixent</name><manufacturerId>SAV</manufacturerId>
    <individualPrice>535.1404</individualPrice><amountMOHLTCPays>500.00</amountMOHLTCPays></drug>
    </pcg9></genericName>
    <genericName><name>OTHER INGREDIENT</name><pcg9><strength>10mg</strength><dosageForm>Tab</dosageForm>
    <drug id="00000002"><name>Other</name><manufacturerId>SAV</manufacturerId></drug>
    </pcg9></genericName></extract>"""
    http.override(r"odb-data-extract\.xml$", xml)
    adapter = make_adapter("ca_on_odb", settings, http, registry)
    artifact = adapter.discover()
    dataset = adapter.parse(adapter.download(artifact))
    normalized = adapter.normalize(dataset)
    assert normalized.failed_rows == 0
    first, second = normalized.records
    assert first.product.ingredient == "DUPILUMAB"
    assert second.product.ingredient == "OTHER INGREDIENT"
    assert first.product.manufacturer == "Sanofi"
    assert first.product.reimbursement_category == "Not an ODB benefit"
    assert {(price.price_type, price.price_variant, price.amount) for price in first.prices} == {
        (PriceBasis.EX_FACTORY, "individual_price", Decimal("535.1404")),
        (PriceBasis.REIMBURSEMENT, "amount_moh_pays", Decimal("500.00")),
    }
    assert all(price.valid_from.isoformat() == "2026-09-29" for price in first.prices)
    assert all(price.coverage_scope == "province:ON" for price in first.prices)
    assert not second.prices


def test_ontario_publication_page_requires_effective_date(settings, http, registry):
    http.override(r"ontario\.ca/document/", b'<a href="https://www.ontario.ca/files/odb-data-extract.xml">XML</a>')
    adapter = make_adapter("ca_on_odb", settings, http, registry)
    with pytest.raises(DiscoveryError, match="effective date"):
        adapter.discover()


def test_ontario_override_is_preserved(settings, http, registry):
    settings.ca_on_odb_xml_url = "https://example.test/odb.xml"
    adapter = make_adapter("ca_on_odb", settings, http, registry)
    assert adapter.discover().files[0].url == settings.ca_on_odb_xml_url
    assert not http.requested


def test_bc_active_prices_zero_placeholders_and_plans(run_adapter):
    _, _, _, dataset, normalized, report = run_adapter("ca_bc_pharmacare")
    assert normalized.failed_rows == 0
    assert not report.errors
    assert dataset.context["edition_date"].isoformat() == "2026-10-06"
    assert dataset.context["raw_rows"] == 7
    assert len(normalized.records) == 3
    dupixent = next(item for item in normalized.records if item.product.product_code == "02470365")
    assert len(dupixent.prices) == 1
    price = dupixent.prices[0]
    assert price.amount == Decimal("535.1404")
    assert price.price_unit == "unit" and price.pack_quantity is None
    assert price.price_type == PriceBasis.REIMBURSEMENT and price.price_status == PriceStatus.CEILING
    assert price.coverage_scope == "province:BC"
    assert price.valid_from.isoformat() == "2026-04-01"
    assert price.valid_to is None
    assert "SA" in price.notes
    erythrocin = next(item for item in normalized.records if item.product.product_code == "00000175")
    assert len(erythrocin.prices) == 1
    assert erythrocin.prices[0].price_variant == "max_price:plans:B,C"
    assert erythrocin.prices[0].valid_to.isoformat() == "2026-12-31"
    unpriced = next(item for item in normalized.records if item.product.product_code == "00000176")
    assert not unpriced.prices


def test_bc_rejects_invalid_price(settings, http, registry):
    import io
    import zipfile

    from tests.conftest import fx

    content = fx("ca_bc_pharmacare", "pddf_2026_10_06.csv").read_text().replace("0535.1404", "invalid")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("pddf_2026_10_06.csv", content)
    http.override(r"pddf\.zip$", output.getvalue())
    adapter = make_adapter("ca_bc_pharmacare", settings, http, registry)
    with pytest.raises(ParserError, match="invalid max_price"):
        adapter.parse(adapter.download(adapter.discover()))
