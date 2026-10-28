"""Data contracts exchanged between pipeline stages and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.enums import ErrorType, PriceBasis, PriceStatus, ProductLevel, Severity


@dataclass
class Issue:
    error_type: ErrorType
    severity: Severity
    message: str
    stage: str | None = None
    row_ref: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactFile:
    url: str
    filename: str
    role: str = "main"
    published_at: date | None = None
    content_type: str | None = None


@dataclass
class SourceArtifact:
    source_id: str
    files: list[ArtifactFile]
    published_at: date | None = None
    edition: str | None = None
    discovered_from: str | None = None
    notes: list[str] = field(default_factory=list)
    partial: bool = False


@dataclass
class SnapshotFile:
    role: str
    filename: str
    path: Path
    source_url: str
    sha256: str
    size: int
    content_type: str | None = None
    requested_url: str | None = None


@dataclass
class RawSnapshot:
    id: str
    source_id: str
    retrieved_at: datetime
    directory: Path
    files: list[SnapshotFile]
    published_at: date | None = None
    edition: str | None = None
    parser_version: str | None = None
    partial: bool = False
    reused: bool = False
    duplicate_of: str | None = None

    def file(self, role: str = "main") -> SnapshotFile:
        for f in self.files:
            if f.role == role:
                return f
        raise KeyError(f"Snapshot {self.id} has no file with role '{role}' (roles: {[f.role for f in self.files]})")

    def files_with_role_prefix(self, prefix: str) -> list[SnapshotFile]:
        return [f for f in self.files if f.role.startswith(prefix)]

    @property
    def primary(self) -> SnapshotFile:
        return self.files[0]


@dataclass
class TableProfile:
    role: str
    columns: list[str]
    column_types: dict[str, str]
    row_count: int


@dataclass
class ParsedRecord:
    row_ref: str
    key: str | None
    data: dict[str, Any]
    role: str = "main"


@dataclass
class ParsedDataset:
    source_id: str
    snapshot: RawSnapshot
    records: list[ParsedRecord]
    profiles: list[TableProfile] = field(default_factory=list)
    truncated: bool = False
    total_rows: int | None = None
    row_errors: int = 0
    issues: list[Issue] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class PriceFact:
    amount: Decimal
    currency: str
    price_type: PriceBasis
    price_status: PriceStatus
    price_unit: str  # 'pack' or the official unit (tablet, ml, unit, billing_unit...)
    unit_text: str | None = None  # unit exactly as published
    pack_quantity: Decimal | None = None
    pack_unit: str | None = None
    vat_included: bool | None = None
    tax_rate: Decimal | None = None  # percent, e.g. Brazilian ICMS rate
    dispensing_fee: Decimal | None = None
    statutory_reduction_included: bool | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    date_basis: str | None = None  # 'explicit' or 'edition_date' (list edition used as valid_from)
    published_at: date | None = None  # overrides the snapshot publication date
    coverage_scope: str = "national"
    price_variant: str = "default"
    notes: str | None = None
    quality_flags: tuple[str, ...] = ()  # published-with-warning reasons, see app.core.quality.QUALITY_FLAG_LABELS
    never_current: bool = False  # previous/proposed/withdrawn/future values: kept in history, never the current price


@dataclass
class SourceProductData:
    """Generic product fields; each source fills what it publishes."""

    product_code: str
    code_type: str
    product_level: ProductLevel = ProductLevel.PRESENTATION
    name: str | None = None
    brand_name: str | None = None
    ingredient: str | None = None
    strength_text: str | None = None
    strength_value: Decimal | None = None
    strength_unit: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    pack_text: str | None = None
    pack_quantity: Decimal | None = None
    pack_unit: str | None = None
    manufacturer: str | None = None
    atc_code: str | None = None
    therapeutic_class: str | None = None
    product_type: str | None = None
    group_code: str | None = None
    group_name: str | None = None
    authorisation_number: str | None = None
    ean: str | None = None
    legal_status: str | None = None
    authorisation_status: str | None = None
    marketing_status: str | None = None
    authorisation_date: date | None = None
    marketing_start_date: date | None = None
    marketing_end_date: date | None = None
    reimbursement_rate: str | None = None
    reimbursement_category: str | None = None
    reimbursement_conditions: str | None = None
    restrictions: str | None = None
    orphan: bool | None = None
    notes: str | None = None
    detail_url: str | None = None


PRODUCT_FIELDS = [f for f in SourceProductData.__dataclass_fields__]


@dataclass
class IngredientComponent:
    name: str
    namespace: str | None = None
    identifier: str | None = None


@dataclass
class NormalizedRecord:
    row_ref: str
    product: SourceProductData
    prices: list[PriceFact] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    ingredient_components: list[IngredientComponent] | None = None


@dataclass
class NormalizedDataset:
    source_id: str
    snapshot: RawSnapshot
    records: list[NormalizedRecord]
    failed_rows: int = 0
    issues: list[Issue] = field(default_factory=list)


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def blocking(self) -> bool:
        return any(i.error_type == ErrorType.SCHEMA_CHANGED and i.severity == Severity.ERROR for i in self.issues)


@dataclass
class RunHints:
    """Execution scope passed to adapters. `limit` is a hint for streaming parsers."""

    limit: int | None = None
    product_ids: set[str] | None = None
    dry_run: bool = False

    @property
    def early_stop_at(self) -> int | None:
        # Read a little more than the limit so that product-id filters and row errors don't starve the run.
        if self.limit is None or self.product_ids:
            return None
        return max(self.limit * 2, self.limit + 50)
