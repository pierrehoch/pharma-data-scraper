"""Read-side queries shared by the REST API and the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, String, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.core.quality import NOT_CURRENT_FLAG, QUALITY_FLAG_LABELS, UNVERIFIED_FLAGS
from app.db.models import (
    Ingredient,
    IngredientAlias,
    IngredientDictionary,
    IngestRun,
    Job,
    JobError,
    Price,
    Product,
    ProductIngredient,
    ProductIngredientResolution,
    SchemaBaseline,
    Source,
)
from app.normalization.text import fmt_decimal
from app.normalization.text import fold
from app.registry import FREQUENCY_DAYS

MAX_PAGE_SIZE = 500
PRODUCT_COLUMNS = [c.key for c in Product.__table__.columns]
PRICE_COLUMNS = [c.key for c in Price.__table__.columns]
_PRODUCT_DATA = [c for c in PRODUCT_COLUMNS if c not in ("id", "first_seen_snapshot_id", "last_seen_snapshot_id")]
_PRICE_DATA = [
    c for c in PRICE_COLUMNS
    if c not in ("id", "product_id", "source_id", "series_key", "observation_key", "superseded_by_id", "first_seen_job_id", "last_seen_snapshot_id")
]
# Price fields that share a name with a product field are prefixed in flat rows.
_PRICE_HEADERS = {c: (f"price_{c}" if c in _PRODUCT_DATA else c) for c in _PRICE_DATA}
DATA_COLUMNS = ["product_id", *_PRODUCT_DATA, "price_id", *_PRICE_HEADERS.values(), "quality_warnings", "ingredient_mappings", "ingredient_resolution"]


def quality_warnings(flags: list[str] | None) -> list[str]:
    return [QUALITY_FLAG_LABELS.get(flag, flag) for flag in flags or []]


def product_coverage(session: Session, terms: list[str], source_ids: list[str]) -> list[dict[str, Any]]:
    """Count matching source items, not deduplicated medicines or equivalent prices."""
    terms = [term.strip().lower() for term in terms if term.strip()]
    if not terms:
        raise ValueError("At least one non-empty coverage search term is required")
    matches = or_(*[
        func.lower(column).contains(term, autoescape=True)
        for term in terms
        for column in (Product.name, Product.brand_name, Product.ingredient, Product.product_code)
    ])
    statement = data_query(DataFilter()).where(matches, Product.source_id.in_(source_ids))
    grouped: dict[str, dict[int, dict[str, Any]]] = {source_id: {} for source_id in source_ids}
    for product, price in session.execute(statement):
        entry = grouped[product.source_id].setdefault(product.id, {
            "product_code": product.product_code, "name": product.name,
            "code_type": product.code_type, "ingredient": product.ingredient,
            "group_code": product.group_code, "group_name": product.group_name,
            "marketing_status": product.marketing_status, "pack_text": product.pack_text,
            "detail_url": product.detail_url, "notes": product.notes,
            "coverage_scope": product.coverage_scope, "prices": [],
        })
        if price is not None and price.amount > 0:
            entry["prices"].append({
                "amount": str(price.amount), "currency": price.currency,
                "price_type": price.price_type, "price_variant": price.price_variant,
                "price_status": price.price_status, "price_unit": price.price_unit,
                "is_current": price.is_current, "quality_flags": list(price.quality_flags or []),
                "valid_from": price.valid_from.isoformat() if price.valid_from else None,
                "valid_to": price.valid_to.isoformat() if price.valid_to else None,
                "published_at": price.published_at.isoformat() if price.published_at else None,
                "unit_text": price.unit_text,
                "date_basis": price.date_basis, "notes": price.notes,
            })
    # CIMA products stay unpriced: expose exact-code evidence without copying prices.
    cima = {item["product_code"]: item for item in grouped.get("es_cima", {}).values()}
    if cima:
        for item in cima.values():
            item["national_code_reconciliation"] = {
                "source_id": "es_nomenclator", "product_present": False, "positive_price_present": False,
            }
        national_matches = data_query(DataFilter(source_id="es_nomenclator", country="ES")).where(
            Product.code_type == "CODIGO_NACIONAL", Product.product_code.in_(cima),
        )
        for product, price in session.execute(national_matches):
            evidence = cima[product.product_code]["national_code_reconciliation"]
            evidence["product_present"] = True
            evidence["positive_price_present"] |= price is not None and price.amount > 0
    results = []
    for source_id, items in grouped.items():
        products = list(items.values())
        prices = [price for product in products for price in product["prices"]]
        results.append({
            "source_id": source_id, "products": len(products),
            "priced_products": sum(bool(product["prices"]) for product in products),
            "price_observations": len(prices),
            "price_observations_with_warnings": sum(bool(price["quality_flags"]) for price in prices),
            "price_types": sorted({price["price_type"] for price in prices}),
            "matches": products,
        })
    return results


def paginate(session: Session, stmt: Select, page: int, page_size: int) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = list(session.scalars(stmt.limit(page_size).offset((page - 1) * page_size)))
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": (total + page_size - 1) // page_size}


# --------------------------------------------------------------------------- data (products + prices)


@dataclass
class DataFilter:
    source_id: str | None = None
    country: str | None = None
    price_type: str | None = None
    q: str | None = None
    current_only: bool = True
    ingredient_id: str | None = None
    verified_only: bool = False


def has_quality_flag(code: str):  # noqa: ANN201
    """SQL predicate on the JSON flag list (stored as text in SQLite, cast for other dialects)."""
    return func.coalesce(cast(Price.quality_flags, String), "").like(f'%"{code}"%')


def data_query(f: DataFilter) -> Select:
    """One row per (product, price); products without any matching price appear once with an empty price.

    ``current_only`` keeps current prices plus non-current values published with an explicit
    ``not_current_value`` warning (previous/proposed/withdrawn/future source figures), which are
    shown by default but never marked current. ``verified_only`` hides every price with an
    unverified-semantics warning.
    """
    on = Price.product_id == Product.id
    if f.current_only:
        on = and_(on, or_(Price.is_current.is_(True), has_quality_flag(NOT_CURRENT_FLAG)))
    if f.verified_only:
        on = and_(on, *[~has_quality_flag(code) for code in sorted(UNVERIFIED_FLAGS)])
    stmt = select(Product, Price).outerjoin(Price, on)
    if f.price_type:
        stmt = stmt.where(Price.price_type == f.price_type)
    if f.source_id:
        stmt = stmt.where(Product.source_id == f.source_id)
    if f.country:
        stmt = stmt.where(Product.country == f.country.upper())
    if f.q:
        like = f"%{f.q.lower()}%"
        stmt = stmt.where(
            or_(
                Product.product_code == f.q.strip(),
                func.lower(Product.name).like(like),
                func.lower(Product.brand_name).like(like),
                func.lower(Product.ingredient).like(like),
            )
        )
    if f.ingredient_id:
        ancestors = select(Ingredient.id, Ingredient.family_id).where(
            Ingredient.id == f.ingredient_id
        ).cte(recursive=True)
        ancestors = ancestors.union(
            select(Ingredient.id, Ingredient.family_id).join(ancestors, Ingredient.id == ancestors.c.family_id)
        )
        descendants = select(ancestors.c.id).where(ancestors.c.family_id.is_(None)).cte(recursive=True)
        descendants = descendants.union(
            select(Ingredient.id).join(descendants, Ingredient.family_id == descendants.c.id)
        )
        stmt = stmt.where(
            select(ProductIngredient.id)
            .where(
                ProductIngredient.product_id == Product.id,
                ProductIngredient.ingredient_id.in_(select(descendants.c.id)),
            )
            .exists()
        )
    return stmt.order_by(Product.country, Product.source_id, Product.name, Product.id, Price.price_type, Price.price_variant, Price.id.desc())


def paginate_rows(session: Session, stmt: Select, page: int, page_size: int) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = session.execute(stmt.limit(page_size).offset((page - 1) * page_size)).all()
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": (total + page_size - 1) // page_size}


def data_row(product: Product, price: Price | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "product_id": product.id,
        **{c: getattr(product, c) for c in _PRODUCT_DATA},
        "price_id": price.id if price else None,
        "ingredient_mappings": [],
        "ingredient_resolution": None,
    }
    for col, header in _PRICE_HEADERS.items():
        row[header] = getattr(price, col) if price is not None else None
    row["quality_warnings"] = quality_warnings(price.quality_flags) if price is not None else []
    return row


def ingredient_metadata(session: Session, product_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Load ingredient links and resolution states for a page or CSV batch in two queries."""
    if not product_ids:
        return {}
    result = {
        product_id: {"ingredient_mappings": [], "ingredient_resolution": None}
        for product_id in product_ids
    }
    links = session.execute(
        select(ProductIngredient, Ingredient)
        .join(Ingredient, Ingredient.id == ProductIngredient.ingredient_id)
        .where(ProductIngredient.product_id.in_(product_ids))
        .order_by(Ingredient.name, Ingredient.id, ProductIngredient.id)
    )
    for link, ingredient in links:
        result[link.product_id]["ingredient_mappings"].append({
            "ingredient_id": ingredient.id,
            "name": ingredient.name,
            "family_id": ingredient.family_id,
            "source_text": link.source_text,
            "method": link.method,
            "evidence": link.evidence,
            "dictionary_version": link.dictionary_version,
        })
    for resolution in session.scalars(
        select(ProductIngredientResolution).where(ProductIngredientResolution.product_id.in_(product_ids))
    ):
        result[resolution.product_id]["ingredient_resolution"] = {
            "status": resolution.status,
            "reason": resolution.reason,
            "components": resolution.components,
            "dictionary_version": resolution.dictionary_version,
        }
    return result


