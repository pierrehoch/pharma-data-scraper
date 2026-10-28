"""Job orchestration. Knows only the SourceAdapter contract – never a specific source."""

from __future__ import annotations

import logging
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters import get_adapter_class
from app.config import Settings
from app.core.adapter import AdapterContext, SourceAdapter
from app.core.enums import AdapterStatus, ErrorType, JobMode, JobStatus, Severity, StageStatus
from app.core.errors import (
    MissingConfigurationError,
    PipelineError,
    SchemaChangedError,
    SourceUnsupportedError,
)
from app.core.http import HttpClient, HttpClientProtocol
from app.core.publisher import Publisher
from app.core.schema import check_expected, compare_with_baseline
from app.core.snapshots import SnapshotStore
from app.core.types import Issue, ParsedDataset, RawSnapshot, RunHints
from app.db.models import IngestRun, Job, JobError, RawSnapshotRecord, SchemaBaseline
from app.observability import Progress, get_logger, human_duration, log_context, redact
from app.registry import SourceConfig, load_registry, normalize_country, sync_registry

log = get_logger(__name__)

STAGES = ["discovery", "download", "snapshot", "parse", "schema_check", "normalization", "validation", "publish"]
MAX_STORED_ROW_ISSUES = 2000


@dataclass
class Selection:
    source_ids: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    all: bool = False


@dataclass
class RunOptions:
    limit: int | None = None
    product_ids: list[str] | None = None
    dry_run: bool = False
    resume: bool = False
    force_download: bool = False
    accept_schema_change: bool = False
    command: str | None = None


@dataclass
class JobOutcome:
    job_id: str
    source_id: str
    status: JobStatus
    adapter_status: str
    metrics: dict[str, Any]
    error_count: int
    warning_count: int


@dataclass
class RunSummary:
    run_id: str
    jobs: list[JobOutcome]

    def totals(self) -> dict[str, int]:
        keys = [
            "records_downloaded",
            "records_parsed",
            "records_normalized",
            "records_published",
            "records_failed",
            "prices_published",
            "validation_warnings",
        ]
        totals = {k: sum(j.metrics.get(k, 0) or 0 for j in self.jobs) for k in keys}
        totals["sources_discovered"] = len(self.jobs)
        totals["sources_downloaded"] = sum(1 for j in self.jobs if j.metrics.get("downloaded"))
        totals["sources_partially_supported"] = sum(
            1 for j in self.jobs if j.adapter_status == AdapterStatus.PARTIALLY_SUPPORTED
        )
        totals["sources_unsupported"] = sum(1 for j in self.jobs if j.status == JobStatus.UNSUPPORTED)
        totals["sources_failed"] = sum(1 for j in self.jobs if j.status in (JobStatus.FAILED, JobStatus.SCHEMA_CHANGED))
        totals["errors"] = sum(j.error_count for j in self.jobs)
        return totals


class StageTracker:
    def __init__(self, job: Job):
        self.job = job
        self.stages: list[dict[str, Any]] = [{"name": s, "status": StageStatus.PENDING.value} for s in STAGES]

    def _get(self, name: str) -> dict[str, Any]:
        return next(s for s in self.stages if s["name"] == name)

    def _sync(self) -> None:
        self.job.stages = [dict(s) for s in self.stages]

    def set(self, name: str, status: StageStatus, *, message: str | None = None, **metrics: Any) -> None:
        stage = self._get(name)
        stage["status"] = status.value
        if message:
            stage["message"] = message
        stage.setdefault("metrics", {}).update(metrics)
        self._sync()

    @contextmanager
    def stage(self, name: str) -> Iterator[dict[str, Any]]:
        stage = self._get(name)
        stage["started_at"] = datetime.now(timezone.utc).isoformat()
        stage.setdefault("metrics", {})
        t0 = time.perf_counter()
        with log_context(stage=name):
            log.info("▶ %s started", name)
            try:
                yield stage["metrics"]
            except BaseException as exc:
                stage["status"] = StageStatus.FAILED.value
                stage["message"] = redact(str(exc))[:500]
                log.info("✗ %s failed after %s", name, human_duration(time.perf_counter() - t0))
                raise
            else:
                if stage["status"] == StageStatus.PENDING.value:
                    stage["status"] = StageStatus.SUCCESS.value
                log.info(
                    "%s %s %s in %s%s",
                    {StageStatus.SUCCESS.value: "✓", StageStatus.SKIPPED.value: "–"}.get(stage["status"], "⚠"),
                    name,
                    stage["status"],
                    human_duration(time.perf_counter() - t0),
                    _metrics_summary(stage),
                )
            finally:
                stage["duration_seconds"] = round(time.perf_counter() - t0, 3)
                self._sync()


