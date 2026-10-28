"""Price normalization: original values are never modified; a per-unit price is derived next to them."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.core.types import PriceFact

_UNIT_ALIASES = {
    "ea": "unit",
    "each": "unit",
    "gm": "g",
    "g": "g",
    "ml": "ml",
    "tablet": "tablet",
    "capsule": "capsule",
}
QUANT = Decimal("0.00000001")


def normalize_price(fact: PriceFact) -> tuple[Decimal | None, str | None]:
    """Return (normalized_price, normalized_unit).

    * price per pack with a known pack quantity → price per pack unit (8.42 EUR / 28 tablets → 0.3007 EUR/tablet)
    * price already expressed per official unit → kept, unit harmonized (EA → unit, GM → g)
    * unknown basis (``price_unit="unknown"``) or anything else → (None, None): no invented normalization
    """
    if fact.amount is None:
        return None, None
    if fact.price_unit == "unknown":
        return None, None
    if fact.price_unit == "pack":
        if fact.pack_quantity and fact.pack_quantity > 0 and fact.pack_unit:
            return (fact.amount / fact.pack_quantity).quantize(QUANT, rounding=ROUND_HALF_UP), fact.pack_unit
        return None, None
    unit = _UNIT_ALIASES.get(fact.price_unit.lower(), fact.price_unit)
    if unit in ("billing_unit",):
        return None, None
    return fact.amount.quantize(QUANT, rounding=ROUND_HALF_UP), unit
