import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.types import IngredientComponent
from app.db.models import (
    Ingredient, IngredientAlias, IngredientDictionary, IngredientIdentifier,
    Product, ProductIngredient, ProductIngredientResolution, Source,
)
from app.normalization.ingredient_dictionary import (
    DictionaryDocument, fetch_rxnorm, fetch_wikidata, install_dictionary,
)
from app.normalization.ingredients import IngredientResolver, backfill


def dictionary():
    return {
        "provider": "test", "revision": "1",
        "ingredients": [
            {
                "name": "acetaminophen", "identifiers": {"rxnorm": "161", "wikidata": "Q57055"},
                "aliases": [
                    {"label": name, "language": language} for name, language in [
                        ("acetaminophen", "en"), ("paracétamol", "fr"), ("paracetamolo", "it"),
                        ("アセトアミノフェン", "ja"), ("对乙酰氨基酚", "zh"),
                    ]
                ], "evidence": "https://www.wikidata.org/wiki/Q57055",
            },
            {
                "name": "example base", "identifiers": {"test": "base"},
                "aliases": [{"label": "example base", "language": "en"}], "evidence": "reviewed:test",
            },
            {
                "name": "example salt", "identifiers": {"test": "salt"},
                "aliases": [{"label": "example salt", "language": "en"}], "evidence": "reviewed:test",
                "family": {"namespace": "test", "value": "base"}, "family_evidence": "reviewed relationship",
            },
        ],
    }


def install(session, tmp_path, value=None):
    return install_dictionary(
        session, DictionaryDocument.model_validate(value or dictionary()), cache_path=tmp_path / "dict.json",
    )


def product(session, ingredient, code="1", source_id="test"):
    if session.get(Source, source_id) is None:
        session.add(Source(
            id=source_id, country="FR", name=source_id, adapter=source_id,
            adapter_status="supported", parser_type="csv", source_url="https://example.org",
            expected_frequency="monthly", identifier_type="TEST",
        ))
        session.flush()
    row = Product(
        source_id=source_id, country="FR", product_code=code, code_type="TEST",
        product_level="presentation", ingredient=ingredient,
    )
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("label", ["PARACÉTAMOL", "acetaminophen", "paracetamolo", "アセトアミノフェン", "对乙酰氨基酚"])
def test_multilingual_aliases_share_identity(session_factory, tmp_path, label):
    with session_factory() as session:
        install(session, tmp_path)
        row = product(session, label)
        IngredientResolver(session).resolve(session, row)
        session.flush()
        link = session.scalar(select(ProductIngredient))
        assert link is not None
        identity = session.scalar(select(IngredientIdentifier).where(
            IngredientIdentifier.namespace == "rxnorm", IngredientIdentifier.value == "161",
        ))
        assert link.ingredient_id == identity.ingredient_id
        assert session.get(ProductIngredientResolution, row.id).status == "resolved"


def test_structured_combination_and_partial_components(session_factory, tmp_path):
    with session_factory() as session:
        install(session, tmp_path)
        row = product(session, "source compound text")
        resolver = IngredientResolver(session)
        resolver.resolve(session, row, [
            IngredientComponent("paracétamol"), IngredientComponent("unmapped substance"),
        ])
        session.flush()
        assert session.scalar(select(func.count()).select_from(ProductIngredient)) == 1
        assert session.get(ProductIngredientResolution, row.id).status == "partial"
        resolver.resolve(session, row, [IngredientComponent("paracétamol"), IngredientComponent("example salt")])
        session.flush()
        assert session.scalar(select(func.count()).select_from(ProductIngredient)) == 2
        assert session.get(ProductIngredientResolution, row.id).status == "resolved"


def test_no_unsafe_splitting_or_class_guessing(session_factory, tmp_path):
    with session_factory() as session:
        install(session, tmp_path)
        resolver = IngredientResolver(session)
        for i, name in enumerate(["paracétamol/caffeine", "Hemofiltrados", "example salt hydrate"]):
            row = product(session, name, str(i))
            resolver.resolve(session, row)
        session.flush()
        assert session.scalar(select(func.count()).select_from(ProductIngredient)) == 0


@pytest.mark.parametrize("label", ["N02BE01 - paracétamol", "N02BE01-paracétamol", "N02BE01 paracétamol"])
def test_cipm_classification_prefix_does_not_hide_exact_ingredient(session_factory, tmp_path, label):
    with session_factory() as session:
        install(session, tmp_path)
        row = product(session, label, source_id="es_cipm")
        IngredientResolver(session).resolve(session, row)
        session.flush()
        assert session.get(ProductIngredientResolution, row.id).status == "resolved"
        assert row.ingredient == label


