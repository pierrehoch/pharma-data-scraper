from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    Ingredient,
    IngredientAlias,
    IngredientDictionary,
    IngredientIdentifier,
    IngredientOverride,
    Price,
    Product,
    ProductIngredient,
    ProductIngredientResolution,
    Source,
)
from app.normalization.text import fold


@pytest.fixture
def ingredient_client(settings, session_factory):  # noqa: ANN001, ANN201
    with session_factory() as session:
        session.add_all([
            Source(
                id="fr_test", country="FR", name="French test", adapter="test",
                expected_frequency="monthly", source_url="https://example.invalid",
                identifier_type="test", adapter_status="active",
            ),
            Source(
                id="de_test", country="DE", name="German test", adapter="test",
                expected_frequency="monthly", source_url="https://example.invalid",
                identifier_type="test", adapter_status="active",
            ),
        ])
        base = Ingredient(id="apap", name="Acetaminophen")
        salt = Ingredient(id="apap-sodium", name="Acetaminophen sodium", family_id="apap", family_evidence="Documented family relation")
        combo = Ingredient(id="combo-x", name="Combination component X")
        other = Ingredient(id="other", name="Other substance")
        session.add_all([base, salt, combo, other])
        session.flush()
        session.add_all([
            IngredientAlias(ingredient_id="apap", label="Acetaminophen", normalized=fold("Acetaminophen"), language="en", evidence="fixture"),
            IngredientAlias(ingredient_id="apap", label="Paracétamol", normalized=fold("Paracétamol"), language="fr", evidence="fixture"),
            IngredientAlias(ingredient_id="apap-sodium", label="Acetaminophen sodium", normalized=fold("Acetaminophen sodium"), language="en", evidence="fixture"),
            IngredientAlias(ingredient_id="combo-x", label="Mélange ambigu", normalized=fold("Mélange ambigu"), language="fr", evidence="fixture"),
            IngredientAlias(ingredient_id="other", label="Mélange ambigu", normalized=fold("Mélange ambigu"), language="fr", evidence="fixture"),
        ])
        session.add(IngredientDictionary(id="fixture-v1", provider="fixture", cache_path="offline.json", summary={"terms": 5}))

        products = [
            Product(id=1, source_id="fr_test", country="FR", product_code="P1", code_type="test", product_level="pack", name="Base product", ingredient="Paracétamol"),
            Product(id=2, source_id="de_test", country="DE", product_code="P2", code_type="test", product_level="pack", name="Salt product", ingredient="Acetaminophen sodium"),
            Product(id=3, source_id="fr_test", country="FR", product_code="P3", code_type="test", product_level="pack", name="Combination", ingredient="Combination"),
            Product(id=4, source_id="de_test", country="DE", product_code="P4", code_type="test", product_level="pack", name="Unmapped", ingredient="Unknown text"),
        ]
        session.add_all(products)
        session.flush()
        session.add_all([
            Price(
                product_id=product.id, source_id=product.source_id, series_key=f"series-{product.id}",
                observation_key=f"obs-{product.id}", price_type="retail", price_status="available",
                amount=Decimal("10.00") if product.id != 2 else Decimal("0"), currency="EUR",
                price_unit="pack", retrieved_at=datetime.now(timezone.utc), snapshot_id=f"snap-{product.id}",
                is_current=True,
            )
            for product in products[:3]
        ])
        session.add_all([
            ProductIngredient(product_id=1, ingredient_id="apap", source_text="Paracétamol", method="alias", evidence="fixture", dictionary_version="fixture-v1"),
            ProductIngredient(product_id=1, ingredient_id="combo-x", source_text="Combination", method="alias", evidence="fixture", dictionary_version="fixture-v1"),
            ProductIngredient(product_id=2, ingredient_id="apap-sodium", source_text="Acetaminophen sodium", method="alias", evidence="fixture", dictionary_version="fixture-v1"),
            ProductIngredient(product_id=3, ingredient_id="apap", source_text="Acetaminophen", method="alias", evidence="fixture", dictionary_version="fixture-v1"),
            ProductIngredient(product_id=3, ingredient_id="combo-x", source_text="Combination component X", method="alias", evidence="fixture", dictionary_version="fixture-v1"),
        ])
        session.add_all([
            ProductIngredientResolution(product_id=1, status="resolved", reason="", fingerprint="a" * 64, components=[], dictionary_version="fixture-v1"),
            ProductIngredientResolution(product_id=2, status="resolved", reason="", fingerprint="b" * 64, components=[], dictionary_version="fixture-v1"),
            ProductIngredientResolution(product_id=3, status="partial", reason="One component unresolved", fingerprint="c" * 64, components=["Acetaminophen"], dictionary_version="fixture-v1"),
        ])
        session.commit()

    from app.api.main import create_app

    return TestClient(create_app())


