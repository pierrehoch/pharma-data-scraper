"""Server-rendered dashboard (Jinja2): monitoring + data viewer. Uses the same query layer as the REST API."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import queries as q
from app.core.enums import ErrorType, JobStatus, Severity
from app.core.quality import QUALITY_FLAG_LABELS
from app.db.models import Ingredient, Job, JobError, Price, Product, SchemaBaseline, Source

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()
DATA_PAGE_SIZE = 100
# Columns shown in the viewer table (all fields are on the detail page and in the CSV).
VIEWER_COLUMNS = [
    ("country", "Country"), ("source_id", "Source"), ("product_code", "Code"), ("name", "Name"), ("ingredient", "Ingredient"),
    ("manufacturer", "Manufacturer"), ("price_type", "Price type"), ("price_variant", "Variant"), ("amount", "Amount"),
    ("currency", "Cur."), ("price_unit", "Per"), ("vat_included", "VAT incl."), ("valid_from", "Valid from"), ("is_current", "Current"),
    ("quality_flags", "Warnings"),
]

STATUS_CLASS = {
    JobStatus.SUCCESS.value: "ok",
    JobStatus.PARTIAL_SUCCESS.value: "warn",
    JobStatus.RUNNING.value: "info",
    JobStatus.QUEUED.value: "info",
    JobStatus.FAILED.value: "err",
    JobStatus.SCHEMA_CHANGED.value: "err",
    JobStatus.CANCELLED.value: "muted",
    JobStatus.UNSUPPORTED.value: "muted",
}
STAGE_ICON = {"success": "✓", "warning": "⚠", "failed": "✗", "skipped": "–", "pending": "…"}


def _session():  # noqa: ANN202
    from app.api.main import get_session

    yield from get_session()


def _render(request: Request, name: str, **ctx) -> HTMLResponse:  # noqa: ANN003
    return templates.TemplateResponse(request, name, {"status_class": STATUS_CLASS, "stage_icon": STAGE_ICON, "fmt": q.to_jsonable,
                                                      "flag_labels": QUALITY_FLAG_LABELS, **ctx})


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def overview(request: Request, session: Session = Depends(_session)) -> HTMLResponse:
    run = q.latest_run(session)
    return _render(request, "overview.html", rows=q.source_overview(session), run=run, totals=(run.summary or {}).get("totals") if run else None)


@router.get("/dashboard/sources/{source_id}", response_class=HTMLResponse, include_in_schema=False)
def source_detail(source_id: str, request: Request, session: Session = Depends(_session)) -> HTMLResponse:
    rows = [r for r in q.source_overview(session) if r["source"].id == source_id]
    if not rows:
        raise HTTPException(404, "Unknown source")
    jobs = list(session.scalars(q.jobs_query(source_id=source_id).limit(30)))
    baselines = list(session.scalars(select(SchemaBaseline).where(SchemaBaseline.source_id == source_id)))
    return _render(request, "source.html", row=rows[0], jobs=jobs, baselines=baselines)


@router.get("/dashboard/jobs", response_class=HTMLResponse, include_in_schema=False)
def jobs(
    request: Request,
    source_id: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    page: int = 1,
    session: Session = Depends(_session),
) -> HTMLResponse:
    res = q.paginate(session, q.jobs_query(source_id or None, status or None, run_id or None), page, 50)
    sources = list(session.scalars(select(Source.id).order_by(Source.id)))
    return _render(request, "jobs.html", res=res, sources=sources, statuses=[s.value for s in JobStatus], f={"source_id": source_id, "status": status})


@router.get("/dashboard/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
def job_detail(job_id: str, request: Request, session: Session = Depends(_session)) -> HTMLResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    errors = list(session.scalars(select(JobError).where(JobError.job_id == job_id).order_by(JobError.id).limit(300)))
    return _render(request, "job.html", job=job, errors=errors, counts=q.error_counts_by_severity(session, job_id))


@router.get("/dashboard/errors", response_class=HTMLResponse, include_in_schema=False)
def errors(
    request: Request,
    source_id: str | None = None,
    severity: str | None = None,
    error_type: str | None = None,
    job_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    session: Session = Depends(_session),
) -> HTMLResponse:
    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to) if date_to else None
    res = q.paginate(session, q.errors_query(source_id or None, severity or None, error_type or None, job_id or None, df, dt), page, 100)
    sources = list(session.scalars(select(Source.id).order_by(Source.id)))
    return _render(
        request,
        "errors.html",
        res=res,
        sources=sources,
        severities=[s.value for s in Severity],
        error_types=[e.value for e in ErrorType],
        f={"source_id": source_id, "severity": severity, "error_type": error_type, "job_id": job_id, "date_from": date_from, "date_to": date_to},
    )


@router.get("/dashboard/data", response_class=HTMLResponse, include_in_schema=False)
def data(
    request: Request,
    source_id: str | None = None,
    country: str | None = None,
    price_type: str | None = None,
    q_: str | None = Query(None, alias="q"),
    ingredient: str | None = None,
    ingredient_id: str | None = None,
    history: bool = False,
    verified_only: bool = False,
    page: int = 1,
    session: Session = Depends(_session),
) -> HTMLResponse:
    ingredient_choices = []
    selected_ingredient = None
    if ingredient_id:
        selected_ingredient = session.get(Ingredient, ingredient_id)
        if selected_ingredient is None:
            raise HTTPException(404, f"Unknown ingredient {ingredient_id}")
    elif ingredient:
        exact = q.exact_ingredient_matches(session, ingredient)
        if len(exact) == 1:
            selected_ingredient = exact[0]
        else:
            ingredient_choices = q.ingredient_suggestions(session, ingredient, page=1, page_size=20)["items"]
    f = q.DataFilter(
        source_id=source_id or None, country=country or None, price_type=price_type or None,
        q=q_ or None, current_only=not history, ingredient_id=selected_ingredient.id if selected_ingredient else None,
        verified_only=verified_only,
    )
    res = q.paginate_rows(session, q.data_query(f), page, DATA_PAGE_SIZE)
    rows = [q.data_row(product, price) for product, price in res["items"]]
    q.add_ingredient_metadata(session, rows)
    params = {
        "source_id": f.source_id, "country": f.country, "price_type": f.price_type, "q": f.q,
        "ingredient_id": f.ingredient_id, "ingredient": ingredient,
    }
    filled = {k: v for k, v in params.items() if v}
    flags = {"verified_only": "true"} if verified_only else {}
    query = urlencode(filled | ({"history": "true"} if history else {}) | flags)
    csv_query = urlencode(filled | {"current_only": str(not history).lower()} | flags)
    return _render(
        request, "data.html", res=res, rows=rows, columns=VIEWER_COLUMNS, f={**params, "history": history, "verified_only": verified_only},
        query=query, csv_query=csv_query, ingredient_choices=ingredient_choices,
        selected_ingredient=selected_ingredient, **q.distinct_values(session),
    )


@router.get("/dashboard/data/{product_id}", response_class=HTMLResponse, include_in_schema=False)
def data_detail(product_id: int, request: Request, session: Session = Depends(_session)) -> HTMLResponse:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Unknown product")
    prices = list(session.scalars(select(Price).where(Price.product_id == product_id).order_by(Price.price_type, Price.price_variant, Price.id.desc())))
    fields = [(c, getattr(product, c)) for c in q.PRODUCT_COLUMNS]
    price_columns = [c for c in q.PRICE_COLUMNS if c not in ("product_id", "source_id", "series_key", "observation_key")]
    metadata = q.ingredient_metadata(session, [product_id])[product_id]
    return _render(request, "data_detail.html", product=product, fields=fields, prices=prices, price_columns=price_columns, **metadata)
