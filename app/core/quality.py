"""Automatic data quality rules. They produce warnings – data is never deleted automatically."""

from __future__ import annotations

import re
from decimal import Decimal

from app.core.enums import ErrorType, Severity
from app.core.types import Issue, NormalizedDataset, NormalizedRecord, PriceFact

_CURRENCY = re.compile(r"^[A-Z]{3}$")
VARIATION_FACTOR = Decimal(10)

# Stable codes stored in Price.quality_flags. Values carrying any "unverified" code are published so the
# source figure stays visible, but their semantics were not fully established by the source.
NOT_CURRENT_FLAG = "not_current_value"
QUALITY_FLAG_LABELS = {
    "currency_inferred_cny": "Currency not stated in the source; CNY inferred from an official Chinese provincial publication",
    "price_basis_unknown": "Source does not state whether the amount is per pack or per unit; no per-unit price derived",
    "price_concept_unknown": "Price concept (listing, award, reimbursement, retail...) not identified by the source",
    "publication_date_unknown": "Source gives no publication or effective date; retrieval snapshot date used",
    "previous_value": "Previous price restated in the notice, not the new value",
    "proposed_value": "Proposed price, not confirmed as adopted",
    "withdrawn_value": "Withdrawn or cancelled price",
    "future_value": "Effective or publication date later than extraction date",
    NOT_CURRENT_FLAG: "Not a current price; shown for information and never marked as current",
    "price_variation_x10": "Abnormal variation (x10) against the previous observation of the same series",
}
UNVERIFIED_FLAGS = frozenset(QUALITY_FLAG_LABELS) - {"price_variation_x10"}


def _warn(message: str, rec: NormalizedRecord, rule: str, **details: object) -> Issue:
    return Issue(
        ErrorType.VALIDATION_ERROR,
        Severity.WARNING,
        message,
        stage="validation",
        row_ref=rec.row_ref,
        details={"rule": rule, "product_code": rec.product.product_code, **details},
    )


def check_record(rec: NormalizedRecord) -> list[Issue]:
    issues: list[Issue] = []
    if not rec.product.product_code:
        issues.append(_warn("Missing product code", rec, "product_code_exists"))
    if rec.product.pack_quantity is not None and rec.product.pack_quantity <= 0:
        issues.append(_warn(f"Pack quantity must be > 0 (got {rec.product.pack_quantity})", rec, "quantity_positive"))
    for fact in rec.prices:
        issues.extend(check_price(rec, fact))
    return issues


def check_price(rec: NormalizedRecord, fact: PriceFact) -> list[Issue]:
    issues: list[Issue] = []
    label = f"{fact.price_type}/{fact.price_variant}"
    if fact.amount is None or fact.amount <= 0:
        issues.append(_warn(f"Price must be > 0 ({label} = {fact.amount})", rec, "price_positive"))
    if not fact.currency or not _CURRENCY.match(fact.currency):
        issues.append(_warn(f"Invalid or missing currency {fact.currency!r} ({label})", rec, "currency_exists"))
    if fact.pack_quantity is not None and fact.pack_quantity <= 0:
        issues.append(_warn(f"Pack quantity must be > 0 ({label})", rec, "quantity_positive"))
    if fact.valid_from and fact.valid_to and fact.valid_from > fact.valid_to:
        issues.append(
            _warn(
                f"valid_from {fact.valid_from} is after valid_to {fact.valid_to} ({label})",
                rec,
                "effective_period_valid",
            )
        )
    return issues


def validate_dataset(dataset: NormalizedDataset) -> list[Issue]:
    issues: list[Issue] = []
    for rec in dataset.records:
        rec_issues = check_record(rec)
        rec.issues.extend(rec_issues)
        issues.extend(rec_issues)
    return issues


def variation_issue(rec: NormalizedRecord, fact: PriceFact, previous: Decimal) -> Issue | None:
    """Flag abnormal jumps against the previous observation of the same price series."""
    if previous is None or previous <= 0 or fact.amount is None or fact.amount <= 0:
        return None
    if fact.amount > previous * VARIATION_FACTOR or fact.amount < previous / VARIATION_FACTOR:
        return _warn(
            f"Abnormal price variation for {fact.price_type}/{fact.price_variant}: {previous} → {fact.amount} {fact.currency}",
            rec,
            "price_variation_x10",
            previous=str(previous),
            current=str(fact.amount),
        )
    return None
