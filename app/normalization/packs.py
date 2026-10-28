"""Pack quantity parsing: '28 comprimés', '28 tbl', '2 plaquettes de 14', 'X 40 G' → (quantity, unit)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.normalization.text import SIMPLE_NUMBER as NUMBER, fold, to_decimal

# canonical unit → tokens (folded). Containers (box, bottle, tube, blister) are deliberately absent.
PACK_UNITS: dict[str, list[str]] = {
    "tablet": [r"comprimes?", r"comprime\(s\)", r"compresse", r"compressa", r"cpr", r"comprimidos?", r"tablets?", r"tabs?", r"tbl", r"tabletten", r"filmtabletten", r"com", r"comp"],
    "capsule": [r"gelules?", r"gelule\(s\)", r"capsules?", r"capsule\(s\)", r"capsulas?", r"cps", r"caps?", r"kapseln"],
    "unit-dose container": [r"recipients? unidoses?", r"recipient\(s\) unidose\(s\)", r"unidoses?", r"monodoses?", r"contenitori monodose"],
    "sachet": [r"sachets?", r"sachet\(s\)", r"bustine", r"bustina", r"sobres?", r"sch", r"envelopes?", r"env"],
    "vial": [r"flacons?-ampoules?", r"flacon\(s\)-ampoule\(s\)", r"flaconcin[oi]", r"viales?", r"vials?", r"fa", r"fr amp", r"durchstechflasche\w*"],
    "ampoule": [r"ampoules?", r"ampoule\(s\)", r"fiale", r"fiala", r"ampollas?", r"amp", r"ampullen"],
    "syringe": [r"seringues?", r"seringue\(s\)", r"siringhe", r"siringa", r"jeringas?", r"syringes?", r"ser", r"fertigspritzen", r"pre-?filled (?:syringes?|disposable injection devices?)"],
    "pen": [r"stylos?", r"stylo\(s\)", r"penne", r"penna", r"plumas?", r"pens?", r"canetas?", r"car", r"pre-?filled pens?"],
    "patch": [r"patchs?", r"dispositifs? transdermiques?", r"cerotti", r"cerotto", r"parches?", r"patches", r"pflaster", r"adesivos?"],
    "suppository": [r"suppositoires?", r"suppositoire\(s\)", r"supposte", r"supositorios?", r"suppositories", r"zapfchen", r"sup"],
    "dose": [r"doses?", r"dosi", r"dosis", r"actuations?", r"puffs?", r"hub"],
    "cartridge": [r"cartouches?", r"cartucce", r"cartuchos?", r"cartridges?"],
    "ml": [r"ml", r"millilitres?"],
    "g": [r"g", r"gr", r"grammes?", r"grammi", r"gramos"],
    "unit": [r"unita'?", r"unidades", r"units?", r"pieces?", r"stuck", r"st"],
}
_DISCRETE_FIRST = [u for u in PACK_UNITS if u not in ("ml", "g", "unit")] + ["ml", "g", "unit"]


def _alt(unit: str) -> str:
    return "(?:" + "|".join(PACK_UNITS[unit]) + ")"


_UNIT_RES = {
    unit: re.compile(rf"(?<![\w.,/])(?P<qty>{NUMBER})\s*{_alt(unit)}(?![a-z])", re.IGNORECASE) for unit in PACK_UNITS
}
_ANY_UNIT = "(?:" + "|".join(_alt(u) for u in _DISCRETE_FIRST) + ")"
# '2 plaquette(s) ... de 14 comprimé(s)', '3 x 28 tablets', 'X 40 G' (Brazil), '*84 cpr' (Italy)
_MULTI = re.compile(rf"(?<![\w.,])(?P<n>\d+)\s*(?:x|×)\s*(?P<qty>{NUMBER})\s*(?P<unit>[a-z(),' -]*)", re.IGNORECASE)
_CONTAINERS = re.compile(
    rf"(?<![\w.,])(?P<n>\d+)\s+(?:plaquettes?|plaquette\(s\)|blisters?|blister\(s\)|etuis?|boites?|flacons?|flacon\(s\)|tubes?|tube\(s\)|sachets?|bolsas?|envases?\s+unidosis)\b[^0-9]*?\bde\s+(?P<qty>{NUMBER})\s*(?P<unit>{_ANY_UNIT})",
    re.IGNORECASE,
)
_X_QTY = re.compile(rf"(?<![a-z])x\s*(?P<qty>{NUMBER})\s*(?P<unit>[a-z]+)?", re.IGNORECASE)


def _pack_decimal(value: str, decimal_sep: str | None) -> Decimal:
    if decimal_sep not in (None, ",", "."):
        raise ValueError("decimal_sep must be ',', '.' or None")
    if decimal_sep is None:
        return to_decimal(value)
    grouping_sep = "." if decimal_sep == "," else ","
    normalized = value.replace(" ", "").replace("\u202f", "").replace(grouping_sep, "")
    return Decimal(normalized.replace(decimal_sep, "."))


@dataclass
class Pack:
    quantity: Decimal
    unit: str
    raw: str


def canonical_pack_unit(token: str | None) -> str | None:
    token = fold(token).strip(" .(),'")
    if not token:
        return None
    for unit in _DISCRETE_FIRST:
        if re.fullmatch(_alt(unit), token, re.IGNORECASE):
            return unit
    return None


def parse_pack(text: str | None, *, default_unit: str | None = None, decimal_sep: str | None = None) -> Pack | None:
    """Extract the pack content. Discrete units win over volume/mass; containers are multiplied out."""
    folded = fold(text)
    if not folded:
        return None
    m = _CONTAINERS.search(folded)
    if m:
        unit = canonical_pack_unit(m.group("unit")) or default_unit
        if unit:
            return Pack(Decimal(m.group("n")) * _pack_decimal(m.group("qty"), decimal_sep), unit, m.group(0))
    m = _MULTI.search(folded)
    if m:
        unit = canonical_pack_unit(m.group("unit").split(" ")[0] if m.group("unit") else None) or default_unit
        if unit:
            return Pack(Decimal(m.group("n")) * _pack_decimal(m.group("qty"), decimal_sep), unit, m.group(0))
    for unit in _DISCRETE_FIRST:
        m = _UNIT_RES[unit].search(folded)
        if m:
            return Pack(_pack_decimal(m.group("qty"), decimal_sep), unit, m.group(0))
    m = _X_QTY.search(folded)
    if m:
        unit = canonical_pack_unit(m.group("unit")) or default_unit
        if unit:
            return Pack(_pack_decimal(m.group("qty"), decimal_sep), unit, m.group(0))
    return None


FORM_TO_PACK_UNIT = {
    "tablet": "tablet",
    "prolonged-release tablet": "tablet",
    "gastro-resistant tablet": "tablet",
    "orodispersible tablet": "tablet",
    "effervescent tablet": "tablet",
    "capsule": "capsule",
    "suppository": "suppository",
    "patch": "patch",
}