def add_ingredient_metadata(session: Session, rows: list[dict[str, Any]]) -> None:
    metadata = ingredient_metadata(session, list({row["product_id"] for row in rows}))
    for row in rows:
        row.update(metadata.get(row["product_id"], {"ingredient_mappings": [], "ingredient_resolution": None}))


def ingredient_suggestions(
    session: Session, query: str | None, page: int = 1, page_size: int = 20,
) -> dict[str, Any]:
    """Page unique concepts matched by multilingual aliases; counts are distinct products."""
    normalized = fold(query)
    matches = select(IngredientAlias.ingredient_id).where(
        IngredientAlias.normalized.contains(normalized, autoescape=True)
    ) if normalized else select(IngredientAlias.ingredient_id)
    ids = select(Ingredient.id).where(Ingredient.id.in_(matches)).order_by(Ingredient.name, Ingredient.id)
    paged = paginate(session, ids, page, page_size)
    ingredient_ids = [row for row in paged["items"]]
    if not ingredient_ids:
        paged["items"] = []
        return paged
    alias_stmt = select(IngredientAlias).where(IngredientAlias.ingredient_id.in_(ingredient_ids))
    if normalized:
        alias_stmt = alias_stmt.where(IngredientAlias.normalized.contains(normalized, autoescape=True))
    aliases: dict[str, IngredientAlias] = {}
    for alias in session.scalars(alias_stmt.order_by(IngredientAlias.normalized, IngredientAlias.language, IngredientAlias.id)):
        aliases.setdefault(alias.ingredient_id, alias)
    products = session.execute(
        select(
            ProductIngredient.ingredient_id,
            func.count(func.distinct(ProductIngredient.product_id)),
            func.count(func.distinct(case(
                (and_(Price.id.is_not(None), Price.amount > 0, Price.is_current.is_(True)), ProductIngredient.product_id),
            ))),
        )
        .where(ProductIngredient.ingredient_id.in_(ingredient_ids))
        .outerjoin(Price, Price.product_id == ProductIngredient.product_id)
        .group_by(ProductIngredient.ingredient_id)
    )
    counts = {ingredient_id: (product_count, priced_count) for ingredient_id, product_count, priced_count in products}
    concepts = {i.id: i for i in session.scalars(select(Ingredient).where(Ingredient.id.in_(ingredient_ids)))}
    paged["items"] = [
        {
            "id": ingredient_id,
            "name": concepts[ingredient_id].name,
            "family_id": concepts[ingredient_id].family_id,
            "matched_alias": aliases[ingredient_id].label,
            "language": aliases[ingredient_id].language,
            "mapped_products": counts.get(ingredient_id, (0, 0))[0],
            "priced_products": counts.get(ingredient_id, (0, 0))[1],
        }
        for ingredient_id in ingredient_ids
        if ingredient_id in concepts and ingredient_id in aliases
    ]
    return paged


