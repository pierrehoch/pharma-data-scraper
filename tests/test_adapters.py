"""Per-source tests: discovery → download → snapshot → parse → schema → normalize → validate (offline fixtures)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.adapters import get_adapter_class
from app.core.adapter import AdapterContext
from app.core.enums import ErrorType, PriceBasis, ProductLevel
from app.core.schema import check_expected
from app.core.snapshots import SnapshotStore, sha256_file
from app.core.types import RunHints, TableProfile

PRICED_SOURCES = ["us_nadac", "us_asp", "fr_bdpm", "de_festbetrag", "it_aifa", "es_nomenclator", "uk_dmd", "uk_drug_tariff", "jp_nhi", "br_cmed", "ca_on_odb", "ca_bc_pharmacare", "de_gba", "cn_vbp", "es_cipm"]
ALL_AUTOMATED = PRICED_SOURCES + ["de_amnog", "ca_hc_dpd", "es_cima"]

# One known value per source, checked against the real excerpt in tests/fixtures.
EXPECTED = {
    "fr_bdpm": ("3400949497294", PriceBasis.RETAIL, "incl_dispensing_fee", Decimal("25.36"), "EUR"),
    "us_nadac": ("81298578503", PriceBasis.WHOLESALE, "nadac", Decimal("6.38317"), "USD"),
    "us_asp": ("90371", PriceBasis.REIMBURSEMENT, "asp_payment_limit", Decimal("138.229"), "USD"),
    "it_aifa": ("050465018", PriceBasis.RETAIL, "default", Decimal("330.55"), "EUR"),
    "es_nomenclator": ("650004", PriceBasis.RETAIL, "default", Decimal("2.5"), "EUR"),
    "uk_drug_tariff": ("994511000001109", PriceBasis.REIMBURSEMENT, "drug_tariff_basic_price", Decimal("22.04"), "GBP"),
    "uk_dmd": ("1064711000001107", PriceBasis.REIMBURSEMENT, "drug_tariff_basic_price", Decimal("1.13"), "GBP"),
    "jp_nhi": ("1124001F1022", PriceBasis.REIMBURSEMENT, "default", Decimal("6.3"), "JPY"),
    "br_cmed": ("505107701157215", PriceBasis.EX_FACTORY, "pf_sem_impostos", Decimal("2098.20"), "BRL"),
    "de_festbetrag": ("11289015", PriceBasis.RETAIL, "apothekenverkaufspreis", Decimal("16.56"), "EUR"),
    "ca_on_odb": ("02230711", PriceBasis.EX_FACTORY, "individual_price", Decimal("0.42"), "CAD"),
    "ca_bc_pharmacare": ("02470365", PriceBasis.REIMBURSEMENT, "max_price:plans:SA", Decimal("535.1404"), "CAD"),
}

# Generic fields each source must fill on at least one record of its fixture.
FILLED_FIELDS = {
    "us_nadac": ["name", "ingredient", "strength_value"],
    "us_asp": ["name", "strength_text", "reimbursement_category"],
    "fr_bdpm": ["name", "brand_name", "ingredient", "strength_value", "dosage_form", "route", "pack_quantity", "manufacturer", "group_code",
                "authorisation_status", "marketing_status", "authorisation_date", "marketing_start_date", "reimbursement_rate", "reimbursement_conditions"],
    "it_aifa": ["name", "brand_name", "ingredient", "manufacturer", "group_code", "reimbursement_category"],
    "es_nomenclator": ["name", "ingredient", "manufacturer", "product_type", "marketing_status", "marketing_start_date", "reimbursement_category", "restrictions", "orphan"],
    "uk_dmd": ["name", "ingredient", "group_code", "pack_quantity"],
    "uk_drug_tariff": ["name", "group_code", "reimbursement_category", "pack_quantity"],
    "jp_nhi": ["name", "ingredient", "manufacturer", "route", "therapeutic_class", "strength_value"],
    "br_cmed": ["name", "brand_name", "ingredient", "manufacturer", "authorisation_number", "ean", "therapeutic_class"],
    "de_festbetrag": ["name", "brand_name", "group_name", "dosage_form", "pack_quantity"],
    "de_amnog": ["name", "ingredient", "manufacturer", "marketing_status", "detail_url"],
    "ca_on_odb": ["name", "ingredient", "strength_text"],
    "ca_bc_pharmacare": ["name", "ingredient", "dosage_form", "reimbursement_conditions"],
    "ca_hc_dpd": ["name", "ingredient", "atc_code", "marketing_status", "dosage_form", "route"],
}


def make_adapter(source_id, settings, http, registry, hints=None):  # noqa: ANN001, ANN201
    cfg = registry[source_id]
    ctx = AdapterContext(settings=settings, http=http, store=SnapshotStore(settings.raw_storage_dir), hints=hints or RunHints())
    return get_adapter_class(cfg.adapter)(cfg, ctx)


@pytest.fixture
def run_adapter(settings, http, registry):  # noqa: ANN001, ANN201
    def _run(source_id, hints=None):  # noqa: ANN001, ANN202
        adapter = make_adapter(source_id, settings, http, registry, hints)
        artifact = adapter.discover()
        snapshot = adapter.download(artifact)
        dataset = adapter.parse(snapshot)
        normalized = adapter.normalize(dataset)
        report = adapter.validate(normalized)
        return adapter, artifact, snapshot, dataset, normalized, report

    return _run


@pytest.mark.parametrize("source_id", ALL_AUTOMATED)
def test_parsing_and_normalization(source_id, run_adapter):  # noqa: ANN001
    adapter, artifact, snapshot, dataset, normalized, report = run_adapter(source_id)
    assert artifact.files, "discovery must return exact file URLs"
    assert dataset.records, "parser returned no record"
    assert dataset.profiles, "parser must profile each file for schema detection"
    assert normalized.failed_rows == 0, [i.message for i in normalized.issues]
    assert normalized.records
    for rec in normalized.records:
        assert rec.product.product_code
        assert rec.product.code_type
    if source_id in PRICED_SOURCES:
        assert any(r.prices for r in normalized.records)
    else:
        assert not any(r.prices for r in normalized.records), "reference sources must not invent prices"
    assert not [i for i in report.issues if i.severity == "error"]


@pytest.mark.parametrize("source_id", list(EXPECTED))
def test_known_price_values(source_id, run_adapter):  # noqa: ANN001
    ident, basis, variant, amount, currency = EXPECTED[source_id]
    *_, normalized, _ = run_adapter(source_id)
    rec = next(r for r in normalized.records if r.product.product_code == ident)
    fact = next(p for p in rec.prices if p.price_type == basis and p.price_variant == variant)
    assert fact.amount == amount
    assert fact.currency == currency


@pytest.mark.parametrize("source_id", list(FILLED_FIELDS))
def test_generic_fields_are_filled(source_id, run_adapter):  # noqa: ANN001
    *_, normalized, _ = run_adapter(source_id)
    missing = [f for f in FILLED_FIELDS[source_id] if all(getattr(r.product, f) in (None, "") for r in normalized.records)]
    assert not missing, f"{source_id}: fields never filled: {missing}"


@pytest.mark.parametrize("source_id", ALL_AUTOMATED)
def test_raw_snapshot_is_preserved(source_id, run_adapter):  # noqa: ANN001
    _, _, snapshot, *_ = run_adapter(source_id)
    meta = json.loads((snapshot.directory / "metadata.json").read_text())
    for key in ("source_id", "source_url", "retrieved_at", "published_at", "filename", "content_type", "sha256", "parser_version"):
        assert key in meta
    for f in snapshot.files:
        assert sha256_file(f.path) == f.sha256
    assert meta["sha256"] == snapshot.primary.sha256


@pytest.mark.parametrize("source_id", [s for s in ALL_AUTOMATED])
def test_schema_change_detection(source_id, run_adapter):  # noqa: ANN001
    adapter, _, _, dataset, _, _ = run_adapter(source_id)
    expected = {e.role: e for e in adapter.expected_schemas()}
    profile = next(p for p in dataset.profiles if p.role in expected)
    exp = expected[profile.role]
    # The real fixture matches the declared schema...
    assert check_expected(profile, exp, truncated=False) == []
    # ...and a removed column is detected.
    removed = exp.required_columns[0]
    changed = TableProfile(profile.role, [c for c in profile.columns if c != removed], profile.column_types, profile.row_count)
    issues = check_expected(changed, exp, truncated=False)
    assert issues and all(i.error_type == ErrorType.SCHEMA_CHANGED for i in issues)


def test_bdpm_encodings_are_detected_per_file(run_adapter):
    _, _, _, dataset, normalized, _ = run_adapter("fr_bdpm")
    enc = dataset.context["encodings"]
    assert enc["cip"] == "utf-8" and enc["compo"] == "cp1252"
    rec = next(r for r in normalized.records if r.product.product_code == "3400949497294")
    p = rec.product
    assert p.ingredient.lower() == "anastrozole" and p.brand_name == "ANASTROZOLE ACCORD"
    assert p.strength_value == Decimal("1") and p.strength_unit == "mg"
    assert p.pack_quantity == Decimal(30) and p.pack_unit == "tablet"
    assert p.pack_text.startswith("plaquette(s)")  # source text kept
    assert p.group_code == "60002283" and p.reimbursement_rate == "100%"
    assert p.authorisation_date.isoformat() == "2010-10-28" and p.marketing_start_date.isoformat() == "2011-03-16"
    incl = next(f for f in rec.prices if f.price_variant == "incl_dispensing_fee")
    assert incl.dispensing_fee == Decimal("1.02") and incl.date_basis == "edition_date"


def test_aifa_dynamic_discovery_picks_latest_edition(run_adapter):
    _, artifact, *_ = run_adapter("it_aifa")
    assert artifact.published_at.isoformat() == "2026-05-31"
    assert all("31-05-2026" in f.url for f in artifact.files)
    assert {f.role for f in artifact.files} == {"class_a", "class_h"}


def test_aifa_class_h_has_ex_factory_and_statutory_flag(run_adapter):
    *_, normalized, _ = run_adapter("it_aifa")
    h = next(r for r in normalized.records if r.product.reimbursement_category == "Classe H")
    bases = {p.price_type for p in h.prices}
    assert PriceBasis.EX_FACTORY in bases and PriceBasis.RETAIL in bases
    public = next(p for p in h.prices if p.price_type == PriceBasis.RETAIL)
    assert public.statutory_reduction_included is True


def test_nadac_latest_as_of_only_and_units(settings, http, registry):
    adapter = make_adapter("us_nadac", settings, http, registry)
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert {r.data["as_of_date"] for r in dataset.records} == {"09/30/2026"}
    normalized = adapter.normalize(dataset)
    rec = next(r for r in normalized.records if r.product.product_code == "00093505698")
    assert rec.prices[0].amount == Decimal("0.02110") and rec.prices[0].price_unit == "unit"


def test_nadac_limited_run_uses_api_sample(settings, http, registry):
    adapter = make_adapter("us_nadac", settings, http, registry, RunHints(limit=5))
    artifact = adapter.discover()
    assert artifact.partial and "datastore/query" in artifact.files[0].url and "sorts%5B0%5D" in artifact.files[0].url


def test_asp_is_billing_code_not_product(run_adapter):
    _, artifact, _, dataset, normalized, _ = run_adapter("us_asp")
    assert {f.role for f in artifact.files} == {"main", "crosswalk"}
    assert dataset.context["period"][0].isoformat() == "2026-10-01"
    assert all(r.product.product_level == ProductLevel.BILLING_CODE for r in normalized.records)
    fact = normalized.records[0].prices[0]
    assert fact.valid_to.isoformat() == "2026-12-31" and fact.unit_text == "1 ML"


def test_japan_prices_per_official_unit(run_adapter):
    *_, normalized, _ = run_adapter("jp_nhi")
    rec = next(r for r in normalized.records if r.product.product_code == "1124001F1022")
    assert rec.product.ingredient == "エスタゾラム"
    fact = rec.prices[0]
    assert fact.price_unit == "tablet" and fact.unit_text == "1錠"
    assert fact.valid_from.isoformat() == "2026-08-13"


def test_japan_discovers_latest_year_and_independent_category_editions(settings, http, registry):
    from datetime import date
    from app.adapters.jp_nhi import INDEX

    http.override(r"mhlw\.go\.jp/stf/seisakunitsuite/bunya/0000078916\.html$", (
        '<a href="/topics/2024/04/tp20240401-01.html">Historical list</a>'
        '<a href="/topics/2026/04/tp20260401-01.html">Current list</a>'
    ).encode())
    http.override(r"topics/2026/04/tp20260401-01\.html$", (
        '<a href="xls/tp20261014-01_03.xlsx">Topical</a>'
        '<a href="xls/tp20260813-01_01.xlsx">Previous oral</a>'
        '<a href="xls/tp20261014-01_01.xlsx">Oral</a>'
        '<a href="xls/tp20261014-01_02.xlsx">Injection</a>'
        '<a href="xls/tp20260401-01_04.xlsx">Dental</a>'
    ).encode())
    artifact = make_adapter("jp_nhi", settings, http, registry).discover()
    assert http.requested == [INDEX, "https://www.mhlw.go.jp/topics/2026/04/tp20260401-01.html"]
    assert artifact.published_at == date(2026, 10, 14)
    assert {f.role: f.filename for f in artifact.files} == {
        "oral": "tp20261014-01_01.xlsx",
        "injection": "tp20261014-01_02.xlsx",
        "topical": "tp20261014-01_03.xlsx",
        "dental": "tp20260401-01_04.xlsx",
    }
    assert next(f for f in artifact.files if f.role == "dental").published_at == date(2026, 4, 1)


def test_japan_missing_category_cannot_silently_reduce_coverage(settings, http, registry):
    from app.core.errors import DiscoveryError

    http.override(r"topics/2026/04/tp20260401-01\.html$", (
        '<a href="xls/tp20261014-01_01.xlsx">Oral only</a>'
    ).encode())
    with pytest.raises(DiscoveryError, match="injection.*topical.*dental"):
        make_adapter("jp_nhi", settings, http, registry).discover()


def test_japan_all_four_dupixent_prices_use_official_dose_units(run_adapter, http):
    from io import BytesIO
    from openpyxl import load_workbook
    from app.core.enums import PriceStatus
    from tests.conftest import fx

    # Exact Dupixent rows from the official October 14, 2026 injection workbook.
    workbook = load_workbook(fx("jp_nhi", "tp20260813-01_02.xlsx"))
    expected = {
        "4490405G1024": ("３００ｍｇ２ｍＬ１筒", "３００ｍｇシリンジ", "53493", "syringe", "1筒"),
        "4490405G2020": ("３００ｍｇ２ｍＬ１キット", "３００ｍｇペン", "53659", "kit", "1キット"),
        "4490405G3027": ("２００ｍｇ１．１４ｍＬ１筒", "２００ｍｇシリンジ", "39549", "syringe", "1筒"),
        "4490405G4023": ("２００ｍｇ１．１４ｍＬ１キット", "２００ｍｇペン", "39706", "kit", "1キット"),
    }
    for code, (spec, name, amount, _, _) in expected.items():
        workbook.active.append([
            "注射薬", code, "デュピルマブ（遺伝子組換え）", spec, None, None, None,
            "デュピクセント皮下注" + name, "サノフィ", None, "先発品", None, int(amount), None, None,
        ])
    payload = BytesIO()
    workbook.save(payload)
    workbook.close()
    http.override(r"tp20260813-01_02\.xlsx$", payload.getvalue())
    *_, normalized, report = run_adapter("jp_nhi")
    matches = [r for r in normalized.records if "デュピクセント" in r.product.name]
    assert len(matches) == 4
    assert not report.errors
    for record in matches:
        _, _, amount, unit, unit_text = expected[record.product.product_code]
        assert record.product.ingredient == "デュピルマブ（遺伝子組換え）"
        assert record.product.manufacturer == "サノフィ"
        assert record.product.code_type == "YJ_CODE"
        assert len(record.prices) == 1
        price = record.prices[0]
        assert price.amount == Decimal(amount) and price.currency == "JPY"
        assert price.price_unit == unit and price.unit_text == unit_text
        assert price.price_type == PriceBasis.REIMBURSEMENT and price.price_status == PriceStatus.REGULATED
        assert price.vat_included is True


def test_brazil_pmvg_formula_is_evaluated_and_flagged(run_adapter):
    *_, normalized, _ = run_adapter("br_cmed")
    rec = next(r for r in normalized.records if r.product.product_code == "505107701157215")
    pmvg = next(p for p in rec.prices if p.price_variant == "pmvg_sem_impostos")
    assert pmvg.price_status == "calculated" and "CAP" in pmvg.notes
    assert pmvg.amount == (Decimal("2098.20") * (1 - Decimal("0.1979"))).quantize(Decimal("0.01"))
    assert rec.prices[0].valid_from.isoformat() == "2026-09-24"
    assert next(p for p in rec.prices if p.price_variant.startswith("pf_icms_18")).tax_rate == Decimal(18)


def test_brazil_discovers_double_underscore_publication(settings, http, registry):
    from tests.conftest import fx

    page = fx("br_cmed", "page.html").read_text().replace(
        "lista_pmc_20260923_222937320", "lista_pmc__20261009_115232671",
    ).replace(
        "lista_pmvg_20260923_222937320", "lista_pmvg__20261009_115232671",
    )
    http.override(r"anvisa/pt-br/assuntos/medicamentos/cmed/precos$", page.encode())
    adapter = make_adapter("br_cmed", settings, http, registry)
    artifact = adapter.discover()
    assert artifact.published_at.isoformat() == "2026-10-09"
    assert {file.role for file in artifact.files} == {"pmc", "pmvg"}
    assert all("__20261009_115232671.xlsx/@@download/file" in file.url for file in artifact.files)
    assert {file.filename for file in artifact.files} == {
        "lista_pmc_20261009.xlsx", "lista_pmvg_20261009.xlsx",
    }


def test_festbetrag_pdf_wrapped_pzn_and_partial_status(run_adapter):
    adapter, _, _, dataset, normalized, _ = run_adapter("de_festbetrag")
    assert adapter.status == "partially_supported"
    assert dataset.row_errors == 0
    assert "19123426" in {r.key for r in dataset.records}  # PZN wrapped over two lines in the PDF
    rec = next(r for r in normalized.records if r.product.product_code == "11289015")
    assert {p.price_variant for p in rec.prices} == {"apothekenverkaufspreis", "festbetrag"}


def test_amnog_has_no_invented_price(run_adapter):
    *_, normalized, _ = run_adapter("de_amnog")
    assert len(normalized.records) == 4
    assert all(r.prices == [] and r.product.product_level == ProductLevel.REGULATORY_ENTRY for r in normalized.records)


def test_ontario_scope_is_provincial(run_adapter):
    *_, normalized, _ = run_adapter("ca_on_odb")
    assert all(p.coverage_scope == "province:ON" for r in normalized.records for p in r.prices)
