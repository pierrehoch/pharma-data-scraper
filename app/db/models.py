from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------- registry


class Source(Base):
    __tablename__ = "source"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    country: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(255))
    adapter: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expected_frequency: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(Text)
    identifier_type: Mapped[str] = mapped_column(String(64))
    adapter_status: Mapped[str] = mapped_column(String(32))
    parser_type: Mapped[str | None] = mapped_column(String(32))
    coverage_scope: Mapped[str] = mapped_column(String(64), default="national")
    currency: Mapped[str | None] = mapped_column(String(3))
    documentary_status: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- operations


class IngestRun(Base):
    __tablename__ = "ingest_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    command: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))
    limit: Mapped[int | None] = mapped_column(Integer)
    selection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")


class Job(Base):
    __tablename__ = "job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("ingest_run.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="normal")
    limit: Mapped[int | None] = mapped_column(Integer)
    product_ids: Mapped[list[Any] | None] = mapped_column(JSON)
    records_total_in_source: Mapped[int | None] = mapped_column(Integer)
    records_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    records_parsed: Mapped[int] = mapped_column(Integer, default=0)
    records_normalized: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    records_published: Mapped[int] = mapped_column(Integer, default=0)
    prices_published: Mapped[int] = mapped_column(Integer, default=0)
    prices_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    validation_warnings: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_snapshot.id"))
    resumed_from_job_id: Mapped[str | None] = mapped_column(String(32))
    adapter_status: Mapped[str | None] = mapped_column(String(32))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    errors: Mapped[list["JobError"]] = relationship(back_populates="job", order_by="JobError.id")

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            start, end = self.started_at, self.finished_at
            if (start.tzinfo is None) != (end.tzinfo is None):
                start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
            return (end - start).total_seconds()
        return None


class JobError(Base):
    __tablename__ = "job_error"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str | None] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    error_type: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    row_ref: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    job: Mapped[Job] = relationship(back_populates="errors")


class RawSnapshotRecord(Base):
    __tablename__ = "raw_snapshot"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[date | None] = mapped_column(Date)
    edition: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    parser_version: Mapped[str | None] = mapped_column(String(32))
    storage_path: Mapped[str] = mapped_column(Text)
    files: Mapped[list[Any]] = mapped_column(JSON, default=list)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[str | None] = mapped_column(String(64))


class SchemaBaseline(Base):
    __tablename__ = "schema_baseline"
    __table_args__ = (UniqueConstraint("source_id", "role", name="uq_schema_baseline_source_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"))
    role: Mapped[str] = mapped_column(String(64))
    columns: Mapped[list[Any]] = mapped_column(JSON)
    column_types: Mapped[dict[str, Any]] = mapped_column(JSON)
    row_count: Mapped[int | None] = mapped_column(Integer)
    snapshot_id: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- products