def exact_ingredient_matches(session: Session, value: str) -> list[Ingredient]:
    normalized = fold(value)
    if not normalized:
        return []
    return list(session.scalars(
        select(Ingredient)
        .join(IngredientAlias, IngredientAlias.ingredient_id == Ingredient.id)
        .where(IngredientAlias.normalized == normalized)
        .order_by(Ingredient.name, Ingredient.id)
        .distinct()
    ))


def ingredient_coverage(session: Session) -> dict[str, Any]:
    """Global mapping state; missing mappings are not asserted matches to any ingredient."""
    status_rows = session.execute(
        select(Product.source_id, ProductIngredientResolution.status, func.count(Product.id))
        .join(ProductIngredientResolution, ProductIngredientResolution.product_id == Product.id)
        .group_by(Product.source_id, ProductIngredientResolution.status)
    )
    missing_rows = session.execute(
        select(Product.source_id, func.count(Product.id))
        .outerjoin(ProductIngredientResolution, ProductIngredientResolution.product_id == Product.id)
        .where(ProductIngredientResolution.product_id.is_(None))
        .group_by(Product.source_id)
    )
    priced_rows = session.execute(
        select(Product.source_id, func.count(func.distinct(Product.id)))
        .join(ProductIngredient, ProductIngredient.product_id == Product.id)
        .join(Price, Price.product_id == Product.id)
        .where(Price.is_current.is_(True), Price.amount > 0)
        .group_by(Product.source_id)
    )
    sources: dict[str, dict[str, Any]] = {}
    for source_id, status, count in status_rows:
        sources.setdefault(source_id, {"resolution_counts": {}, "resolution_missing": 0, "priced_mapped_products": 0})
        sources[source_id]["resolution_counts"][status] = count
    for source_id, count in missing_rows:
        sources.setdefault(source_id, {"resolution_counts": {}, "resolution_missing": 0, "priced_mapped_products": 0})
        sources[source_id]["resolution_missing"] = count
    for source_id, count in priced_rows:
        sources.setdefault(source_id, {"resolution_counts": {}, "resolution_missing": 0, "priced_mapped_products": 0})
        sources[source_id]["priced_mapped_products"] = count
    dictionaries = list(session.scalars(select(IngredientDictionary).order_by(IngredientDictionary.retrieved_at.desc(), IngredientDictionary.id)))
    return {
        "sources": [{"source_id": source_id, **values} for source_id, values in sorted(sources.items())],
        "dictionaries": [
            {
                "id": row.id,
                "provider": row.provider,
                "retrieved_at": to_jsonable(row.retrieved_at),
                "summary": row.summary,
            }
            for row in dictionaries
        ],
        "limitations": "Global missing-resolution counts are data coverage gaps, not known matches for any ingredient.",
    }


