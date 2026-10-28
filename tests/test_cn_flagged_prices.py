"""Chinese source figures with incomplete semantics are published with explicit warnings, never guessed."""

import csv
import io
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.adapters._cn_public import NOT_CURRENT_REASON, QUERY_REASON, UNESTABLISHED_REASON, publication_flags
from app.core.enums import PriceBasis, PriceStatus
from app.core.pipeline import RunOptions, Selection
from app.core.pricing import normalize_price
from app.core.types import PriceFact
from app.db.models import Price
from tests.test_cn_provincial import CODE, workbook
from tests.test_cn_public import gd_catalogue, public_adapter


def cell(**overrides):
    data = {
        "amount": "12.5", "original_value": "12.5", "value_kind": "price", "price_label": "挂网价",
        "currency": None, "fact_unit": None, "price_type": "public_procurement", "price_variant": "guawang_unit_listing",
        "price_status": "reference", "pricing_unit": None, "status": "final", "published_at": "2025-10-11",
        "effective_from": None, "normalized": False, "reason": UNESTABLISHED_REASON,
        "source_url": "https://ybj.hubei.gov.cn/a.xlsx", "notice_url": "https://ybj.hubei.gov.cn/a.shtml",
    }
    return data | overrides


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, ["currency_inferred_cny", "price_basis_unknown"]),
    ({"currency": "CNY", "fact_unit": "pack"}, []),
    ({"price_type": None, "price_variant": None}, ["currency_inferred_cny", "price_basis_unknown", "price_concept_unknown"]),
    ({"reason": NOT_CURRENT_REASON, "status": "previous", "currency": "CNY", "fact_unit": "pack"},
     ["previous_value", "not_current_value"]),
    ({"reason": NOT_CURRENT_REASON, "status": "proposed", "currency": "CNY", "fact_unit": "pack"},
     ["proposed_value", "not_current_value"]),
    ({"reason": NOT_CURRENT_REASON, "status": "withdrawn", "currency": "CNY", "fact_unit": "pack"},
     ["withdrawn_value", "not_current_value"]),
    ({"reason": NOT_CURRENT_REASON, "status": "final", "currency": "CNY", "fact_unit": "pack"},
     ["future_value", "not_current_value"]),
    ({"reason": QUERY_REASON, "price_type": None, "published_at": None},
     ["currency_inferred_cny", "price_basis_unknown", "price_concept_unknown", "publication_date_unknown"]),
])
def test_publishable_values_get_exact_warnings(overrides, expected):
    assert publication_flags(cell(**overrides)) == expected


@pytest.mark.parametrize("overrides", [
    {"reason": "raw herbal/ingredient material"},
    {"reason": "template/example value"},
    {"reason": "ambiguous duplicate price column"},
    {"reason": "Aggregate monetary amount, not a unit price", "value_kind": "aggregate_amount"},
    {"reason": "Daily treatment cost or mixed cost/bid-limit basis, not a unit price", "value_kind": "daily_cost"},
    {"amount": "0"},
    {"amount": None, "reason": "Non-scalar price cell: '*'"},
])
def test_out_of_scope_or_unusable_values_stay_unpublished(overrides):
    assert publication_flags(cell(**overrides)) is None


def test_flagged_fact_never_claims_unknown_unit_or_concept(settings, http, registry):
    adapter = public_adapter(settings, http, registry, "cn_hubei")
    data = cell()
    fact = adapter._flagged_fact(data, publication_flags(data), Decimal(1), "unit")
    assert fact.currency == "CNY" and fact.price_unit == "unknown"
    # guawang_unit_listing would claim a per-unit basis the source does not state.
    assert fact.price_variant == "guawang_listing" and fact.price_type == PriceBasis.PUBLIC_PROCUREMENT
    assert fact.quality_flags == ("currency_inferred_cny", "price_basis_unknown") and not fact.never_current
    assert normalize_price(fact) == (None, None)
    unknown = adapter._flagged_fact(cell(price_type=None, price_variant=None, price_label="降价后价格"),
                                    ["price_concept_unknown"], None, None)
    assert unknown.price_type == PriceBasis.OTHER and unknown.price_variant == "unclassified:降价后价格"


def test_unknown_basis_is_never_normalized_per_unit():
    fact = PriceFact(amount=Decimal(10), currency="CNY", price_type=PriceBasis.OTHER, price_status=PriceStatus.REFERENCE,
                     price_unit="unknown", pack_quantity=Decimal(10), pack_unit="tablet")
    assert normalize_price(fact) == (None, None)


def run_gd(pipeline, http):
    adapter = public_adapter(pipeline.settings, http, pipeline.registry)
    gd_catalogue(http)
    columns = ["国家医保代码", "药品名称", "包装规格", "原包装挂网价格（元）", "包装挂网价格（元）"]
    http.override(r"/attachement/test.xlsx", workbook(columns, [dict(zip(columns, [CODE, "测试片", "28片/盒", 40, 30]))]))
    outcome = pipeline.run(Selection(source_ids=["cn_gdmede"]), RunOptions()).jobs[0]
    assert outcome.error_count == 0
    return adapter


def test_previous_value_is_published_but_never_supersedes_current(pipeline, session_factory, http):
    run_gd(pipeline, http)
    with session_factory() as session:
        prices = {(p.price_variant, p.amount): p for p in session.scalars(select(Price).where(Price.amount > 1))}
    current = prices[("guawang_pack_listing", Decimal(30))]
    previous = prices[("guawang_pack_listing:previous", Decimal(40))]
    assert current.is_current and current.quality_flags == [] and current.superseded_by_id is None
    assert not previous.is_current and previous.quality_flags == ["previous_value", "not_current_value"]
    assert previous.unit_price is not None  # explicit pack basis: per-unit value remains derivable


def test_api_csv_and_dashboard_show_flagged_values_by_default(pipeline, http):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    run_gd(pipeline, http)
    with TestClient(create_app()) as client:
        rows = client.get("/prices?source_id=cn_gdmede&page_size=50").json()["items"]
        by_amount = {}
        for row in rows:
            by_amount.setdefault(row["amount"], []).append(row)
        assert {"30", "40", "0.3"} <= set(by_amount)
        previous = by_amount["40"][0]
        assert previous["is_current"] is False and "not_current_value" in previous["quality_flags"]
        assert any("Previous price" in warning for warning in previous["quality_warnings"])
        query = by_amount["0.3"]
        assert len(query) == 3 and all(r["price_unit"] == "unknown" and r["unit_price"] is None for r in query)
        assert by_amount["30"][0]["quality_warnings"] == []

        verified = client.get("/prices?source_id=cn_gdmede&verified_only=true&page_size=50").json()["items"]
        assert {r["amount"] for r in verified if r["price_id"]} == {"30"}

        exported = list(csv.DictReader(io.StringIO(client.get("/prices.csv?source_id=cn_gdmede").text)))
        flagged = next(r for r in exported if r["amount"] == "40")
        assert "not_current_value" in flagged["quality_flags"] and "Previous price" in flagged["quality_warnings"]

        page = client.get("/dashboard/data?source_id=cn_gdmede").text
        assert "not_current_value" in page and "currency_inferred_cny" in page and "hide prices published with warnings" in page
        hidden = client.get("/dashboard/data?source_id=cn_gdmede&verified_only=true").text
        assert "not_current_value" not in hidden and "verified_only=true" in hidden

        detail = client.get(f"/products/{previous['product_id']}").json()
        assert any(p["quality_warnings"] for p in detail["prices"])
        assert "Previous price" in client.get(f"/dashboard/data/{previous['product_id']}").text
