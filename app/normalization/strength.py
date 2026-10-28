"""Strength parsing: '500 mg', '0,5 g', '500,0 mg' → 500 mg; '5 mg/5 ml' → 1 mg/ml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.normalization.text import NUMBER, fmt_decimal, fold, to_decimal
from app.normalization.units import UNIT_PATTERN, to_canonical

_STRENGTH = re.compile(
    rf"(?<![\w.,])(?P<value>{NUMBER})\s*(?P<unit>{UNIT_PATTERN})(?![a-z])"
    rf"(?:\s*/\s*(?P<per_value>{NUMBER})?\s*(?P<per_unit>{UNIT_PATTERN}|dose|tab|tablet|capsule|comprime|compressa|comprimido)(?![a-z]))?",
    re.IGNORECASE,
)


@dataclass
class Strength:
    value: Decimal
    unit: str
    raw: str
    multiple: bool = False

    @property
    def label(self) -> str:
        return f"{fmt_decimal(self.value)} {self.unit}"


def parse_strength(text: str | None) -> Strength | None:
    """Return the first strength found, normalized to canonical units.

    Concentrations are expressed per single denominator unit (5 mg/5 ml → 1 mg/ml).
    `multiple=True` flags combination products (several strengths found).
    """
    if not text:
        return None
    folded = fold(text)
    matches = list(_STRENGTH.finditer(folded))
    if not matches:
        return None
    m = matches[0]
    value, unit = to_canonical(to_decimal(m.group("value")), m.group("unit"))
    if unit is None:
        return None
    per_unit_raw = m.group("per_unit")
    if per_unit_raw:
        per_value = to_decimal(m.group("per_value")) if m.group("per_value") else Decimal(1)
        per_value, per_unit = to_canonical(per_value, per_unit_raw)
        if per_unit == "mg":  # express solid concentrations per gram (10 mg/g, not 0.01 mg/mg)
            per_value, per_unit = per_value / 1000, "g"
        if per_value == 0:
            return None
        if per_unit in ("dose", "tab", "tablet", "capsule", "comprime", "compressa", "comprimido"):
            per_unit = None  # "x mg/tablet" is a plain per-unit strength
        if per_unit:
            value = value / per_value
            unit = f"{unit}/{per_unit}"
    value = Decimal(fmt_decimal(value) or "0")
    return Strength(value=value, unit=unit, raw=m.group(0), multiple=len(matches) > 1)