def _metrics_summary(stage: dict[str, Any]) -> str:
    scalars = {k: v for k, v in (stage.get("metrics") or {}).items() if isinstance(v, (int, float, str, bool)) and k != "snapshot_id"}
    text = ", ".join(f"{k}={v:,}" if isinstance(v, int) and not isinstance(v, bool) else f"{k}={v}" for k, v in scalars.items())
    if stage.get("message"):
        text = f"{text} – {stage['message']}" if text else stage["message"]
    return f" · {text}" if text else ""


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        http: HttpClientProtocol | None = None,
        store: SnapshotStore | None = None,
        registry: dict[str, SourceConfig] | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.http = http or HttpClient(
            user_agent=settings.http_user_agent, timeout=settings.http_timeout, delay_seconds=settings.http_delay_seconds
        )
        self.store = store or SnapshotStore(settings.raw_storage_dir)
        self.registry = registry if registry is not None else load_registry(settings.sources_file)

    # ------------------------------------------------------------------ selection

    def select_sources(self, selection: Selection) -> list[SourceConfig]:
        if selection.source_ids:
            unknown = [s for s in selection.source_ids if s not in self.registry]
            if unknown:
                raise ValueError(f"Unknown source(s): {unknown}. Known: {sorted(self.registry)}")
            return [self.registry[s] for s in selection.source_ids]
        if selection.countries:
            wanted = {normalize_country(c) for c in selection.countries}
            chosen = [c for c in self.registry.values() if c.country in wanted and c.enabled]
            if not chosen:
                raise ValueError(f"No enabled source for countries {sorted(wanted)}")
            return chosen
        if selection.all:
            return [c for c in self.registry.values() if c.enabled]
        raise ValueError("Select sources with --source, --country or --all")

    # ------------------------------------------------------------------ run

    def run(self, selection: Selection, options: RunOptions) -> RunSummary:
        configs = self.select_sources(selection)
        with self.session_factory() as session:
            sync_registry(session, self.registry)
            run = IngestRun(
                command=options.command,
                mode=self._mode(options).value,
                limit=options.limit,
                selection={"sources": [c.source_id for c in configs], "product_ids": options.product_ids},
                status="running",
            )
            session.add(run)
            session.commit()
            run_id = run.id
        outcomes = []
        log.info(
            "Run %s: %d source(s) %s (mode=%s%s)",
            run_id[:8],
            len(configs),
            [c.source_id for c in configs],
            self._mode(options).value,
            f", limit={options.limit}" if options.limit else "",
        )
        try:
            for i, cfg in enumerate(configs, start=1):
                log.info("──── [%d/%d] %s – %s ────", i, len(configs), cfg.source_id, cfg.name)
                outcomes.append(self.run_source(cfg, options, run_id=run_id))
        finally:
            summary = RunSummary(run_id=run_id, jobs=outcomes)
            with self.session_factory() as session:
                run = session.get(IngestRun, run_id)
                run.finished_at = datetime.now(timezone.utc)
                run.summary = {
                    "totals": summary.totals(),
                    "jobs": [{"job_id": o.job_id, "source_id": o.source_id, "status": o.status.value} for o in outcomes],
                }
                run.status = "finished" if len(outcomes) == len(configs) else "interrupted"
                session.commit()
        return summary

    @staticmethod
    def _mode(options: RunOptions) -> JobMode:
        if options.dry_run:
            return JobMode.DRY_RUN
        if options.resume:
            return JobMode.RESUME
        return JobMode.NORMAL

    def run_source(self, cfg: SourceConfig, options: RunOptions, *, run_id: str | None = None) -> JobOutcome:
        adapter_cls = get_adapter_class(cfg.adapter)
        session = self.session_factory()
        issues: list[Issue] = []
        stored_issue_count = 0
        metrics: dict[str, Any] = {"downloaded": False}

        job = Job(
            run_id=run_id,
            source_id=cfg.source_id,
            status=JobStatus.RUNNING.value,
            mode=self._mode(options).value,
            limit=options.limit,
            product_ids=options.product_ids,
            adapter_status=adapter_cls.status.value,
            checkpoint={},
        )
        session.add(job)
        session.commit()
        job_id = job.id
        tracker = StageTracker(job)

        def persist_issues() -> None:
            nonlocal stored_issue_count
            pending = issues[stored_issue_count:]
            row_level = sum(1 for i in issues[:stored_issue_count] if i.row_ref)
            for issue in pending:
                if issue.row_ref and row_level >= MAX_STORED_ROW_ISSUES:
                    continue
                row_level += 1 if issue.row_ref else 0
                session.add(
                    JobError(
                        job_id=job_id,
                        source_id=cfg.source_id,
                        stage=issue.stage,
                        severity=issue.severity.value,
                        error_type=issue.error_type.value,
                        message=redact(issue.message)[:4000],
                        row_ref=issue.row_ref,
                        details={k: (redact(v) if isinstance(v, str) else v) for k, v in issue.details.items()},
                    )
                )
            stored_issue_count = len(issues)

        hints = RunHints(
            limit=options.limit,
            product_ids=set(options.product_ids) if options.product_ids else None,
            dry_run=options.dry_run,
        )
        ctx = AdapterContext(settings=self.settings, http=self.http, store=self.store, hints=hints)
        adapter: SourceAdapter = adapter_cls(cfg, ctx)
        status = JobStatus.FAILED
        snapshot: RawSnapshot | None = None

        with log_context(job_id=job_id, source_id=cfg.source_id):
            log.info("Job %s started (mode=%s, limit=%s, adapter=%s)", job_id[:8], job.mode, options.limit, adapter_cls.status.value)
            try:
                if adapter_cls.status == AdapterStatus.PARTIALLY_SUPPORTED and adapter_cls.limitations:
                    issues.append(
                        Issue(ErrorType.UNSUPPORTED_SOURCE, Severity.INFO, f"Partially supported source: {adapter_cls.limitations}", stage="discovery")
                    )
                config_issues = adapter.check_configuration()
                if config_issues:
                    issues.extend(config_issues)
                    raise MissingConfigurationError("; ".join(i.message for i in config_issues))

                resume_job = self._find_resumable(session, cfg.source_id, job_id) if options.resume else None
                if options.resume and resume_job is None:
                    log.info("--resume: no interrupted job found for this source, running it normally")
                    issues.append(Issue(ErrorType.INTERNAL_ERROR, Severity.INFO, "--resume: no interrupted job found, starting a normal run", stage="discovery"))
                if resume_job is not None:
                    log.info(
                        "Resuming job %s from record %s (snapshot %s)",
                        resume_job.id[:8],
                        (resume_job.checkpoint or {}).get("published_upto", 0),
                        (resume_job.checkpoint or {}).get("snapshot_id"),
                    )
                    job.resumed_from_job_id = resume_job.id
                    hints.limit = resume_job.limit if options.limit is None else options.limit
                    job.limit = hints.limit
                    if resume_job.product_ids and not options.product_ids:
                        hints.product_ids = set(resume_job.product_ids)
                        job.product_ids = resume_job.product_ids

                # ---------------------------------------------------------- discovery + download
                with tracker.stage("discovery") as m:
                    if resume_job is not None:
                        tracker.set("discovery", StageStatus.SKIPPED, message=f"Resuming job {resume_job.id}")
                        artifact = None
                    else:
                        artifact = adapter.discover()
                        log.info(
                            "Found edition %s (published %s): %s",
                            artifact.edition or "?",
                            artifact.published_at or "unknown",
                            ", ".join(f"{f.role}={f.filename}" for f in artifact.files[:6]) + (" …" if len(artifact.files) > 6 else ""),
                        )
                        for note in artifact.notes:
                            log.info("Discovery note: %s", redact(note))
                        m["files"] = [f.filename for f in artifact.files]
                        m["published_at"] = artifact.published_at.isoformat() if artifact.published_at else None
                        job.artifact = {
                            "files": [{"url": redact(f.url), "role": f.role, "filename": f.filename} for f in artifact.files],
                            "published_at": m["published_at"],
                            "edition": artifact.edition,
                            "discovered_from": redact(artifact.discovered_from or ""),
                            "notes": artifact.notes,
                        }
                session.commit()

                with tracker.stage("download") as m:
                    snapshot = self._obtain_snapshot(session, adapter, artifact, resume_job, options, ctx)
                    m["snapshot_id"] = snapshot.id
                    m["reused"] = snapshot.reused
                    m["bytes"] = sum(f.size for f in snapshot.files)
                    if snapshot.reused:
                        tracker.set("download", StageStatus.SKIPPED, message=f"Reused snapshot {snapshot.id}")
                    metrics["downloaded"] = True

                with tracker.stage("snapshot") as m:
                    self._record_snapshot(session, snapshot)
                    job.snapshot_id = snapshot.id
                    job.checkpoint = {**(job.checkpoint or {}), "snapshot_id": snapshot.id, "snapshot_dir": str(snapshot.directory)}
                    m["files"] = [{"role": f.role, "sha256": f.sha256, "size": f.size} for f in snapshot.files]
                    if snapshot.duplicate_of:
                        m["duplicate_of"] = snapshot.duplicate_of
                persist_issues()
                session.commit()

                # ---------------------------------------------------------- parse + schema
                with tracker.stage("parse") as m:
                    dataset = adapter.parse(snapshot)
                    issues.extend(dataset.issues)
                    total = dataset.total_rows if dataset.total_rows is not None else len(dataset.records)
                    m.update(total_rows=total, records=len(dataset.records), row_errors=dataset.row_errors, truncated=dataset.truncated)
                    job.records_total_in_source = None if dataset.truncated else total
                    if dataset.row_errors:
                        tracker.set("parse", StageStatus.WARNING, message=f"{dataset.row_errors} malformed rows")

                with tracker.stage("schema_check") as m:
                    schema_issues = self._check_schema(session, adapter, dataset)
                    m["issues"] = len(schema_issues)
                    if schema_issues and options.accept_schema_change:
                        for i in schema_issues:
                            i.severity = Severity.WARNING
                            i.message = f"[accepted with --accept-schema-change] {i.message}"
                        tracker.set("schema_check", StageStatus.WARNING, message="Schema change accepted by operator")
                        issues.extend(schema_issues)
                    elif schema_issues:
                        issues.extend(schema_issues)
                        raise SchemaChangedError(
                            f"{len(schema_issues)} schema change(s) detected – data not published: "
                            + "; ".join(i.message for i in schema_issues[:3])
                        )

                scoped = self._apply_scope(adapter, dataset, hints)
                if hints.product_ids:
                    job.records_downloaded = len(scoped.records)
                else:
                    job.records_downloaded = min(total, hints.limit) if hints.limit else total
                job.records_parsed = len(scoped.records)
                job.records_failed = dataset.row_errors if not hints.limit else 0

                # ---------------------------------------------------------- normalization + validation
                with tracker.stage("normalization") as m:
                    normalized = adapter.normalize(scoped)
                    issues.extend(normalized.issues)
                    job.records_normalized = len(normalized.records)
                    job.records_failed += normalized.failed_rows
                    m.update(records=len(normalized.records), failed=normalized.failed_rows)
                    if normalized.failed_rows:
                        tracker.set("normalization", StageStatus.WARNING, message=f"{normalized.failed_rows} rows failed")

                with tracker.stage("validation") as m:
                    report = adapter.validate(normalized)
                    issues.extend(report.issues)
                    job.validation_warnings = len(report.warnings)
                    m.update(warnings=len(report.warnings), errors=len(report.errors))
                    if report.warnings:
                        tracker.set("validation", StageStatus.WARNING, message=f"{len(report.warnings)} warnings")
                persist_issues()
                session.commit()

                # ---------------------------------------------------------- publish
                self._publish(session, cfg, job, tracker, adapter, snapshot, normalized, resume_job, options, issues, persist_issues)

                job = session.get(Job, job_id)
                if not options.dry_run:
                    self._update_baselines(session, cfg.source_id, dataset, snapshot)
                status = JobStatus.SUCCESS
                if job.records_failed or adapter_cls.status == AdapterStatus.PARTIALLY_SUPPORTED:
                    status = JobStatus.PARTIAL_SUCCESS
                session.commit()
                outcome = self._finalize(session, job_id, tracker, status, issues, persist_issues, metrics, rollback=False)
                session.close()
                return outcome
            except SourceUnsupportedError as exc:
                status = JobStatus.UNSUPPORTED
                issues.append(Issue(exc.error_type, Severity.INFO, exc.message, stage="discovery", details=exc.details))
                tracker.set("discovery", StageStatus.SKIPPED, message=exc.message)
            except SchemaChangedError as exc:
                status = JobStatus.SCHEMA_CHANGED
                if not any(i.error_type == ErrorType.SCHEMA_CHANGED and i.severity == Severity.ERROR for i in issues):
                    stage = next((s["name"] for s in tracker.stages if s["status"] == StageStatus.FAILED.value), "parse")
                    issues.append(Issue(ErrorType.SCHEMA_CHANGED, Severity.ERROR, exc.message, stage=stage, details=exc.details))
                log.error(exc.message)
            except PipelineError as exc:
                status = JobStatus.FAILED
                stage = next((s["name"] for s in tracker.stages if s["status"] == StageStatus.FAILED.value), None)
                issues.append(Issue(exc.error_type, Severity.ERROR, exc.message, stage=stage, details=exc.details))
                log.error("%s: %s", exc.error_type, exc.message)
            except KeyboardInterrupt:
                status = JobStatus.CANCELLED
                issues.append(
                    Issue(
                        ErrorType.INTERNAL_ERROR,
                        Severity.WARNING,
                        "Job cancelled by operator (interrupt). Use --resume to continue from the last committed batch.",
                    )
                )
                self._finalize(session, job_id, tracker, status, issues, persist_issues, metrics, rollback=True)
                session.close()
                raise
            except Exception as exc:  # noqa: BLE001
                status = JobStatus.FAILED
                stage = next((s["name"] for s in tracker.stages if s["status"] == StageStatus.FAILED.value), None)
                issues.append(
                    Issue(
                        ErrorType.INTERNAL_ERROR,
                        Severity.ERROR,
                        f"Unexpected {type(exc).__name__} in stage '{stage}': {exc}",
                        stage=stage,
                        details={"traceback": traceback.format_exc(limit=8)},
                    )
                )
                log.exception("Unexpected error")
            outcome = self._finalize(session, job_id, tracker, status, issues, persist_issues, metrics, rollback=True)
            session.close()
            return outcome

    # ------------------------------------------------------------------ helpers

    def _find_resumable(self, session: Session, source_id: str, current_job_id: str) -> Job | None:
        stmt = (
            select(Job)
            .where(
                Job.source_id == source_id,
                Job.id != current_job_id,
                Job.mode != JobMode.DRY_RUN.value,
                Job.status.in_([JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.RUNNING.value]),
            )
            .order_by(Job.started_at.desc())
        )
        for candidate in session.scalars(stmt).all()[:5]:
            cp = candidate.checkpoint or {}
            if cp.get("snapshot_dir") and Path(cp["snapshot_dir"]).is_dir() and not cp.get("resumed_by"):
                return candidate
        return None

    def _obtain_snapshot(
        self,
        session: Session,
        adapter: SourceAdapter,
        artifact,  # noqa: ANN001
        resume_job: Job | None,
        options: RunOptions,
        ctx: AdapterContext,
    ) -> RawSnapshot:
        if resume_job is not None:
            snapshot = self.store.load(Path(resume_job.checkpoint["snapshot_dir"]))
            return snapshot
        latest = session.scalars(
            select(RawSnapshotRecord)
            .where(RawSnapshotRecord.source_id == adapter.source_id)
            .order_by(RawSnapshotRecord.retrieved_at.desc())
        ).first()
        if latest is not None:
            ctx.previous_snapshot_id = latest.id
            ctx.previous_snapshot_hashes = [f["sha256"] for f in latest.files or []]
            same_edition = (
                artifact.published_at is not None
                and latest.published_at == artifact.published_at
                and not latest.partial
                and not artifact.partial
                and sorted(redact(f.url) for f in artifact.files) == sorted(f.get("discovered_url") or "" for f in latest.files or [])
            )
            if same_edition and not options.force_download and Path(latest.storage_path).is_dir():
                log.info("Edition %s already downloaded in snapshot %s: reusing it", artifact.published_at, latest.id)
                return self.store.load(Path(latest.storage_path))
        return adapter.download(artifact)

    def _record_snapshot(self, session: Session, snapshot: RawSnapshot) -> None:
        if session.get(RawSnapshotRecord, snapshot.id) is not None:
            return
        primary = snapshot.primary
        session.add(
            RawSnapshotRecord(
                id=snapshot.id,
                source_id=snapshot.source_id,
                retrieved_at=snapshot.retrieved_at,
                published_at=snapshot.published_at,
                edition=snapshot.edition,
                source_url=primary.source_url,
                filename=primary.filename,
                content_type=primary.content_type,
                sha256=primary.sha256,
                size=primary.size,
                parser_version=snapshot.parser_version,
                storage_path=str(snapshot.directory),
                files=[
                    {
                        "role": f.role,
                        "filename": f.filename,
                        "sha256": f.sha256,
                        "size": f.size,
                        "source_url": f.source_url,
                        "discovered_url": f.requested_url,
                    }
                    for f in snapshot.files
                ],
                partial=snapshot.partial,
                duplicate_of=snapshot.duplicate_of,
            )
        )
        session.flush()

    def _check_schema(self, session: Session, adapter: SourceAdapter, dataset: ParsedDataset) -> list[Issue]:
        issues: list[Issue] = []
        expected = {e.role: e for e in adapter.expected_schemas()}
        for profile in dataset.profiles:
            exp = expected.get(profile.role)
            if exp is not None:
                issues.extend(check_expected(profile, exp, truncated=dataset.truncated))
            baseline = session.scalars(
                select(SchemaBaseline).where(SchemaBaseline.source_id == adapter.source_id, SchemaBaseline.role == profile.role)
            ).first()
            if baseline is not None:
                issues.extend(
                    compare_with_baseline(
                        profile,
                        {"columns": baseline.columns, "column_types": baseline.column_types, "row_count": baseline.row_count},
                        truncated=dataset.truncated,
                        typed_columns=exp.typed_columns if exp else None,
                    )
                )
        return issues

    def _update_baselines(self, session: Session, source_id: str, dataset: ParsedDataset, snapshot: RawSnapshot) -> None:
        for profile in dataset.profiles:
            baseline = session.scalars(
                select(SchemaBaseline).where(SchemaBaseline.source_id == source_id, SchemaBaseline.role == profile.role)
            ).first()
            if baseline is None:
                baseline = SchemaBaseline(source_id=source_id, role=profile.role)
                session.add(baseline)
            baseline.columns = profile.columns
            baseline.column_types = profile.column_types
            if not dataset.truncated:
                baseline.row_count = profile.row_count
            baseline.snapshot_id = snapshot.id
            baseline.accepted_at = datetime.now(timezone.utc)
        session.commit()

    @staticmethod
    def _apply_scope(adapter: SourceAdapter, dataset: ParsedDataset, hints: RunHints) -> ParsedDataset:
        records = dataset.records
        if hints.product_ids:
            records = [r for r in records if adapter.record_matches_product_ids(r, hints.product_ids)]
        if hints.limit is not None:
            records = records[: hints.limit]
        return ParsedDataset(
            source_id=dataset.source_id,
            snapshot=dataset.snapshot,
            records=records,
            profiles=dataset.profiles,
            truncated=dataset.truncated,
            total_rows=dataset.total_rows,
            row_errors=dataset.row_errors,
            issues=[],
            context=dataset.context,
        )

    def _publish(
        self,
        session: Session,
        cfg: SourceConfig,
        job: Job,
        tracker: StageTracker,
        adapter: SourceAdapter,
        snapshot: RawSnapshot,
        normalized,  # noqa: ANN001
        resume_job: Job | None,
        options: RunOptions,
        issues: list[Issue],
        persist_issues,  # noqa: ANN001
    ) -> None:
        publisher = Publisher(session, cfg, snapshot, job.id)
        base_failed = job.records_failed or 0
        start_index = 0
        if resume_job is not None:
            start_index = int((resume_job.checkpoint or {}).get("published_upto", 0))
        batch = max(1, self.settings.publish_batch_size)
        records = normalized.records
        job_id = job.id
        t0 = time.perf_counter()
        stage_publish = tracker._get("publish")
        stage_publish["started_at"] = datetime.now(timezone.utc).isoformat()
        skipped = 0
        progress = Progress("publish", total=len(records) - start_index, unit="records", logger=log, check_every=50)
        ctx = log_context(stage="publish")
        ctx.__enter__()
        log.info(
            "▶ publish started: %s records%s%s",
            f"{len(records) - start_index:,}",
            f" (skipping {start_index:,} already published)" if start_index else "",
            " [dry-run, will be rolled back]" if options.dry_run else f", commit every {batch} records",
        )
        try:
            for i, record in enumerate(records):
                if i < start_index:
                    skipped += 1
                    continue
                try:
                    with session.begin_nested():
                        publisher.publish(record)
                except Exception as exc:  # noqa: BLE001 - isolate row failures
                    publisher.stats.failed += 1
                    issues.append(
                        Issue(
                            ErrorType.INTERNAL_ERROR,
                            Severity.ERROR,
                            f"Record {record.row_ref} ({record.product.product_code}) could not be published: {type(exc).__name__}: {exc}",
                            stage="publish",
                            row_ref=record.row_ref,
                            details={"traceback": traceback.format_exc(limit=4)},
                        )
                    )
                    log.warning("Record %s (%s) failed: %s: %s", record.row_ref, record.product.product_code, type(exc).__name__, exc)
                s = publisher.stats
                progress.update(prices_new=s.prices_new, unchanged=s.prices_unchanged, failed=s.failed)
                if not options.dry_run and (i + 1) % batch == 0:
                    self._apply_publish_stats(job, publisher.stats, base_failed)
                    issues.extend(publisher.stats.issues)
                    publisher.stats.issues = []
                    job.checkpoint = {**(job.checkpoint or {}), "published_upto": i + 1}
                    stage_publish.setdefault("metrics", {})["progress"] = f"{i + 1}/{len(records)}"
                    tracker._sync()
                    persist_issues()
                    session.commit()
                    log.debug("Committed batch: checkpoint published_upto=%s", i + 1)
            progress.finish()
            issues.extend(publisher.stats.issues)
            publisher.stats.issues = []
            if options.dry_run:
                stats = publisher.stats
                session.rollback()  # database untouched in dry-run
                job = session.get(Job, job_id)
                tracker.job = job
                self._apply_publish_stats(job, stats, base_failed)
                tracker.set("publish", StageStatus.SKIPPED, message="dry-run: database not modified (rolled back)")
            else:
                self._apply_publish_stats(job, publisher.stats, base_failed)
                job.checkpoint = {**(job.checkpoint or {}), "published_upto": len(records)}
                if resume_job is not None:
                    resume_job.checkpoint = {**(resume_job.checkpoint or {}), "resumed_by": job.id}
        finally:
            ctx.__exit__(None, None, None)
            stats = publisher.stats
            stage_publish["duration_seconds"] = round(time.perf_counter() - t0, 3)
            stage_publish.setdefault("metrics", {}).update(
                records=stats.records_published,
                prices_new=stats.prices_new,
                prices_unchanged=stats.prices_unchanged,
                failed=stats.failed,
                skipped_resume=skipped,
            )
            if stage_publish["status"] == StageStatus.PENDING.value:
                stage_publish["status"] = (StageStatus.WARNING if stats.failed else StageStatus.SUCCESS).value
            tracker._sync()

    @staticmethod
    def _apply_publish_stats(job: Job, s, base_failed: int) -> None:  # noqa: ANN001
        job.records_published = s.records_published
        job.prices_published = s.prices_new
        job.prices_unchanged = s.prices_unchanged
        job.records_failed = base_failed + s.failed

    _JOB_METRIC_FIELDS = (
        "records_total_in_source",
        "records_downloaded",
        "records_parsed",
        "records_normalized",
        "records_failed",
        "validation_warnings",
        "snapshot_id",
        "resumed_from_job_id",
        "limit",
        "product_ids",
        "artifact",
    )

    def _finalize(
        self,
        session: Session,
        job_id: str,
        tracker: StageTracker,
        status: JobStatus,
        issues,  # noqa: ANN001
        persist_issues,  # noqa: ANN001
        metrics,  # noqa: ANN001
        *,
        rollback: bool,
    ) -> JobOutcome:
        if rollback:
            # Uncommitted writes are discarded; operational metrics captured so far are kept.
            captured = {f: tracker.job.__dict__.get(f) for f in self._JOB_METRIC_FIELDS}
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            job = session.get(Job, job_id)
            for f, v in captured.items():
                if v is not None:
                    setattr(job, f, v)
        else:
            job = session.get(Job, job_id)
        tracker.job = job
        row_issues = sum(1 for i in issues if i.row_ref)
        if row_issues > MAX_STORED_ROW_ISSUES:
            issues.append(
                Issue(
                    ErrorType.VALIDATION_ERROR,
                    Severity.INFO,
                    f"{row_issues - MAX_STORED_ROW_ISSUES} additional row-level issues were not stored (cap {MAX_STORED_ROW_ISSUES})",
                )
            )
        tracker._sync()
        for s in tracker.stages:
            if s["status"] == StageStatus.PENDING.value and status not in (JobStatus.SUCCESS, JobStatus.PARTIAL_SUCCESS):
                s["status"] = StageStatus.SKIPPED.value
        tracker._sync()
        job.status = status.value
        job.finished_at = datetime.now(timezone.utc)
        persist_issues()
        job.error_count = sum(1 for i in issues if i.severity == Severity.ERROR)
        job.validation_warnings = max(job.validation_warnings or 0, sum(1 for i in issues if i.error_type == ErrorType.VALIDATION_ERROR and i.severity == Severity.WARNING))
        session.commit()
        log.log(
            logging.WARNING if status in (JobStatus.FAILED, JobStatus.SCHEMA_CHANGED, JobStatus.CANCELLED) else logging.INFO,
            "Job %s finished: %s in %s · parsed=%s, normalized=%s, published=%s, prices_new=%s, prices_unchanged=%s, warnings=%s, errors=%s",
            job.id[:8],
            status.value.upper(),
            human_duration(job.duration_seconds or 0),
            job.records_parsed,
            job.records_normalized,
            job.records_published,
            job.prices_published,
            job.prices_unchanged,
            job.validation_warnings,
            job.error_count,
        )
        if job.error_count or job.validation_warnings:
            log.info("Details: /dashboard/jobs/%s", job.id)
        return JobOutcome(
            job_id=job.id,
            source_id=job.source_id,
            status=status,
            adapter_status=job.adapter_status or "",
            metrics={
                "downloaded": metrics.get("downloaded", False),
                "records_downloaded": job.records_downloaded,
                "records_parsed": job.records_parsed,
                "records_normalized": job.records_normalized,
                "records_published": job.records_published,
                "records_failed": job.records_failed,
                "prices_published": job.prices_published,
                "validation_warnings": job.validation_warnings,
            },
            error_count=job.error_count,
            warning_count=sum(1 for i in issues if i.severity == Severity.WARNING),
        )
