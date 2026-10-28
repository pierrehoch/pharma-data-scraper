"""unify price types on supply-chain levels

price_type is now one of ex_factory / wholesale / retail / reimbursement / public_procurement / other.
Ceilings move to price_status, the source concept to price_variant. series_key and observation_key are
recomputed so existing histories continue seamlessly.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 15:40:00
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

import sqlalchemy as sa
from alembic import op


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

# (source_id, old price_type) -> (new price_type, new variant when the old one was "default", new status or None)
RULES: dict[tuple[str, str], tuple[str, str | None, str | None]] = {
    ("br_cmed", "maximum_consumer_price"): ("retail", None, None),
    ("br_cmed", "maximum_government_price"): ("public_procurement", None, None),
    ("ca_bc_pharmacare", "reimbursement_ceiling"): ("reimbursement", None, None),
    ("ca_on_odb", "list_price"): ("ex_factory", None, None),
    ("cn_nhsa", "procurement_price"): ("public_procurement", None, None),
    ("cn_vbp", "procurement_price"): ("public_procurement", None, None),
    ("de_festbetrag", "retail_public"): ("retail", None, None),
    ("de_festbetrag", "reimbursement_ceiling"): ("reimbursement", None, None),
    ("de_gba", "retail_public"): ("retail", None, None),
    ("de_gba", "other"): ("retail", None, None),
    ("de_gba", "acquisition_cost"): ("wholesale", None, None),
    ("es_nomenclator", "retail_public"): ("retail", None, None),
    ("es_nomenclator", "reimbursement_ceiling"): ("reimbursement", None, "ceiling"),
    ("fr_bdpm", "retail_public"): ("retail", None, None),
    ("it_aifa", "retail_public"): ("retail", None, None),
    ("it_aifa", "maximum_government_price"): ("public_procurement", "prezzo_massimo_di_cessione", None),
    ("uk_dmd", "tariff_reimbursement"): ("reimbursement", "drug_tariff_basic_price", None),
    ("uk_dmd", "list_price"): ("wholesale", "nhs_indicative_price", None),
    ("uk_drug_tariff", "tariff_reimbursement"): ("reimbursement", "drug_tariff_basic_price", None),
    ("us_asp", "medicare_payment"): ("reimbursement", "asp_payment_limit", None),
    ("us_nadac", "acquisition_cost"): ("wholesale", "nadac", None),
}
# Any other source still carrying a legacy value.
FALLBACK = {
    "acquisition_cost": "wholesale",
    "wholesale_price": "wholesale",
    "list_price": "wholesale",
    "retail_public": "retail",
    "maximum_consumer_price": "retail",
    "reimbursement_ceiling": "reimbursement",
    "medicare_payment": "reimbursement",
    "tariff_reimbursement": "reimbursement",
    "maximum_government_price": "public_procurement",
    "procurement_price": "public_procurement",
}
CEILING_TYPES = {"reimbursement_ceiling", "maximum_consumer_price", "maximum_government_price"}

# Inverse mapping for downgrade: (source_id, new type, new variant or None=any) -> (old type, old variant or None=keep, old status)
DOWN_RULES: list[tuple[tuple[str, str, str | None], tuple[str, str | None, str | None]]] = [
    (("de_gba", "retail", "net_after_statutory_rebates"), ("other", None, None)),
    (("es_nomenclator", "reimbursement", None), ("reimbursement_ceiling", None, "reference")),
] + [
    ((src, new, variant), (old, "default" if variant else None, None))
    for (src, old), (new, variant, _status) in RULES.items()
    if not (src == "de_gba" and old == "other") and not (src == "es_nomenclator" and old == "reimbursement_ceiling")
]


def _fmt(value) -> str:  # noqa: ANN001 - mirrors app.normalization.text.fmt_decimal
    if value is None:
        return ""
    text = format(Decimal(str(value)).normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _bool(value) -> str:  # noqa: ANN001
    return str(None if value is None else bool(value))


def _date(value) -> str:  # noqa: ANN001
    return "" if value is None else value if isinstance(value, str) else value.isoformat()


def _rewrite(resolve) -> None:  # noqa: ANN001
    """Apply resolve(row) -> (type, variant, status) | None to every price row, recomputing the hashed keys."""
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, product_id, source_id, price_type, price_variant, price_status, price_unit, coverage_scope, "
        "valid_from, valid_to, CAST(amount AS TEXT) AS amount, currency, vat_included, statutory_reduction_included, "
        "CAST(pack_quantity AS TEXT) AS pack_quantity FROM price"
    )).mappings().all()
    updates = []
    for r in rows:
        target = resolve(r)
        if target is None:
            continue
        ptype, variant, status = target
        skey = f"{ptype}|{variant}|{r['price_unit']}|{r['coverage_scope']}"
        parts = [
            str(r["product_id"]), skey, _date(r["valid_from"]), _date(r["valid_to"]), _fmt(r["amount"]),
            r["currency"], _bool(r["vat_included"]), _bool(r["statutory_reduction_included"]), _fmt(r["pack_quantity"]),
        ]
        okey = hashlib.sha256("|".join(parts).encode()).hexdigest()
        updates.append({"id": r["id"], "t": ptype, "v": variant, "s": status, "sk": skey, "ok": okey})
    stmt = sa.text(
        "UPDATE price SET price_type = :t, price_variant = :v, price_status = :s, series_key = :sk, "
        "observation_key = :ok WHERE id = :id"
    )
    for i in range(0, len(updates), 5000):
        conn.execute(stmt, updates[i:i + 5000])


def upgrade() -> None:
    def resolve(r):  # noqa: ANN001, ANN202
        old = r["price_type"]
        rule = RULES.get((r["source_id"], old))
        if rule is None:
            if old not in FALLBACK:
                return None
            rule = (FALLBACK[old], None, None)
        ptype, variant, status = rule
        new_variant = variant if variant and r["price_variant"] == "default" else r["price_variant"]
        new_status = status or ("ceiling" if old in CEILING_TYPES and r["price_status"] == "reference" else r["price_status"])
        return ptype, new_variant, new_status

    _rewrite(resolve)


def downgrade() -> None:
    def resolve(r):  # noqa: ANN001, ANN202
        for (src, new, variant), (old, old_variant, old_status) in DOWN_RULES:
            if r["source_id"] == src and r["price_type"] == new and (variant is None or r["price_variant"] == variant):
                return old, old_variant or r["price_variant"], old_status or r["price_status"]
        return None

    _rewrite(resolve)
