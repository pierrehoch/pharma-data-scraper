"""Unit vocabulary: mass → mg, volume → ml, international units → IU."""

from __future__ import annotations

from decimal import Decimal

from app.normalization.text import fold

_MASS = {
    "mg": Decimal(1), "milligram": Decimal(1), "milligrams": Decimal(1), "milligramme": Decimal(1),
    "milligrammes": Decimal(1), "milligrammi": Decimal(1), "miligramos": Decimal(1),
    "g": Decimal(1000), "gr": Decimal(1000), "gm": Decimal(1000), "gram": Decimal(1000), "grams": Decimal(1000),
    "gramme": Decimal(1000), "grammes": Decimal(1000), "grammi": Decimal(1000), "gramos": Decimal(1000),
    "kg": Decimal(1000000),
    "mcg": Decimal("0.001"), "µg": Decimal("0.001"), "μg": Decimal("0.001"), "ug": Decimal("0.001"),
    "microgram": Decimal("0.001"), "micrograms": Decimal("0.001"), "microgramme": Decimal("0.001"),
    "microgrammes": Decimal("0.001"), "microgrammi": Decimal("0.001"), "microgramos": Decimal("0.001"),
    "ng": Decimal("0.000001"),
}
_VOLUME = {
    "ml": Decimal(1), "millilitre": Decimal(1), "milliliter": Decimal(1), "millilitres": Decimal(1),
    "milliliters": Decimal(1), "mililitros": Decimal(1), "millilitri": Decimal(1),
    "l": Decimal(1000), "litre": Decimal(1000), "liter": Decimal(1000), "litro": Decimal(1000),
    "dl": Decimal(100), "cl": Decimal(10), "µl": Decimal("0.001"), "μl": Decimal("0.001"), "ul": Decimal("0.001"),
}
_IU = {"iu", "ui", "u.i.", "i.u.", "u.i", "i.u", "unit", "units", "unita", "unidades", "unites", "単位", "ie", "e.i."}
_OTHER = {"%": "%", "mmol": "mmol", "meq": "meq", "kbq": "kbq", "mbq": "mbq", "ch": "ch", "dh": "dh"}

UNIT_PATTERN = (
    r"(?:microgrammes?|micrograms?|microgrammi|microgramos|milligrammes?|milligrams?|milligrammi|miligramos|"
    r"grammes?|grams?|grammi|gramos|millilitres?|milliliters?|mcg|µg|μg|ug|mg|kg|ng|g|ml|µl|μl|ul|dl|cl|l|"
    r"u\.i\.|i\.u\.|ui|iu|units?|unidades|unita|mmol|meq|%|単位)"
)


def normalize_unit(raw: str | None) -> tuple[str | None, Decimal]:
    """Return (canonical_unit, factor) so that value_in_canonical = value * factor."""
    if not raw:
        return None, Decimal(1)
    unit = fold(raw).strip(" .")
    if unit in _MASS:
        return "mg", _MASS[unit]
    if unit in _VOLUME:
        return "ml", _VOLUME[unit]
    if unit in _IU or unit.rstrip(".") in _IU:
        return "IU", Decimal(1)
    if unit in _OTHER:
        return _OTHER[unit], Decimal(1)
    return unit or None, Decimal(1)


def to_canonical(value: Decimal, unit: str | None) -> tuple[Decimal, str | None]:
    canonical, factor = normalize_unit(unit)
    return value * factor, canonical