def distinct_values(session: Session) -> dict[str, list[str]]:
    return {
        "sources": list(session.scalars(select(Source.id).order_by(Source.id))),
        "countries": list(session.scalars(select(Source.country).distinct().order_by(Source.country))),
        "price_types": list(session.scalars(select(Price.price_type).distinct().order_by(Price.price_type))),
    }


# --------------------------------------------------------------------------- monitoring


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def source_overview(session: Session) -> list[dict[str, Any]]:
    sources = list(session.scalars(select(Source).order_by(Source.country, Source.id)))
    out = []
    for src in sources:
        last = session.scalars(select(Job).where(Job.source_id == src.id).order_by(Job.started_at.desc()).limit(1)).first()
        last_ok = session.scalars(
            select(Job)
            .where(Job.source_id == src.id, Job.status.in_([JobStatus.SUCCESS.value, JobStatus.PARTIAL_SUCCESS.value]), Job.mode != "dry_run")
            .order_by(Job.started_at.desc())
            .limit(1)
        ).first()
        baseline_count = session.scalar(select(func.count()).select_from(SchemaBaseline).where(SchemaBaseline.source_id == src.id)) or 0
        schema_status = "unknown"
        if last is not None and last.status == JobStatus.SCHEMA_CHANGED.value:
            schema_status = "CHANGED"
        elif baseline_count:
            schema_status = "ok"
        days = FREQUENCY_DAYS.get(src.expected_frequency)
        next_expected = None
        overdue = False
        if last_ok and days:
            next_expected = _aware(last_ok.started_at) + timedelta(days=days)
            overdue = next_expected < datetime.now(timezone.utc)
        products = session.scalar(select(func.count()).select_from(Product).where(Product.source_id == src.id)) or 0
        prices = session.scalar(select(func.count()).select_from(Price).where(Price.source_id == src.id)) or 0
        out.append(
            {
                "source": src,
                "last_job": last,
                "last_success": last_ok,
                "schema_status": schema_status,
                "next_expected_update": next_expected,
                "overdue": overdue,
                "products": products,
                "prices": prices,
                "missing_configuration": bool(
                    last
                    and session.scalar(
                        select(func.count()).select_from(JobError).where(JobError.job_id == last.id, JobError.error_type == "MISSING_CONFIGURATION")
                    )
                ),
            }
        )
    return out


def latest_run(session: Session) -> IngestRun | None:
    return session.scalars(select(IngestRun).order_by(IngestRun.started_at.desc()).limit(1)).first()


def jobs_query(source_id: str | None = None, status: str | None = None, run_id: str | None = None) -> Select:
    stmt = select(Job).order_by(Job.started_at.desc())
    if source_id:
        stmt = stmt.where(Job.source_id == source_id)
    if status:
        stmt = stmt.where(Job.status == status)
    if run_id:
        stmt = stmt.where(Job.run_id == run_id)
    return stmt


def errors_query(
    source_id: str | None = None,
    severity: str | None = None,
    error_type: str | None = None,
    job_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Select:
    stmt = select(JobError).order_by(JobError.id.desc())
    if source_id:
        stmt = stmt.where(JobError.source_id == source_id)
    if severity:
        stmt = stmt.where(JobError.severity == severity)
    if error_type:
        stmt = stmt.where(JobError.error_type == error_type)
    if job_id:
        stmt = stmt.where(JobError.job_id == job_id)
    if date_from:
        stmt = stmt.where(JobError.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        stmt = stmt.where(JobError.created_at < datetime.combine(date_to, datetime.max.time()))
    return stmt


def error_counts_by_severity(session: Session, job_id: str) -> dict[str, int]:
    return dict(
        session.execute(select(JobError.severity, func.count()).where(JobError.job_id == job_id).group_by(JobError.severity)).all()
    )


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return fmt_decimal(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
