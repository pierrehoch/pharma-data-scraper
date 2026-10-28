"""REST API and dashboard tests (after an offline ingestion)."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.core.pipeline import RunOptions, Selection

FR_CIP = "3400949497294"


@pytest.fixture
def client(pipeline, settings):  # noqa: ANN001, ANN201
    pipeline.run(Selection(source_ids=["fr_bdpm", "it_aifa", "br_cmed", "cn_nhsa", "us_asp", "de_amnog"]), RunOptions(command="test"))
    from app.api.main import create_app

    return TestClient(create_app())


def test_sources(client):  # noqa: ANN001
    sources = client.get("/sources").json()
    assert {s["source_id"] for s in sources} >= {"fr_bdpm", "br_cmed", "cn_nhsa"}
    cn = next(s for s in sources if s["source_id"] == "cn_nhsa")
    assert cn["adapter_status"] == "manual_or_unsupported" and cn["last_run"]["status"] == "unsupported"
    fr = next(s for s in sources if s["source_id"] == "fr_bdpm")
    assert fr["products"] > 0 and fr["prices"] > 0 and "license" not in fr
    assert client.get("/sources?country=FR").json()[0]["source_id"] == "fr_bdpm"


def test_jobs_and_errors(client):  # noqa: ANN001
    jobs = client.get("/jobs?page_size=2").json()
    assert jobs["total"] == 6 and len(jobs["items"]) == 2 and jobs["pages"] == 3
    job = client.get(f"/jobs/{jobs['items'][0]['id']}").json()
    assert [s["name"] for s in job["stages"]][:3] == ["discovery", "download", "snapshot"]
    assert "matching" not in [s["name"] for s in job["stages"]]
    assert client.get("/jobs/unknown").status_code == 404
    assert client.get("/errors?severity=info").status_code == 200


def test_prices_filters(client):  # noqa: ANN001
    res = client.get("/prices?source_id=fr_bdpm&page_size=500").json()
    assert res["total"] > 0
    row = next(r for r in res["items"] if r["product_code"] == FR_CIP and r["price_variant"] == "incl_dispensing_fee")
    assert row["amount"] == "25.36" and row["currency"] == "EUR" and row["price_type"] == "retail"
    assert row["dispensing_fee"] == "1.02" and row["reimbursement_rate"] == "100%" and row["group_code"] == "60002283"
    assert isinstance(row["amount"], str)  # exact decimal preserved
    by_type = client.get("/prices?price_type=ex_factory&page_size=500").json()["items"]
    assert by_type and {r["price_type"] for r in by_type} == {"ex_factory"}
    assert {r["country"] for r in client.get("/prices?country=br&page_size=50").json()["items"]} == {"BR"}
    search = client.get(f"/prices?q={FR_CIP}").json()["items"]
    assert {r["product_code"] for r in search} == {FR_CIP}
    assert client.get("/prices?q=anastrozole&page_size=500").json()["total"] >= 2


def test_products_without_prices_are_listed(client):  # noqa: ANN001
    rows = client.get("/prices?source_id=de_amnog&page_size=50").json()["items"]
    assert rows and all(r["price_id"] is None and r["amount"] is None for r in rows)
    assert all(r["product_level"] == "regulatory_entry" and r["marketing_status"] for r in rows)


def test_history_toggle(client, pipeline, http):  # noqa: ANN001
    from tests.conftest import fx

    cip = fx("fr_bdpm", "CIS_CIP_bdpm.txt").read_bytes().replace(b"24,34\t25,36", b"25,00\t26,02")
    http.override(r"/download/file/CIS_CIP_bdpm\.txt$", cip)
    pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions(force_download=True))
    current = client.get(f"/prices?q={FR_CIP}").json()["items"]
    everything = client.get(f"/prices?q={FR_CIP}&current_only=false").json()["items"]
    assert len(everything) == len(current) + 2
    assert {r["amount"] for r in current if r["price_variant"] == "incl_dispensing_fee"} == {"26.02"}


def test_prices_csv(client):  # noqa: ANN001
    from app.api.queries import DATA_COLUMNS

    r = client.get("/prices.csv?source_id=fr_bdpm")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert list(rows[0]) == DATA_COLUMNS and len(set(DATA_COLUMNS)) == len(DATA_COLUMNS)
    assert any(row["product_code"] == FR_CIP and row["amount"] == "25.36" for row in rows)


def test_product_detail(client):  # noqa: ANN001
    pid = client.get(f"/prices?q={FR_CIP}").json()["items"][0]["product_id"]
    detail = client.get(f"/products/{pid}").json()
    assert detail["product_code"] == FR_CIP and detail["brand_name"] == "ANASTROZOLE ACCORD"
    assert {p["price_variant"] for p in detail["prices"]} == {"incl_dispensing_fee", "excl_dispensing_fee"}
    assert client.get("/products/999999").status_code == 404


def test_removed_endpoints(client):  # noqa: ANN001
    for url in ("/review-queue", "/coverage", "/licenses", "/compare?a=1&b=2", "/canonical-search?q=ab", "/exports/prices"):
        assert client.get(url).status_code == 404, url


def test_dashboard_pages_render(client):  # noqa: ANN001
    for url in ("/dashboard", "/dashboard/jobs", "/dashboard/errors?severity=warning", "/dashboard/sources/fr_bdpm",
                "/dashboard/data", "/dashboard/data?source_id=br_cmed&history=true", "/dashboard/data?q=anastrozole&price_type=retail"):
        r = client.get(url)
        assert r.status_code == 200, url
    overview = client.get("/dashboard").text
    assert "fr_bdpm" in overview and "UNSUPPORTED" in overview and "Licence" not in overview
    job_id = client.get("/jobs?source_id=fr_bdpm").json()["items"][0]["id"]
    page = client.get(f"/dashboard/jobs/{job_id}").text
    assert "Discovery" in page and "Publish" in page


def test_data_viewer(client):  # noqa: ANN001
    page = client.get(f"/dashboard/data?q={FR_CIP}").text
    assert FR_CIP in page and "25.36" in page and f"/prices.csv?q={FR_CIP}" in page
    amnog = client.get("/dashboard/data?source_id=de_amnog").text
    assert "No data matches" not in amnog
    pid = client.get(f"/prices?q={FR_CIP}").json()["items"][0]["product_id"]
    detail = client.get(f"/dashboard/data/{pid}").text
    assert "reimbursement_rate" in detail and "100%" in detail and "incl_dispensing_fee" in detail
    assert client.get("/dashboard/data/999999").status_code == 404
