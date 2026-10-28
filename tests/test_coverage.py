import argparse
import json

import pytest

from app.api.queries import product_coverage
from app.cli import build_parser, cmd_coverage
from app.core.pipeline import RunOptions, Selection


def test_coverage_counts_products_not_price_rows(pipeline, session_factory):
    pipeline.run(Selection(source_ids=["ca_bc_pharmacare"]), RunOptions())
    with session_factory() as session:
        rows = product_coverage(session, ["dupixent", "dupilumab"], ["ca_bc_pharmacare", "ca_on_odb"])
    bc, ontario = rows
    assert bc["products"] == 1
    assert bc["priced_products"] == 1
    assert bc["price_observations"] == 1
    assert bc["matches"][0]["product_code"] == "02470365"
    assert bc["matches"][0]["prices"][0]["valid_from"] == "2026-04-01"
    assert bc["matches"][0]["prices"][0]["amount"] == "535.14040000"
    assert ontario["products"] == 0


def test_coverage_product_only_and_literal_search(pipeline, session_factory):
    pipeline.run(Selection(source_ids=["ca_bc_pharmacare"]), RunOptions())
    with session_factory() as session:
        row = product_coverage(session, ["unpriced"], ["ca_bc_pharmacare"])[0]
        assert row["products"] == 1 and row["priced_products"] == row["price_observations"] == 0
        assert product_coverage(session, ["%"], ["ca_bc_pharmacare"])[0]["products"] == 0
        with pytest.raises(ValueError, match="non-empty"):
            product_coverage(session, [" "], ["ca_bc_pharmacare"])


def test_coverage_command_json(pipeline, session_factory, monkeypatch, capsys):
    from app import cli

    pipeline.run(Selection(source_ids=["ca_bc_pharmacare"]), RunOptions())
    monkeypatch.setattr(cli, "_session_factory", lambda: session_factory)
    assert cmd_coverage(argparse.Namespace(query=["dupilumab"], json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(source for source in payload["sources"] if source["source_id"] == "ca_bc_pharmacare")
    assert row["country"] == "CA"
    assert row["priced_products"] == 1
    assert "not necessarily currently effective" in payload["semantics"]


def test_coverage_parser_and_empty_query():
    args = build_parser().parse_args(["coverage", "--query", "Dupixent", "--query", "dupilumab", "--json"])
    assert args.query == ["Dupixent", "dupilumab"]
    assert args.json is True


def test_cima_reconciles_exact_national_codes_without_copying_prices(pipeline, session_factory):
    from app.db.models import Product
    from sqlalchemy import select

    pipeline.run(Selection(source_ids=["es_cima", "es_nomenclator"]), RunOptions())
    with session_factory() as session:
        before = product_coverage(session, ["dupixent"], ["es_cima"])[0]
        assert before["matches"][0]["national_code_reconciliation"] == {
            "source_id": "es_nomenclator", "product_present": False, "positive_price_present": False,
        }
        product = session.scalar(select(Product).where(Product.source_id == "es_nomenclator", Product.product_code == "650004"))
        assert product is not None
        product.product_code = "727309"
        session.flush()
        after = product_coverage(session, ["dupixent"], ["es_cima"])[0]
        assert after["matches"][0]["national_code_reconciliation"] == {
            "source_id": "es_nomenclator", "product_present": True, "positive_price_present": True,
        }
        assert after["priced_products"] == after["price_observations"] == 0
        assert after["matches"][0]["prices"] == []