def test_ambiguous_alias_stays_unmapped(session_factory, tmp_path):
    with session_factory() as session:
        document = dictionary()
        document["ingredients"][1]["aliases"].append({"label": "acetaminophen", "language": "en"})
        install(session, tmp_path, document)
        row = product(session, "acetaminophen")
        IngredientResolver(session).resolve(session, row)
        session.flush()
        assert session.scalar(select(func.count()).select_from(ProductIngredient)) == 0
        assert session.get(ProductIngredientResolution, row.id).status == "ambiguous"


def test_backfill_is_idempotent_and_invalidates_changed_source(session_factory, tmp_path):
    with session_factory() as session:
        install(session, tmp_path)
        row = product(session, "paracétamol")
        IngredientResolver(session).resolve(session, row, [IngredientComponent("paracétamol")])
        row_id = row.id
        session.commit()
        assert backfill(session) == {"resolved": 1}
        assert backfill(session) == {"resolved": 1}
        assert session.scalar(select(func.count()).select_from(ProductIngredient)) == 1
        session.get(Product, row_id).ingredient = "unknown changed substance"
        session.commit()
        assert backfill(session) == {"unresolved": 1}
        assert session.scalar(select(func.count()).select_from(ProductIngredient)) == 0


def test_national_code_reconciliation_requires_exact_identifier_system(session_factory, tmp_path):
    with session_factory() as session:
        install(session, tmp_path)
        reference = product(session, "acetaminophen", "12345678", "ca_hc_dpd")
        target = product(session, "local label with strength", "12345678", "ca_on_odb")
        pin = product(session, "local label", "12345678", "ca_bc_pharmacare")
        for row in (reference, target, pin):
            row.country = "CA"
            row.code_type = "DIN_PIN" if row is pin else "DIN"
        session.commit()
        assert backfill(session) == {"resolved": 2, "unresolved": 1}
        assert session.scalar(select(ProductIngredient).where(ProductIngredient.product_id == target.id)).method == "national_code"
        assert session.scalar(select(ProductIngredient).where(ProductIngredient.product_id == pin.id)) is None


def test_manual_override_survives_automatic_refresh(session_factory, tmp_path):
    with session_factory() as session:
        row = product(session, "local medicine label")
        document = dictionary()
        document["overrides"] = [{
            "source_id": "test", "source_text": "local medicine label",
            "ingredients": [{"namespace": "rxnorm", "value": "161"}], "evidence": "reviewed source",
        }]
        install(session, tmp_path, document)
        session.commit()
        install(session, tmp_path)
        IngredientResolver(session).resolve(session, row)
        session.flush()
        assert session.scalar(select(ProductIngredient)).method == "manual"


def test_family_cycle_and_identity_conflict_are_rejected(session_factory, tmp_path):
    with session_factory() as session:
        install(session, tmp_path)
        session.commit()
        value = dictionary()
        value["ingredients"][1].update(
            family={"namespace": "test", "value": "salt"}, family_evidence="invalid cycle",
        )
        with pytest.raises(ValueError, match="Cyclic"):
            install(session, tmp_path, value)
        session.rollback()
        value = dictionary()
        value["ingredients"][0]["identifiers"]["test"] = "base"
        with pytest.raises(ValueError, match="Conflicting ingredient identities"):
            install(session, tmp_path, value)
        session.rollback()
        assert session.scalar(select(func.count()).select_from(Ingredient)) == 3


def test_refresh_removes_stale_provider_aliases_without_changing_ids(session_factory, tmp_path):
    with session_factory() as session:
        install(session, tmp_path)
        ids = list(session.scalars(select(Ingredient.id).order_by(Ingredient.id)))
        value = dictionary()
        value["ingredients"][0]["aliases"] = [{"label": "new reviewed label", "language": "en"}]
        install(session, tmp_path, value)
        session.flush()
        assert list(session.scalars(select(Ingredient.id).order_by(Ingredient.id))) == ids
        assert session.scalar(select(IngredientAlias).where(IngredientAlias.normalized == "paracetamol")) is None


def test_dictionary_download_fixtures_validate_ingredient_types(tmp_path):
    class Http:
        def get_json(self, url, **kwargs):
            if "allconcepts" in url:
                tty = "PIN" if "PIN" in url else "IN"
                return {"minConceptGroup": {"minConcept": [{"rxcui": "2" if tty == "PIN" else "1", "name": tty, "tty": tty}]}}
            if "related" in url:
                return {"relatedGroup": {"conceptGroup": [
                    {"tty": "IN", "conceptProperties": [{"tty": "IN", "rxcui": "1"}]},
                ]}}
            return {"entities": {"Q1": {
                "type": "item", "lastrevid": 42, "labels": {"en": {"value": "IN"}, "ja": {"value": "日本語"}},
                "claims": {"P3345": [{"mainsnak": {"datavalue": {"value": "1"}}}]},
            }}}

    rxnorm, path = fetch_rxnorm(Http(), tmp_path, substance_names={"pin"})
    assert path.is_file()
    assert rxnorm.ingredients[1].family.value == "1"
    wikidata, path = fetch_wikidata(Http(), tmp_path, ["Q1"], {"1", "2"})
    assert wikidata.ingredients[0].identifiers == {"wikidata": "Q1", "rxnorm": "1"}
    assert any(a.language == "ja" for a in wikidata.ingredients[0].aliases)
    assert json.loads(path.read_text())["provider"] == "wikidata"


