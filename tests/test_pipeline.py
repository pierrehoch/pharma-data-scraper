"""End-to-end pipeline tests: download → snapshot → parse → normalize → validate → publish (offline)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.enums import JobStatus
from app.core.pipeline import RunOptions, Selection
from app.db.models import Job, JobError, Price, Product, RawSnapshotRecord, SchemaBaseline
from tests.conftest import fx


def count(session, model, **filters) -> int:  # noqa: ANN001
    stmt = select(func.count()).select_from(model)
    for k, v in filters.items():
        stmt = stmt.where(getattr(model, k) == v)
    return session.scalar(stmt)


def test_ingest_all_with_limit(pipeline, session_factory):  # noqa: ANN001
    from app.adapters import get_adapter_class
    from app.core.enums import AdapterStatus

    summary = pipeline.run(Selection(all=True), RunOptions(limit=5, command="test"))
    by_source = {j.source_id: j for j in summary.jobs}
    enabled = {sid: cfg for sid, cfg in pipeline.registry.items() if cfg.enabled}
    assert set(by_source) == set(enabled)
    assert by_source["cn_nhsa"].status == JobStatus.UNSUPPORTED
    assert by_source["de_festbetrag"].status == JobStatus.PARTIAL_SUCCESS
    assert by_source["de_amnog"].status == JobStatus.PARTIAL_SUCCESS
    assert by_source["ca_on_odb"].status == JobStatus.SUCCESS
    assert by_source["ca_bc_pharmacare"].status == JobStatus.SUCCESS
    assert all(job.status in {JobStatus.SUCCESS, JobStatus.PARTIAL_SUCCESS, JobStatus.UNSUPPORTED} for job in by_source.values()), by_source
    for sid in ("fr_bdpm", "us_nadac", "it_aifa", "jp_nhi", "us_asp", "es_nomenclator", "uk_dmd", "uk_drug_tariff", "br_cmed", "ca_hc_dpd"):
        assert by_source[sid].status == JobStatus.SUCCESS, (sid, by_source[sid])
        assert 0 < by_source[sid].metrics["records_parsed"] <= 5
    totals = summary.totals()
    assert totals["sources_unsupported"] == 1
    assert totals["sources_partially_supported"] == sum(
        get_adapter_class(cfg.adapter).status == AdapterStatus.PARTIALLY_SUPPORTED for cfg in enabled.values()
    )
    with session_factory() as s:
        assert count(s, Price) > 0
        assert count(s, Product, source_id="de_amnog") > 0  # sources without prices still produce products
        assert count(s, Job) == len(enabled)
        # dashboard reflects exactly the CLI figures
        jobs = list(s.scalars(select(Job).where(Job.run_id == summary.run_id)))
        assert sum(j.records_parsed for j in jobs) == totals["records_parsed"]
        assert sum(j.records_published for j in jobs) == totals["records_published"]


def test_limit_and_product_filter(pipeline, session_factory):  # noqa: ANN001
    out = pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions(limit=2))
    assert out.jobs[0].metrics["records_parsed"] == 2
    out = pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions(product_ids=["3400936963504"]))
    assert out.jobs[0].metrics["records_parsed"] == 1


def test_dry_run_leaves_database_untouched(pipeline, session_factory):  # noqa: ANN001
    out = pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions(limit=10, dry_run=True))
    job = out.jobs[0]
    assert job.status == JobStatus.SUCCESS
    assert job.metrics["records_normalized"] > 0 and job.metrics["records_published"] > 0
    with session_factory() as s:
        assert count(s, Product) == 0
        assert count(s, Price) == 0
        assert count(s, SchemaBaseline) == 0
        assert count(s, RawSnapshotRecord) == 1  # raw data is always kept
        stages = {st["name"]: st for st in s.get(Job, job.job_id).stages}
        assert stages["publish"]["status"] == "skipped"


def test_history_is_append_only(pipeline, session_factory, http):  # noqa: ANN001
    pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions())
    with session_factory() as s:
        first_count = count(s, Price)
    # same edition, same content → no new observation
    pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions(force_download=True))
    with session_factory() as s:
        assert count(s, Price) == first_count
    # new edition with a changed price → new observation, old one kept but not current
    cip = fx("fr_bdpm", "CIS_CIP_bdpm.txt").read_bytes().replace(b"24,34\t25,36\t1,02", b"24,90\t25,92\t1,02")
    page = fx("fr_bdpm", "telechargement.html").read_text(encoding="utf-8").replace("29/09/2026", "29/10/2026")
    http.override(r"/download/file/CIS_CIP_bdpm\.txt$", cip)
    http.override(r"telechargement$", page.encode())
    pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions())
    with session_factory() as s:
        obs = list(
            s.scalars(
                select(Price)
                .join(Product)
                .where(Product.product_code == "3400949497294", Price.price_variant == "incl_dispensing_fee")
                .order_by(Price.id)
            )
        )
        assert [Decimal(str(o.amount)) for o in obs] == [Decimal("25.36"), Decimal("25.92")]
        assert [o.is_current for o in obs] == [False, True]
        assert obs[0].superseded_by_id == obs[1].id
        assert count(s, Price) == first_count + 2  # both price variants of that pack changed
        assert count(s, Product, product_code="3400949497294") == 1


def test_schema_change_blocks_publication(pipeline, session_factory, http):  # noqa: ANN001
    bad = fx("fr_bdpm", "CIS_CIP_bdpm.txt").read_bytes().replace(b"\n", b"\textra\n")
    http.override(r"/download/file/CIS_CIP_bdpm\.txt$", bad)
    out = pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions())
    assert out.jobs[0].status == JobStatus.SCHEMA_CHANGED
    with session_factory() as s:
        assert count(s, Price) == 0
        errors = list(s.scalars(select(JobError).where(JobError.error_type == "SCHEMA_CHANGED")))
        assert errors and "Expected columns: 13 – Actual columns: 14" in errors[0].message
        assert count(s, RawSnapshotRecord) == 1


def test_schema_baseline_detects_renamed_column(pipeline, session_factory, http):  # noqa: ANN001
    assert pipeline.run(Selection(source_ids=["it_aifa"]), RunOptions()).jobs[0].status == JobStatus.SUCCESS
    renamed = fx("it_aifa", "class_a.csv").read_bytes().replace(b"Codice Gruppo Equivalenza", b"Gruppo di equivalenza", 1)
    http.override(r"Classe_A_per_principio_attivo_31-05-2026\.csv$", renamed)
    out = pipeline.run(Selection(source_ids=["it_aifa"]), RunOptions(force_download=True))
    assert out.jobs[0].status == JobStatus.SCHEMA_CHANGED
    out = pipeline.run(Selection(source_ids=["it_aifa"]), RunOptions(force_download=True, accept_schema_change=True))
    assert out.jobs[0].status == JobStatus.SUCCESS


def test_resume_after_interruption(pipeline, session_factory, monkeypatch):  # noqa: ANN001
    from app.core import publisher as pub

    original = pub.Publisher.publish
    calls = {"n": 0}

    def flaky(self, record):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 5:
            raise KeyboardInterrupt
        return original(self, record)

    monkeypatch.setattr(pub.Publisher, "publish", flaky)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions())
    with session_factory() as s:
        job = s.scalars(select(Job)).one()
        assert job.status == JobStatus.CANCELLED.value
        assert job.checkpoint["published_upto"] == 3  # batch size 3 committed
    monkeypatch.setattr(pub.Publisher, "publish", original)
    out = pipeline.run(Selection(source_ids=["fr_bdpm"]), RunOptions(resume=True))
    assert out.jobs[0].status == JobStatus.SUCCESS
    with session_factory() as s:
        resumed = s.get(Job, out.jobs[0].job_id)
        assert resumed.resumed_from_job_id is not None
        stages = {st["name"]: st for st in resumed.stages}
        assert stages["publish"]["metrics"]["skipped_resume"] == 3
        assert stages["discovery"]["status"] == "skipped"
        assert count(s, Product, source_id="fr_bdpm") == resumed.records_parsed


def test_china_unsupported_then_manual_import(pipeline, settings, session_factory):  # noqa: ANN001
    out = pipeline.run(Selection(source_ids=["cn_nhsa"]), RunOptions())
    assert out.jobs[0].status == JobStatus.UNSUPPORTED
    folder = Path(settings.manual_import_dir) / "cn_nhsa"
    folder.mkdir(parents=True)
    (folder / "vbp.csv").write_bytes(fx("cn_nhsa", "vbp_sample.csv").read_bytes())
    out = pipeline.run(Selection(source_ids=["cn_nhsa"]), RunOptions())
    assert out.jobs[0].status == JobStatus.SUCCESS
    with session_factory() as s:
        assert count(s, Price, source_id="cn_nhsa") == 1


def test_missing_trud_key_is_explicit(pipeline, settings, session_factory):  # noqa: ANN001
    settings.trud_api_key = None
    out = pipeline.run(Selection(source_ids=["uk_dmd"]), RunOptions())
    assert out.jobs[0].status == JobStatus.FAILED
    with session_factory() as s:
        err = s.scalars(select(JobError).where(JobError.error_type == "MISSING_CONFIGURATION")).first()
        assert err is not None and "TRUD_API_KEY" in err.message


def test_secrets_are_not_stored(pipeline, settings, session_factory):  # noqa: ANN001
    from app.observability import register_secrets

    register_secrets([settings.trud_api_key])
    pipeline.run(Selection(source_ids=["uk_dmd"]), RunOptions())
    with session_factory() as s:
        job = s.scalars(select(Job)).one()
        assert settings.trud_api_key not in str(job.artifact)
        snap = s.scalars(select(RawSnapshotRecord)).one()
        meta = (Path(snap.storage_path) / "metadata.json").read_text()
        assert settings.trud_api_key not in meta


def test_download_error_is_explicit(pipeline, session_factory, http):  # noqa: ANN001
    from app.core.errors import DownloadError

    def boom():  # noqa: ANN202
        raise DownloadError("HTTP 503 when requesting https://www.aifa.gov.it/liste-farmaci-a-h")

    http.override(r"aifa\.gov\.it/liste-farmaci-a-h$", fx("it_aifa", "page.html"))
    http.routes.insert(0, type(http.routes[0])(r"aifa\.gov\.it/liste-farmaci-a-h$", boom))
    out = pipeline.run(Selection(source_ids=["it_aifa"]), RunOptions())
    assert out.jobs[0].status == JobStatus.FAILED
    with session_factory() as s:
        err = s.scalars(select(JobError).where(JobError.error_type == "DOWNLOAD_ERROR")).one()
        assert "503" in err.message and err.stage == "discovery"


def test_snapshot_reused_for_same_edition(pipeline, session_factory, http):  # noqa: ANN001
    pipeline.run(Selection(source_ids=["it_aifa"]), RunOptions())
    n_requests = len(http.requested)
    out = pipeline.run(Selection(source_ids=["it_aifa"]), RunOptions())
    # only the discovery page is fetched again; files come from the stored snapshot
    assert len(http.requested) - n_requests == 1
    with session_factory() as s:
        job = s.get(Job, out.jobs[0].job_id)
        assert {st["name"]: st for st in job.stages}["download"]["status"] == "skipped"
        assert count(s, RawSnapshotRecord) == 1