def test_alias_suggestions_and_counts(ingredient_client):  # noqa: ANN001
    result = ingredient_client.get("/ingredients?q=PARACETAMOL").json()
    assert result["total"] == 1
    assert result["items"][0] == {
        "id": "apap",
        "name": "Acetaminophen",
        "family_id": None,
        "matched_alias": "Paracétamol",
        "language": "fr",
        "mapped_products": 2,
        "priced_products": 2,
    }
    ambiguous = ingredient_client.get("/ingredients?q=mélange").json()
    assert {item["id"] for item in ambiguous["items"]} == {"combo-x", "other"}


def test_family_membership_deduplicates_combinations_and_preserves_other_filters(ingredient_client):  # noqa: ANN001
    response = ingredient_client.get("/prices?ingredient_id=apap&country=fr&price_type=retail")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 2
    assert {row["product_id"] for row in result["items"]} == {1, 3}
    combo = next(row for row in result["items"] if row["product_id"] == 3)
    assert {mapping["ingredient_id"] for mapping in combo["ingredient_mappings"]} == {"apap", "combo-x"}
    assert combo["ingredient_resolution"]["status"] == "partial"
    detail = ingredient_client.get("/products/3").json()
    assert {mapping["name"] for mapping in detail["ingredient_mappings"]} == {"Acetaminophen", "Combination component X"}
    assert detail["ingredient_resolution"]["status"] == "partial"
    family_rows = ingredient_client.get("/prices?ingredient_id=apap&page_size=100").json()["items"]
    assert {row["product_id"] for row in family_rows} == {1, 2, 3}
    assert ingredient_client.get("/prices?ingredient_id=apap-sodium").json()["total"] == 3
    assert ingredient_client.get("/prices?ingredient_id=missing").status_code == 404


def test_csv_contains_json_mapping_metadata_and_filters(ingredient_client):  # noqa: ANN001
    response = ingredient_client.get("/prices.csv?ingredient_id=apap&source_id=fr_test&current_only=true")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2
    assert all(json.loads(row["ingredient_mappings"]) for row in rows)
    assert {row["ingredient_resolution"] for row in rows} == {
        '{"status":"resolved","reason":"","components":[],"dictionary_version":"fixture-v1"}',
        '{"status":"partial","reason":"One component unresolved","components":["Acetaminophen"],"dictionary_version":"fixture-v1"}',
    }
    assert ingredient_client.get("/prices.csv?ingredient_id=missing").status_code == 404


def test_salt_selection_searches_documented_family_across_surfaces(ingredient_client):  # noqa: ANN001
    suggestion = ingredient_client.get("/ingredients?q=Acetaminophen%20sodium").json()["items"][0]
    assert suggestion["id"] == "apap-sodium"
    response = ingredient_client.get("/prices", params={"ingredient_id": suggestion["id"]})
    assert response.status_code == 200
    rows = response.json()["items"]
    assert {row["product_id"] for row in rows} == {1, 2, 3}
    assert len({row["price_id"] for row in rows}) == len(rows)
    salt = next(row for row in rows if row["product_id"] == 2)
    assert salt["ingredient_mappings"][0]["ingredient_id"] == "apap-sodium"
    exported = ingredient_client.get("/prices.csv?ingredient_id=apap-sodium&country=FR")
    assert exported.status_code == 200
    assert {row["product_id"] for row in csv.DictReader(io.StringIO(exported.text))} == {"1", "3"}
    dashboard = ingredient_client.get("/dashboard/data?ingredient=Acetaminophen%20sodium")
    assert dashboard.status_code == 200
    assert all(name in dashboard.text for name in ("Base product", "Salt product", "Combination"))
    assert "Unmapped" not in dashboard.text
    assert "ingredient_id=apap-sodium" in dashboard.text


def test_dashboard_resolves_only_complete_unique_aliases(ingredient_client):  # noqa: ANN001
    exact = ingredient_client.get("/dashboard/data?ingredient=paracétamol")
    assert exact.status_code == 200 and "Ingredient: <strong>Acetaminophen</strong>" in exact.text
    assert "Base product" in exact.text and "Unmapped" not in exact.text
    partial = ingredient_client.get("/dashboard/data?ingredient=acet")
    assert "partial or ambiguous text is not applied" in partial.text
    assert "Base product" in partial.text and "Unmapped" in partial.text
    ambiguous = ingredient_client.get("/dashboard/data?ingredient=mélange%20ambigu")
    assert "Combination component X" in ambiguous.text and "Other substance" in ambiguous.text
    assert ingredient_client.get("/dashboard/data?ingredient_id=missing").status_code == 404
    detail = ingredient_client.get("/dashboard/data/3")
    assert "Resolution: <strong>partial</strong>" in detail.text and "Acetaminophen" in detail.text


