"""Publication into the database: one product row per source item, append-only price history."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.pricing import normalize_price
from app.core.quality import variation_issue
from app.core.types import PRODUCT_FIELDS, Issue, NormalizedRecord, PriceFact, RawSnapshot
from app.db.models import Price, Product
from app.normalization.text import fmt_decimal
from app.normalization.ingredients import IngredientResolver
from app.registry import SourceConfig


@dataclass
class PublishStats:
    records_published: int = 0
    prices_new: int = 0
    prices_unchanged: int = 0
    failed: int = 0
    issues: list[Issue] = field(default_factory=list)


def series_key(fact: PriceFact) -> str:
    return f"{fact.price_type}|{fact.price_variant}|{fact.price_unit}|{fact.coverage_scope}"


def observation_key(product_id: int, fact: PriceFact) -> str:
    parts = [
        str(product_id),
        series_key(fact),
        fact.valid_from.isoformat() if fact.valid_from else "",
        fact.valid_to.isoformat() if fact.valid_to else "",
        fmt_decimal(fact.amount) or "",
        fact.currency,
        str(fact.vat_included),
        str(fact.statutory_reduction_included),
        fmt_decimal(fact.pack_quantity) or "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _same_value(obs: Price, fact: PriceFact) -> bool:
    return (
        Decimal(str(obs.amount)) == fact.amount
        and obs.currency == fact.currency
        and obs.vat_included == fact.vat_included
        and obs.statutory_reduction_included == fact.statutory_reduction_included
        and (Decimal(str(obs.pack_quantity)) if obs.pack_quantity is not None else None) == fact.pack_quantity
    )


def _sort_date(obs_or_fact_date: date | None, fallback: date | None) -> date:
    return obs_or_fact_date or fallback or date.min


class Publisher:
    def __init__(self, session: Session, config: SourceConfig, snapshot: RawSnapshot, job_id: str):
        self.session = session
        self.config = config
        self.snapshot = snapshot
        self.job_id = job_id
        self.now = datetime.now(timezone.utc)
        self.stats = PublishStats()
        self.ingredient_resolver = IngredientResolver(session)

    def publish(self, record: NormalizedRecord) -> None:
        product = self._upsert_product(record)
        for fact in record.prices:
            self._publish_price(product, record, fact)
        self.stats.records_published += 1

    def _upsert_product(self, record: NormalizedRecord) -> Product:
        data = record.product
        product = self.session.scalars(
            select(Product).where(Product.source_id == self.config.source_id, Product.product_code == data.product_code)
        ).first()
        if product is None:
            product = Product(source_id=self.config.source_id, first_seen_snapshot_id=self.snapshot.id, first_seen_at=self.now)
            self.session.add(product)
        for name in PRODUCT_FIELDS:
            setattr(product, name, getattr(data, name))
        product.product_level = data.product_level.value
        product.country = self.config.country
        product.coverage_scope = self.config.coverage_scope
        product.last_seen_snapshot_id = self.snapshot.id
        product.last_seen_at = self.now
        self.session.flush()
        self.ingredient_resolver.resolve(self.session, product, record.ingredient_components)
        return product

    # ------------------------------------------------------------------ prices

    def _publish_price(self, product: Product, record: NormalizedRecord, fact: PriceFact) -> None:
        skey = series_key(fact)
        # is_current is filtered in Python: as a SQL predicate SQLite may pick the low-selectivity is_current index.
        series = self.session.scalars(
            select(Price).where(Price.product_id == product.id, Price.series_key == skey).order_by(Price.id.desc())
        ).all()
        current = next((o for o in series if o.is_current), None)
        edition_dated = fact.date_basis == "edition_date"
        okey = observation_key(product.id, fact)

        existing = self.session.scalars(select(Price).where(Price.observation_key == okey)).first()
        if existing is not None:
            self._touch(existing)
            return
        # Lists without explicit price dates: an unchanged value in a new edition is not a new observation.
        if edition_dated and current is not None and _same_value(current, fact):
            self._touch(current)
            return

        normalized_price, normalized_unit = normalize_price(fact)
        flags: list[str] = list(fact.quality_flags)
        if current is not None:
            issue = variation_issue(record, fact, Decimal(str(current.amount)))
            if issue is not None:
                flags.append("price_variation_x10")
                self.stats.issues.append(issue)
        obs = Price(
            product_id=product.id,
            source_id=self.config.source_id,
            series_key=skey,
            observation_key=okey,
            price_type=str(fact.price_type),
            price_variant=fact.price_variant,
            price_status=str(fact.price_status),
            amount=fact.amount,
            currency=fact.currency,
            price_unit=fact.price_unit,
            unit_text=fact.unit_text or fact.price_unit,
            pack_quantity=fact.pack_quantity,
            pack_unit=fact.pack_unit,
            unit_price=normalized_price,
            unit_price_unit=normalized_unit,
            vat_included=fact.vat_included,
            tax_rate=fact.tax_rate,
            dispensing_fee=fact.dispensing_fee,
            statutory_reduction_included=fact.statutory_reduction_included,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            date_basis=fact.date_basis,
            published_at=fact.published_at or self.snapshot.published_at,
            retrieved_at=self.snapshot.retrieved_at,
            coverage_scope=fact.coverage_scope,
            notes=fact.notes,
            snapshot_id=self.snapshot.id,
            row_ref=record.row_ref,
            quality_flags=flags,
            is_current=not fact.never_current,
            first_seen_job_id=self.job_id,
            last_seen_snapshot_id=self.snapshot.id,
            last_seen_at=self.now,
        )
        self.session.add(obs)
        self.session.flush()
        if current is not None and not fact.never_current:
            new_date = _sort_date(fact.valid_from, fact.published_at or self.snapshot.published_at)
            cur_date = _sort_date(current.valid_from, current.published_at)
            if new_date >= cur_date:
                current.is_current = False
                current.superseded_at = self.now
                current.superseded_by_id = obs.id
            else:  # back-filled older observation: history grows, current price unchanged
                obs.is_current = False
        self.stats.prices_new += 1

    def _touch(self, obs: Price) -> None:
        obs.last_seen_snapshot_id = self.snapshot.id
        obs.last_seen_at = self.now
        self.stats.prices_unchanged += 1