def test_reviewed_bootstrap_has_real_multilingual_identities(session_factory, tmp_path):
    path = Path(__file__).parents[1] / "data" / "ingredients" / "bootstrap.json"
    document = DictionaryDocument.model_validate_json(path.read_text())
    with session_factory() as session:
        install_dictionary(session, document, cache_path=path)
        resolver = IngredientResolver(session)
        for index, label in enumerate(["dupilumab", "Dupilumabe", "デュピルマブ"]):
            row = product(session, label, str(index))
            resolver.resolve(session, row)
        session.flush()
        links = list(session.scalars(select(ProductIngredient)))
        assert len(links) == 3
        assert len({r.ingredient_id for r in links}) == 1
        assert session.scalar(select(IngredientIdentifier.value).where(
            IngredientIdentifier.ingredient_id == links[0].ingredient_id,
            IngredientIdentifier.namespace == "rxnorm",
        )) == "1876376"


def test_additive_migration_downgrade_preserves_source_data(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    root = Path(__file__).parents[1]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "0002")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO price (id, product_id, source_id, series_key, observation_key, price_type, price_variant, "
            "price_status, amount, currency, price_unit, retrieved_at, coverage_scope, snapshot_id, quality_flags, "
            "is_current) VALUES (1,1,'test','original-series','original-observation','retail','default','observed',"
            "12.50,'EUR','pack','2026-01-01','national','snapshot','[]',1)"
        ))
    command.upgrade(config, "head")
    assert "ingredient" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        before = connection.execute(text("SELECT * FROM price")).all()
    command.downgrade(config, "0002")
    assert "ingredient" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM price")).all() == before


def test_cli_import_export_and_failure_preserve_dictionary(session_factory, tmp_path, capsys):
    from app.cli import main

    source = tmp_path / "input.json"
    source.write_text(json.dumps(dictionary()))
    assert main(["ingredients", "import", str(source)]) == 0
    exported = tmp_path / "export.json"
    assert main(["ingredients", "export", str(exported)]) == 0
    assert exported.is_file()
    assert main(["ingredients", "export", str(exported)]) == 1
    assert "Refusing to overwrite" in capsys.readouterr().err
    assert main(["ingredients", "import", str(exported)]) == 0
    with session_factory() as session:
        count = session.scalar(select(func.count()).select_from(Ingredient))
        assert count == 3
    source.write_text('{"provider":"invalid","unknown":true}')
    assert main(["ingredients", "import", str(source)]) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Ingredient)) == count
    assert main(["ingredients", "backfill", "--source", "unknown-source"]) == 1


def test_incremental_sync_removes_newly_excluded_entity_aliases(session_factory, tmp_path, monkeypatch):
    from app.cli import main
    from app.normalization.ingredient_dictionary import cache_json

    old = dictionary()
    old["provider"] = "wikidata"
    old["ingredients"] = [old["ingredients"][0]]
    path = tmp_path / "wikidata.json"
    path.write_text(json.dumps(old))
    assert main(["ingredients", "import", str(path)]) == 0
    replacement = DictionaryDocument.model_validate({
        "provider": "wikidata", "revision": "2",
        "ingredients": [{
            "name": "caffeine", "identifiers": {"wikidata": "Q60235", "rxnorm": "1886"},
            "aliases": [{"label": "caffeine", "language": "en"}], "evidence": "reviewed:test",
        }],
        "excluded_entities": ["Q57055: ambiguous external substance identity; not imported"],
    })

    def fetch(http, cache_dir, entity_ids, known_rxnorm):
        return replacement, cache_json(cache_dir, "wikidata", replacement.model_dump())

    monkeypatch.setattr("app.normalization.ingredient_dictionary.fetch_wikidata", fetch)
    assert main(["ingredients", "sync", "--provider", "wikidata", "--entity", "Q57055", "--entity", "Q60235"]) == 0
    with session_factory() as session:
        assert session.scalar(select(IngredientAlias).where(IngredientAlias.label == "paracétamol")) is None
        assert session.scalar(select(IngredientAlias).where(IngredientAlias.label == "caffeine")) is not None