def test_coverage_reports_resolution_gaps_and_dictionary(ingredient_client):  # noqa: ANN001
    coverage = ingredient_client.get("/ingredients/coverage").json()
    assert "not known matches" in coverage["limitations"]
    by_source = {row["source_id"]: row for row in coverage["sources"]}
    assert by_source["fr_test"]["resolution_counts"] == {"resolved": 1, "partial": 1}
    assert by_source["de_test"]["resolution_missing"] == 1
    assert by_source["fr_test"]["priced_mapped_products"] == 2
    assert coverage["dictionaries"][0]["id"] == "fixture-v1"


def review_form(client, product_id=4):
    from bs4 import BeautifulSoup

    response = client.get(f"/dashboard/ingredients/{product_id}")
    assert response.status_code == 200
    html = BeautifulSoup(response.text, "html.parser")
    return {
        "csrf": html.select_one('input[name="csrf"]')["value"],
        "revision": html.select_one('input[name="revision"]')["value"],
        "action": "save", "ingredient_ids": ["apap"], "evidence": "Verified source label: fixture",
    }


def test_review_queue_grouping_and_filters(ingredient_client, session_factory):
    with session_factory() as session:
        session.add(Product(
            id=5, source_id="de_test", country="DE", product_code="P5", code_type="test",
            product_level="pack", name="Second unmapped", ingredient="Unknown text",
        ))
        session.commit()
    response = ingredient_client.get("/dashboard/ingredients")
    assert response.status_code == 200
    assert "Unknown text" in response.text and "Combination" in response.text
    assert "Base product" not in response.text
    assert 'href="/dashboard/ingredients"' in ingredient_client.get("/dashboard/data").text
    filtered = ingredient_client.get("/dashboard/ingredients?source_id=fr_test&status=partial&search=Combination")
    assert filtered.status_code == 200
    assert "Combination" in filtered.text and "Unknown text" not in filtered.text
    assert "No ingredient groups" in ingredient_client.get("/dashboard/ingredients?search=absent").text
    assert ingredient_client.get("/dashboard/ingredients?status=invalid").status_code == 422
    assert ingredient_client.get("/dashboard/ingredients/999").status_code == 404


def test_review_picker_exposes_verified_identity_and_evidence(ingredient_client, session_factory):
    with session_factory() as session:
        session.add(IngredientIdentifier(
            ingredient_id="apap", namespace="rxnorm", value="161",
            evidence="https://rxnav.nlm.nih.gov/REST/rxcui/161/properties.json",
        ))
        session.commit()
    response = ingredient_client.get("/dashboard/ingredients/4?search=paracetamol")
    assert response.status_code == 200
    assert 'name="ingredient_ids" value="apap"' in response.text
    assert "rxnorm: 161" in response.text
    assert "https://rxnav.nlm.nih.gov/REST/rxcui/161/properties.json" in response.text
    assert "No dictionary match" in ingredient_client.get(
        "/dashboard/ingredients/4?search=no-such-substance",
    ).text


