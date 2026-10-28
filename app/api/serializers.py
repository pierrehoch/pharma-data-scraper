"""JSON serializers (Decimals as strings to preserve exact published values)."""

from __future__ import annotations

from typing import Any

from app.api.queries import PRICE_COLUMNS, PRODUCT_COLUMNS, quality_warnings
from app.api.queries import to_jsonable as j
from app.db.models import Job, JobError, Price, Product


def source_row_out(row: dict[str, Any]) -> dict[str, Any]:
    s = row["source"]
    last, ok = row["last_job"], row["last_success"]
    return {
        "source_id": s.id,
        "country": s.country,
        "name": s.name,
        "enabled": s.enabled,
        "adapter": s.adapter,
        "adapter_status": s.adapter_status,
        "parser_type": s.parser_type,
        "expected_frequency": s.expected_frequency,
        "source_url": s.source_url,
        "identifier_type": s.identifier_type,
        "coverage_scope": s.coverage_scope,
        "documentary_status": s.documentary_status,
        "limitations": s.notes,
        "last_run": job_summary_out(last) if last else None,
        "last_successful_run": j(ok.started_at) if ok else None,
        "schema_status": row["schema_status"],
        "next_expected_update": j(row["next_expected_update"]),
        "overdue": row["overdue"],
        "products": row["products"],
        "prices": row["prices"],
        "missing_configuration": row["missing_configuration"],
    }


def job_summary_out(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "run_id": job.run_id,
        "source_id": job.source_id,
        "status": job.status,
        "mode": job.mode,
        "limit": job.limit,
        "product_ids": job.product_ids,
        "started_at": j(job.started_at),
        "finished_at": j(job.finished_at),
        "duration_seconds": job.duration_seconds,
        "adapter_status": job.adapter_status,
        "records_total_in_source": job.records_total_in_source,
        "records_downloaded": job.records_downloaded,
        "records_parsed": job.records_parsed,
        "records_normalized": job.records_normalized,
        "records_failed": job.records_failed,
        "records_published": job.records_published,
        "prices_published": job.prices_published,
        "prices_unchanged": job.prices_unchanged,
        "validation_warnings": job.validation_warnings,
        "error_count": job.error_count,
        "snapshot_id": job.snapshot_id,
        "resumed_from_job_id": job.resumed_from_job_id,
    }


def job_detail_out(job: Job, errors: list[JobError]) -> dict[str, Any]:
    return {**job_summary_out(job), "stages": job.stages, "artifact": job.artifact, "checkpoint": job.checkpoint, "errors": [error_out(e) for e in errors]}


def error_out(e: JobError) -> dict[str, Any]:
    return {
        "id": e.id,
        "job_id": e.job_id,
        "source_id": e.source_id,
        "stage": e.stage,
        "severity": e.severity,
        "error_type": e.error_type,
        "message": e.message,
        "row_ref": e.row_ref,
        "details": e.details,
        "created_at": j(e.created_at),
    }


def product_out(p: Product) -> dict[str, Any]:
    return {c: j(getattr(p, c)) for c in PRODUCT_COLUMNS}


def price_out(o: Price, converted: Any = None) -> dict[str, Any]:
    out = {c: j(getattr(o, c)) for c in PRICE_COLUMNS}
    out["quality_warnings"] = quality_warnings(o.quality_flags)
    if converted is not None:
        out["conversion"] = {
            "converted_price": j(converted.converted_price),
            "target_currency": converted.target_currency,
            "fx_rate": j(converted.fx_rate),
            "fx_date": j(converted.fx_date),
            "fx_source": converted.fx_source,
            "fx_rate_type": converted.fx_rate_type,
        }
    return out
