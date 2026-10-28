"""Source-label review queue and transactional, reviewed ingredient corrections."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.api import queries as q
from app.dashboard.routes import _render, _session
from app.db.models import (
    Ingredient, IngredientIdentifier, IngredientOverride, Product, ProductIngredientResolution, Source,
)
from app.normalization.ingredients import IngredientResolver

router = APIRouter()
COOKIE = "ingredient_review_csrf"
STATUSES = ("needs_review", "unresolved", "partial", "ambiguous", "missing", "resolved", "manual", "all")


def _products(product: Product):
    return select(Product).where(
        Product.source_id == product.source_id,
        Product.ingredient == product.ingredient,
    )


def _override(session: Session, product: Product) -> IngredientOverride | None:
    return session.scalar(select(IngredientOverride).where(
        IngredientOverride.source_id == product.source_id,
        IngredientOverride.source_text == product.ingredient,
    ))


def _revision(product: Product, override: IngredientOverride | None) -> str:
    value = [
        product.source_id, product.ingredient,
        [override.ingredient_ids, override.evidence] if override else None,
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


@router.get("/dashboard/ingredients", response_class=HTMLResponse, include_in_schema=False)
def ingredient_queue(
    request: Request,
    source_id: str | None = None,
    status: str = "needs_review",
    search: str = "",
    page: int = 1,
    session: Session = Depends(_session),
) -> HTMLResponse:
    if status not in STATUSES:
        raise HTTPException(422, "Unknown resolution status")
    resolution = ProductIngredientResolution
    stmt = select(
        Product.source_id, Product.ingredient,
        func.min(Product.id).label("product_id"),
        func.count().label("products"),
        func.sum(case((resolution.status == "resolved", 1), else_=0)).label("resolved"),
        func.sum(case((resolution.status == "partial", 1), else_=0)).label("partial"),
        func.sum(case((resolution.status == "ambiguous", 1), else_=0)).label("ambiguous"),
        func.sum(case((or_(resolution.status == "unresolved", resolution.status.is_(None)), 1), else_=0)).label("unresolved"),
    ).outerjoin(resolution, resolution.product_id == Product.id)
    if source_id:
        stmt = stmt.where(Product.source_id == source_id)
    if search.strip():
        stmt = stmt.where(Product.ingredient.contains(search.strip(), autoescape=True))
    manual = select(IngredientOverride.id).where(
        IngredientOverride.source_id == Product.source_id,
        IngredientOverride.source_text == Product.ingredient,
    ).exists()
    if status == "needs_review":
        stmt = stmt.where(or_(resolution.status != "resolved", resolution.status.is_(None)))
    elif status == "missing":
        stmt = stmt.where(resolution.product_id.is_(None))
    elif status == "manual":
        stmt = stmt.where(manual)
    elif status != "all":
        stmt = stmt.where(resolution.status == status)
    grouped = stmt.group_by(Product.source_id, Product.ingredient)
    total = session.scalar(select(func.count()).select_from(grouped.subquery())) or 0
    page = max(1, page)
    rows = session.execute(
        grouped.order_by(func.count().desc(), Product.source_id, Product.ingredient)
        .limit(50).offset((page - 1) * 50)
    ).all()
    return _render(
        request, "ingredients.html", rows=rows, total=total, page=page, pages=(total + 49) // 50,
        sources=list(session.scalars(select(Source.id).order_by(Source.id))), statuses=STATUSES,
        source_id=source_id, status=status, search=search,
        query=urlencode({"source_id": source_id or "", "status": status, "search": search}),
    )


@router.get("/dashboard/ingredients/{product_id}", response_class=HTMLResponse, include_in_schema=False)
def ingredient_review(
    product_id: int,
    request: Request,
    search: str = "",
    page: int = 1,
    updated: int | None = None,
    session: Session = Depends(_session),
) -> HTMLResponse:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Unknown product")
    override = _override(session, product)
    selected = list(session.scalars(select(Ingredient).where(
        Ingredient.id.in_(override.ingredient_ids if override else []),
    ).order_by(Ingredient.name)))
    examples = list(session.scalars(_products(product).order_by(Product.id).limit(10)))
    example_rows = [{"product_id": example.id} for example in examples]
    q.add_ingredient_metadata(session, example_rows)
    count = session.scalar(select(func.count()).select_from(_products(product).subquery())) or 0
    suggestions = q.ingredient_suggestions(session, search, page=page, page_size=20) if search.strip() else None
    suggestion_ids = [item["id"] for item in suggestions["items"]] if suggestions else []
    identifiers: dict[str, list[IngredientIdentifier]] = {}
    for identifier in session.scalars(select(IngredientIdentifier).where(
        IngredientIdentifier.ingredient_id.in_(suggestion_ids),
    ).order_by(IngredientIdentifier.namespace, IngredientIdentifier.value)):
        identifiers.setdefault(identifier.ingredient_id, []).append(identifier)
    csrf = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
    response = _render(
        request, "ingredient_review.html", product=product, examples=zip(examples, example_rows),
        count=count, override=override, selected=selected, revision=_revision(product, override),
        csrf=csrf, search=search, suggestions=suggestions, updated=updated,
        query=urlencode({"search": search}), identifiers=identifiers,
    )
    response.set_cookie(COOKIE, csrf, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        path="/dashboard/ingredients")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/dashboard/ingredients/{product_id}", include_in_schema=False)
def save_ingredient_review(
    product_id: int,
    request: Request,
    csrf: str = Form(...),
    revision: str = Form(...),
    action: Literal["save", "delete"] = Form(...),
    ingredient_ids: list[str] = Form([]),
    evidence: str = Form(""),
    session: Session = Depends(_session),
) -> RedirectResponse:
    cookie = request.cookies.get(COOKIE)
    if not cookie or not secrets.compare_digest(cookie.encode(), csrf.encode()):
        raise HTTPException(403, "Invalid review form token. Reload the review page and try again.")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin mapping changes are not allowed")
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Unknown product")
    if not product.ingredient or not product.ingredient.strip():
        raise HTTPException(422, "Source publishes no ingredient wording; a source-label mapping cannot be created")
    override = _override(session, product)
    if not secrets.compare_digest(revision.encode(), _revision(product, override).encode()):
        raise HTTPException(409, "Mapping or source wording changed. Reload before saving.")
    if action == "save":
        ids = sorted(set(ingredient_ids))
        if not ids or not evidence.strip():
            raise HTTPException(422, "Select at least one ingredient and supply evidence or a review note")
        known = set(session.scalars(select(Ingredient.id).where(Ingredient.id.in_(ids))))
        if known != set(ids):
            raise HTTPException(422, "One or more selected ingredient identities do not exist")
        if override is None:
            override = IngredientOverride(source_id=product.source_id, source_text=product.ingredient)
        override.ingredient_ids = ids
        override.evidence = evidence.strip()
        session.add(override)
    else:
        if override is None:
            raise HTTPException(404, "No reviewed mapping exists to remove")
        session.delete(override)
    session.flush()
    resolver = IngredientResolver(session)
    last_id = 0
    count = 0
    # One transaction: never publish a correction without updating its existing products.
    while batch := list(session.scalars(
        _products(product).where(Product.id > last_id).order_by(Product.id).limit(500)
    )):
        for item in batch:
            resolver.resolve(session, item)
        session.flush()
        last_id = batch[-1].id
        count += len(batch)
    session.commit()
    return RedirectResponse(f"/dashboard/ingredients/{product_id}?updated={count}", status_code=303)
