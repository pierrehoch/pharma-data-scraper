"""Controlled vocabularies. Stored as plain strings in the DB for portability."""

from __future__ import annotations

from enum import StrEnum


class PriceBasis(StrEnum):
    """Level of the supply chain a price applies to: the axis on which prices of different sources are comparable.

    The rest of the semantics lives in other fields: maximum/cap → ``price_status=ceiling``; the source's own
    concept (PMC, Festbetrag, NADAC, Drug Tariff…) → ``price_variant``; taxes, dispensing fees and statutory
    rebates → ``vat_included`` / ``tax_rate`` / ``dispensing_fee`` / ``statutory_reduction_included``.
    """

    EX_FACTORY = "ex_factory"  # ex-manufacturer: price charged by the manufacturer (BR PF, IT ex-factory, ES PVL, ON DBP)
    WHOLESALE = "wholesale"  # pharmacy/hospital purchase price (US NADAC, UK NHS list price, DE clinic purchase)
    RETAIL = "retail"  # pharmacy price to the public, possibly net of statutory rebates (FR, IT, ES PVP, DE AVP, BR PMC)
    REIMBURSEMENT = "reimbursement"  # amount paid or capped by a payer (JP NHI, UK Drug Tariff, US ASP, Festbetrag…)
    PUBLIC_PROCUREMENT = "public_procurement"  # sales to public buyers/tenders (CN VBP, BR PMVG, IT prezzo di cessione)
    OTHER = "other"


class PriceStatus(StrEnum):
    OBSERVED = "observed"
    REGULATED = "regulated"
    CEILING = "ceiling"
    REFERENCE = "reference"
    CALCULATED = "calculated"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SCHEMA_CHANGED = "schema_changed"
    UNSUPPORTED = "unsupported"


class JobMode(StrEnum):
    NORMAL = "normal"
    DRY_RUN = "dry_run"
    RESUME = "resume"


class StageStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class ErrorType(StrEnum):
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    DISCOVERY_ERROR = "DISCOVERY_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    ENCODING_ERROR = "ENCODING_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AdapterStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    MANUAL_OR_UNSUPPORTED = "manual_or_unsupported"


class ProductLevel(StrEnum):
    """Granularity of what a national identifier designates."""

    PRESENTATION = "presentation"  # a marketed pack (CIP13, AIC, PZN, NDC, DIN+pack...)
    PRODUCT = "product"  # a product without pack (DIN, YJ code)
    BILLING_CODE = "billing_code"  # HCPCS: not a product
    REGULATORY_ENTRY = "regulatory_entry"  # e.g. AMNOG negotiation record
    DEVICE = "device"  # medical devices / accessories listed in drug files


class FxRateType(StrEnum):
    SPOT = "spot"
    MONTHLY_AVERAGE = "monthly_average"
    ANNUAL_AVERAGE = "annual_average"
    PPP = "ppp"
