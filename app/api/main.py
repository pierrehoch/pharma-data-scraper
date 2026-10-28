"""REST API (FastAPI) + dashboard mount."""

from __future__ import annotations

import csv
import io
import itertools
import json
from datetime import date
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import queries as q
from app.api import serializers as ser
from app.config import Settings, get_settings
from app.core.enums import FxRateType
from app.db.models import Ingredient, IngestRun, Job, JobError, Price, Product
from app.db.session import make_session_factory
from app.fx.service import convert
from app.observability import configure_logging, register_secrets


def get_session() -> Iterator[Session]:
    factory = make_session_factory(get_settings().database_url)
    with factory() as session:
        yield session


def settings_dep() -> Settings:
    return get_settings()


def data_filter(
    source_id: str | None = None,
    country: str | None = None,
    price_type: str | None = None,
    q_: str | None = Query(None, alias="q"),
    current_only: bool = True,
    ingredient_id: str | None = None,
    verified_only: bool = False,
) -> q.DataFilter:
    return q.DataFilter(source_id=source_id or None, country=country or None, price_type=price_type or None, q=q_ or None, current_only=current_only, ingredient_id=ingredient_id or None, verified_only=verified_only)


def _csv_cell(value: Any) -> str:
    value = q.to_jsonable(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def stream_csv(f: q.DataFilter, settings: Settings) -> StreamingResponse:
    def body() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(q.DATA_COLUMNS)
        # Own session: the request-scoped one may be closed before streaming ends.
        with make_session_factory(settings.database_url)() as session:
            cursor = iter(session.execute(q.data_query(f).execution_options(yield_per=1000)))
            i = 0
            while batch := list(itertools.islice(cursor, 500)):
                rows = [q.data_row(product, price) for product, price in batch]
                q.add_ingredient_metadata(session, rows)
                for row in rows:
                    writer.writerow([_csv_cell(row[c]) for c in q.DATA_COLUMNS])
                i += len(batch)
                if i % 500 == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
        yield buf.getvalue()

    name = f"prices_{date.today().isoformat()}.csv"
    return StreamingResponse(body(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    register_secrets(settings.secrets)
    app = FastAPI(
        title="Drug Price Platform API",
        version="0.2.0",
        description="Drug products and prices as published by each source (generic fields, exact decimals, full price history).",
    )

    from app.dashboard.routes import router as dashboard_router

    app.include_router(dashboard_router)
    from app.dashboard.ingredients import router as ingredient_review_router

    app.include_router(ingredient_review_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sources")
    def list_sources(country: str | None = None, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        rows = q.source_overview(session)
        if country:
            rows = [r for r in rows if r["source"].country == country.upper()]
        return [ser.source_row_out(r) for r in rows]

    @app.get("/ingredients")
    def ingredients(
        q_: str | None = Query(None, alias="q"),
        page: int = 1,
        page_size: int = 20,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        return q.ingredient_suggestions(session, q_, page, page_size)

    @app.get("/ingredients/coverage")
    def ingredient_coverage(session: Session = Depends(get_session)) -> dict[str, Any]:
        return q.ingredient_coverage(session)

    @app.get("/sources/{source_id}")
    def get_source(source_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        rows = [r for r in q.source_overview(session) if r["source"].id == source_id]
        if not rows:
            raise HTTPException(404, f"Unknown source {source_id}")
        return ser.source_row_out(rows[0])

    @app.get("/runs")
    def runs(page: int = 1, page_size: int = 20, session: Session = Depends(get_session)) -> dict[str, Any]:
        res = q.paginate(session, select(IngestRun).order_by(IngestRun.started_at.desc()), page, page_size)
        res["items"] = [
            {"id": r.id, "started_at": q.to_jsonable(r.started_at), "finished_at": q.to_jsonable(r.finished_at), "mode": r.mode,
             "limit": r.limit, "command": r.command, "status": r.status, "summary": r.summary}
            for r in res["items"]
        ]
        return res

    @app.get("/jobs")
    def jobs(
        source_id: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        res = q.paginate(session, q.jobs_query(source_id, status, run_id), page, page_size)
        res["items"] = [ser.job_summary_out(j) for j in res["items"]]
        return res

    @app.get("/jobs/{job_id}")
    def job_detail(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, f"Unknown job {job_id}")
        errors = list(session.scalars(select(JobError).where(JobError.job_id == job_id).order_by(JobError.id).limit(500)))
        return ser.job_detail_out(job, errors)

    @app.get("/errors")
    def errors(
        source_id: str | None = None,
        severity: str | None = None,
        error_type: str | None = None,
        job_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = 1,
        page_size: int = 50,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        res = q.paginate(session, q.errors_query(source_id, severity, error_type, job_id, date_from, date_to), page, page_size)
        res["items"] = [ser.error_out(e) for e in res["items"]]
        return res

    @app.get("/prices")
    def prices(
        f: q.DataFilter = Depends(data_filter),
        page: int = 1,
        page_size: int = 100,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Products with their prices (filters: source_id, country, price_type, q text search, current_only, verified_only).

        Prices published with warnings are included by default; ``quality_flags``/``quality_warnings`` explain them.
        """
        if f.ingredient_id and session.get(Ingredient, f.ingredient_id) is None:
            raise HTTPException(404, f"Unknown ingredient {f.ingredient_id}")
        res = q.paginate_rows(session, q.data_query(f), page, page_size)
        rows = [q.data_row(product, price) for product, price in res["items"]]
        q.add_ingredient_metadata(session, rows)
        res["items"] = [{k: q.to_jsonable(v) for k, v in row.items()} for row in rows]
        return res

    @app.get("/prices.csv")
    def prices_csv(
        f: q.DataFilter = Depends(data_filter),
        settings: Settings = Depends(settings_dep),
        session: Session = Depends(get_session),
    ) -> StreamingResponse:
        if f.ingredient_id and session.get(Ingredient, f.ingredient_id) is None:
            raise HTTPException(404, f"Unknown ingredient {f.ingredient_id}")
        return stream_csv(f, settings)

    @app.get("/products/{product_id}")
    def product_detail(
        product_id: int,
        target_currency: str | None = None,
        fx_rate_type: FxRateType = FxRateType.SPOT,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        product = session.get(Product, product_id)
        if product is None:
            raise HTTPException(404, f"Unknown product {product_id}")
        history = session.scalars(select(Price).where(Price.product_id == product_id).order_by(Price.price_type, Price.price_variant, Price.id.desc()))
        out = []
        for o in history:
            converted = None
            if target_currency:
                on = o.valid_from or o.published_at or o.retrieved_at.date()
                converted = convert(session, o.amount, o.currency, target_currency.upper(), on, rate_type=fx_rate_type)
            out.append(ser.price_out(o, converted))
        metadata = q.ingredient_metadata(session, [product_id])[product_id]
        return {**ser.product_out(product), **metadata, "prices": out}

    return app


app = create_app()