class Product(Base):
    """One item of one source (pack, product, billing code, regulatory entry). Generic fields, filled when published."""

    __tablename__ = "product"
    __table_args__ = (UniqueConstraint("source_id", "product_code", name="uq_product_source_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), index=True)
    country: Mapped[str] = mapped_column(String(8), index=True)
    coverage_scope: Mapped[str] = mapped_column(String(64), default="national")
    product_code: Mapped[str] = mapped_column(String(128), index=True)
    code_type: Mapped[str] = mapped_column(String(64))
    product_level: Mapped[str] = mapped_column(String(32))

    name: Mapped[str | None] = mapped_column(Text)
    brand_name: Mapped[str | None] = mapped_column(Text)
    ingredient: Mapped[str | None] = mapped_column(Text)
    strength_text: Mapped[str | None] = mapped_column(Text)
    strength_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    strength_unit: Mapped[str | None] = mapped_column(String(64))
    dosage_form: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(Text)
    pack_text: Mapped[str | None] = mapped_column(Text)
    pack_quantity: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    pack_unit: Mapped[str | None] = mapped_column(String(128))
    manufacturer: Mapped[str | None] = mapped_column(Text)
    atc_code: Mapped[str | None] = mapped_column(String(32), index=True)
    therapeutic_class: Mapped[str | None] = mapped_column(Text)
    product_type: Mapped[str | None] = mapped_column(Text)

    group_code: Mapped[str | None] = mapped_column(String(128))
    group_name: Mapped[str | None] = mapped_column(Text)
    authorisation_number: Mapped[str | None] = mapped_column(String(128))
    ean: Mapped[str | None] = mapped_column(String(255))

    legal_status: Mapped[str | None] = mapped_column(Text)
    authorisation_status: Mapped[str | None] = mapped_column(Text)
    marketing_status: Mapped[str | None] = mapped_column(Text)
    authorisation_date: Mapped[date | None] = mapped_column(Date)
    marketing_start_date: Mapped[date | None] = mapped_column(Date)
    marketing_end_date: Mapped[date | None] = mapped_column(Date)

    reimbursement_rate: Mapped[str | None] = mapped_column(String(64))
    reimbursement_category: Mapped[str | None] = mapped_column(Text)
    reimbursement_conditions: Mapped[str | None] = mapped_column(Text)
    restrictions: Mapped[str | None] = mapped_column(Text)
    orphan: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    detail_url: Mapped[str | None] = mapped_column(Text)

    first_seen_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    last_seen_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    prices: Mapped[list["Price"]] = relationship(back_populates="product", order_by="Price.id")


class Ingredient(Base):
    __tablename__ = "ingredient"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text)
    family_id: Mapped[str | None] = mapped_column(ForeignKey("ingredient.id"), index=True)
    family_evidence: Mapped[str | None] = mapped_column(Text)


class IngredientIdentifier(Base):
    __tablename__ = "ingredient_identifier"
    __table_args__ = (UniqueConstraint("namespace", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), index=True)
    namespace: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[str] = mapped_column(Text)


class IngredientAlias(Base):
    __tablename__ = "ingredient_alias"
    __table_args__ = (UniqueConstraint("ingredient_id", "normalized", "language", "evidence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), index=True)
    label: Mapped[str] = mapped_column(Text)
    normalized: Mapped[str] = mapped_column(String(1024), index=True)
    language: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[str] = mapped_column(String(512))


class IngredientDictionary(Base):
    __tablename__ = "ingredient_dictionary"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cache_path: Mapped[str] = mapped_column(Text)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProductIngredient(Base):
    __tablename__ = "product_ingredient"
    __table_args__ = (UniqueConstraint("product_id", "ingredient_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), index=True)
    source_text: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[str] = mapped_column(Text)
    dictionary_version: Mapped[str] = mapped_column(String(64))


class ProductIngredientResolution(Base):
    __tablename__ = "product_ingredient_resolution"

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    components: Mapped[list[Any]] = mapped_column(JSON, default=list)
    dictionary_version: Mapped[str] = mapped_column(String(64))


class IngredientOverride(Base):
    __tablename__ = "ingredient_override"
    __table_args__ = (UniqueConstraint("source_id", "source_text"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64))
    source_text: Mapped[str] = mapped_column(Text)
    ingredient_ids: Mapped[list[Any]] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(Text)


class Price(Base):
    """Append-only price history: a new row whenever a published value changes."""

    __tablename__ = "price"
    __table_args__ = (
        Index("ix_price_series", "product_id", "series_key"),
        UniqueConstraint("observation_key", name="uq_price_observation_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    series_key: Mapped[str] = mapped_column(String(512))
    observation_key: Mapped[str] = mapped_column(String(64))

    price_type: Mapped[str] = mapped_column(String(32), index=True)
    price_variant: Mapped[str] = mapped_column(String(128), default="default")
    price_status: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    currency: Mapped[str] = mapped_column(String(3))
    price_unit: Mapped[str] = mapped_column(String(128))
    unit_text: Mapped[str | None] = mapped_column(String(255))
    pack_quantity: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    pack_unit: Mapped[str | None] = mapped_column(String(128))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    unit_price_unit: Mapped[str | None] = mapped_column(String(128))

    vat_included: Mapped[bool | None] = mapped_column(Boolean)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    dispensing_fee: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    statutory_reduction_included: Mapped[bool | None] = mapped_column(Boolean)

    valid_from: Mapped[date | None] = mapped_column(Date, index=True)
    valid_to: Mapped[date | None] = mapped_column(Date)
    date_basis: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_scope: Mapped[str] = mapped_column(String(64), default="national")
    notes: Mapped[str | None] = mapped_column(Text)

    snapshot_id: Mapped[str] = mapped_column(String(64), index=True)
    row_ref: Mapped[str | None] = mapped_column(String(512))
    quality_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)

    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[int | None] = mapped_column(Integer)
    first_seen_job_id: Mapped[str | None] = mapped_column(String(32))
    last_seen_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="prices")


class IdentifierCrosswalk(Base):
    """Generic identifier equivalences (e.g. NDC ↔ HCPCS from CMS crosswalk files)."""

    __tablename__ = "identifier_crosswalk"
    __table_args__ = (
        UniqueConstraint("from_type", "from_id", "to_type", "to_id", "snapshot_id", name="uq_identifier_crosswalk"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_type: Mapped[str] = mapped_column(String(64))
    from_id: Mapped[str] = mapped_column(String(128), index=True)
    to_type: Mapped[str] = mapped_column(String(64))
    to_id: Mapped[str] = mapped_column(String(128), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str | None] = mapped_column(String(64))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FxRate(Base):
    __tablename__ = "fx_rate"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "rate_date", "rate_type", "source", name="uq_fx_rate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_currency: Mapped[str] = mapped_column(String(3))
    quote_currency: Mapped[str] = mapped_column(String(3), index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    rate_type: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