def test_review_save_edit_remove_applies_exact_source_label(ingredient_client, session_factory):
    from sqlalchemy import select
    from app.normalization.ingredients import IngredientResolver

    with session_factory() as session:
        session.add_all([
            Product(id=5, source_id="de_test", country="DE", product_code="P5", code_type="test",
                    product_level="pack", ingredient="Unknown text"),
            Product(id=6, source_id="fr_test", country="FR", product_code="P6", code_type="test",
                    product_level="pack", ingredient="Unknown text"),
        ])
        session.commit()
    data = review_form(ingredient_client)
    data["ingredient_ids"] = ["apap", "combo-x", "apap"]
    response = ingredient_client.post("/dashboard/ingredients/4", data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("updated=2")
    with session_factory() as session:
        override = session.scalar(select(IngredientOverride))
        assert override.ingredient_ids == ["apap", "combo-x"]
        assert override.source_text == "Unknown text" and override.source_id == "de_test"
        for product_id in (4, 5):
            assert session.get(ProductIngredientResolution, product_id).status == "resolved"
            links = list(session.scalars(select(ProductIngredient).where(ProductIngredient.product_id == product_id)))
            assert {row.ingredient_id for row in links} == {"apap", "combo-x"}
            assert {row.method for row in links} == {"manual"}
        assert session.get(ProductIngredientResolution, 6) is None
        # Future imports use the same override without dashboard intervention.
        session.add(Product(id=7, source_id="de_test", country="DE", product_code="P7", code_type="test",
                            product_level="pack", ingredient="Unknown text"))
        session.flush()
        IngredientResolver(session).resolve(session, session.get(Product, 7))
        session.commit()
        assert session.get(ProductIngredientResolution, 7).status == "resolved"
    assert "Unknown text" in ingredient_client.get("/dashboard/ingredients?status=manual").text
    assert "Unknown text" not in ingredient_client.get("/dashboard/ingredients?source_id=de_test").text
    stale = ingredient_client.post("/dashboard/ingredients/4", data=data)
    assert stale.status_code == 409
    edited = review_form(ingredient_client)
    edited["ingredient_ids"] = ["other"]
    assert ingredient_client.post("/dashboard/ingredients/4", data=edited, follow_redirects=False).status_code == 303
    assert ingredient_client.get("/products/5").json()["ingredient_mappings"][0]["ingredient_id"] == "other"
    deleted = review_form(ingredient_client)
    deleted["action"] = "delete"
    assert ingredient_client.post("/dashboard/ingredients/4", data=deleted, follow_redirects=False).status_code == 303
    with session_factory() as session:
        assert session.scalar(select(IngredientOverride)) is None
        assert session.get(ProductIngredientResolution, 4).status == "unresolved"
        assert session.get(Product, 4).ingredient == "Unknown text"
        assert len(list(session.scalars(select(Price)))) == 3


@pytest.mark.parametrize("change", [
    {"csrf": "invalid"}, {"csrf": "invalid-\u00e9"}, {"ingredient_ids": ["missing"]},
    {"ingredient_ids": []}, {"evidence": "   "},
])
def test_invalid_review_does_not_mutate(ingredient_client, session_factory, change):
    from sqlalchemy import func, select

    data = review_form(ingredient_client)
    data.update(change)
    response = ingredient_client.post("/dashboard/ingredients/4", data=data)
    assert response.status_code in (403, 422)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngredientOverride)) == 0
        assert session.get(ProductIngredientResolution, 4) is None


def test_review_cross_origin_empty_label_and_stale_source_rejected(ingredient_client, session_factory):
    data = review_form(ingredient_client)
    assert ingredient_client.post("/dashboard/ingredients/4", data=data,
                                  headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    with session_factory() as session:
        session.get(Product, 4).ingredient = "Source changed"
        session.commit()
    assert ingredient_client.post("/dashboard/ingredients/4", data=data).status_code == 409
    with session_factory() as session:
        session.get(Product, 4).ingredient = None
        session.commit()
    page = ingredient_client.get("/dashboard/ingredients/4")
    assert "source has no ingredient wording" in page.text
    assert 'id="review-form"' not in page.text
    assert ingredient_client.post("/dashboard/ingredients/4", data=data).status_code == 422


def test_review_failure_rolls_back_mapping_and_product_annotations(ingredient_client, session_factory, monkeypatch):
    from sqlalchemy import func, select
    from app.normalization.ingredients import IngredientResolver

    data = review_form(ingredient_client)
    original = IngredientResolver.resolve

    def fail(self, session, product, components=None):
        original(self, session, product, components)
        session.flush()
        raise RuntimeError("Simulated resolution failure")

    monkeypatch.setattr(IngredientResolver, "resolve", fail)
    with pytest.raises(RuntimeError, match="Simulated"):
        ingredient_client.post("/dashboard/ingredients/4", data=data)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngredientOverride)) == 0
        assert session.get(ProductIngredientResolution, 4) is None
        assert session.scalar(select(func.count()).select_from(ProductIngredient).where(ProductIngredient.product_id == 4)) == 0


def test_review_updates_across_batches_and_removal_restores_automatic_alias(ingredient_client, session_factory):
    from sqlalchemy import func, select

    with session_factory() as session:
        session.add_all([
            Product(source_id="de_test", country="DE", product_code=f"batch-{index}", code_type="test",
                    product_level="pack", ingredient="Unknown text")
            for index in range(501)
        ])
        session.commit()
    data = review_form(ingredient_client)
    saved = ingredient_client.post("/dashboard/ingredients/4", data=data, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"].endswith("updated=502")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductIngredient).where(
            ProductIngredient.method == "manual",
        )) == 502
    data = review_form(ingredient_client, 1)
    data["ingredient_ids"] = ["other"]
    assert ingredient_client.post("/dashboard/ingredients/1", data=data, follow_redirects=False).status_code == 303
    data = review_form(ingredient_client, 1)
    data["action"] = "delete"
    assert ingredient_client.post("/dashboard/ingredients/1", data=data, follow_redirects=False).status_code == 303
    mappings = ingredient_client.get("/products/1").json()["ingredient_mappings"]
    assert len(mappings) == 1
    assert mappings[0]["ingredient_id"] == "apap"
    assert mappings[0]["method"] == "exact_alias"
