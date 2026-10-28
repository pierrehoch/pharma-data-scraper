from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from app.core.parsing import strip_accents


def fold(value: str | None) -> str:
    """NFKC + Latin accents removed + lowercase + collapsed whitespace (Japanese text preserved)."""
    if not value:
        return ""
    text = strip_accents(unicodedata.normalize("NFKC", value)).lower()
    return re.sub(r"\s+", " ", text).strip()


def fmt_decimal(value: Decimal | None) -> str | None:
    """'500.000' → '500', '0.50' → '0.5', never scientific notation."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


SIMPLE_NUMBER = r"\d+(?:[.,]\d+)?"
NUMBER = r"\d{1,3}(?:[ \u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"


def to_decimal(text: str) -> Decimal:
    return Decimal(re.sub(r"[ \u202f]", "", text).replace(",", "."))


def extract_brand(name: str | None) -> str | None:
    """'DUPIXENT 300 mg, solution…' → 'DUPIXENT'; 'DUPIXENT*2 penne…' (AIFA) → 'DUPIXENT'."""
    if not name:
        return None
    text = name.split("*", 1)[0] if "*" in name else name
    text = re.split(r"[,(–]| - |\d", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip(" .-/") or None
