"""Dosage form vocabulary, used only to guess the unit of a pack size ('30' of what?)."""

from __future__ import annotations

import re

from app.normalization.text import fold


def _w(*words: str) -> str:
    """Latin words/abbreviations matched as whole tokens."""
    return r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?![a-z])"


def _cjk(*words: str) -> str:
    return "(?:" + "|".join(re.escape(w) for w in words) + ")"


# Order matters: most specific first. Ambiguous national abbreviations (e.g. BR 'COM', DE 'SAL')
# are NOT listed here: adapters pass them as `extra` patterns so they only apply to their source.
_FORMS: list[tuple[str, str]] = [
    ("prolonged-release tablet", _w(r"comprimes? (?:a|à) liberation prolongee", r"lp", r"er tab\w*", r"extended[- ]release", r"prolonged[- ]release", r"rilascio prolungato", r"liberacion prolongada", r"retardtablette\w*")
     + "|" + _cjk("徐放錠", "徐放")),
    ("gastro-resistant tablet", _w(r"gastro-?resistant\w*", r"gastrorresistentes?", r"gastroresistent\w*", r"gastro-?resistente?s?", r"magensaftresistent\w*", r"ec tab\w*", r"dr tab\w*", r"delayed[- ]release")
     + "|" + _cjk("腸溶錠")),
    ("orodispersible tablet", _w(r"orodispersible\w*", r"bucodispersables?", r"orodispersibil\w*", r"odt", r"schmelztablette\w*") + "|" + _cjk("口腔内崩壊錠", "OD錠", "od錠")),
    ("effervescent tablet", _w(r"effervescent\w*", r"efervescentes?", r"brausetablette\w*")),
    ("powder for injection", _w(r"poudre pour solution (?:injectable|pour perfusion)", r"polvere per soluzione (?:iniettabile|per infusione)", r"polvo para solucion inyectable", r"pulver zur herstellung einer (?:injektions|infusions)l\w*", r"powder for solution for injection")),
    ("eye drops", _w(r"collyre", r"collirio", r"colirio", r"augentropfen", r"eye drops?", r"ophthalmic solution") + "|" + _cjk("点眼")),
    ("nasal spray", _w(r"pulverisation nasale", r"spray nasale", r"nebulizador nasal", r"nasal spray", r"nasenspray") + "|" + _cjk("点鼻")),
    ("inhalation", _w(r"inhalation", r"inhalateur", r"inalatore", r"inalazione", r"inhalador", r"inhaler", r"dosieraerosol", r"aerosol", r"pressurised inhalation", r"suspension pour inhalation") + "|" + _cjk("吸入")),
    ("injection", _w(r"solution injectable", r"suspension injectable", r"solution pour perfusion", r"soluzione iniettabile", r"sospensione iniettabile", r"soluzione per infusione", r"solucion inyectable", r"suspension inyectable", r"solucion para perfusion", r"injection", r"injectable", r"infusion", r"perfusion", r"injektionsl\w*", r"infusionsl\w*", r"ampoules?", r"ampolla", r"fiala", r"flaconcino", r"vials?", r"pre-?filled", r"seringues?", r"siringa", r"jeringa", r"syringes?", r"stylo", r"penna", r"fertigspritze\w*", r"iniettabile", r"inyectable")
     + "|" + _cjk("注射", "注")),
    ("capsule", _w(r"capsules?", r"capsule molle", r"caps", r"gelules?", r"capsula", r"capsulas", r"cps", r"hartkapsel\w*", r"weichkapsel\w*", r"kapsel\w*") + "|" + _cjk("カプセル")),
    ("tablet", _w(r"tablets?", r"tab", r"tabs", r"comprimes?", r"comprime\(s\)", r"compresse", r"compressa", r"compresse rivestite", r"cpr", r"comprimidos?", r"filmtablette\w*", r"tablette\w*", r"film-coated tablets?") + "|" + _cjk("錠")),
    ("syrup", _w(r"sirop", r"sciroppo", r"jarabe", r"xarope", r"syrup") + "|" + _cjk("シロップ")),
    ("oral suspension", _w(r"suspension buvable", r"sospensione orale", r"suspension oral", r"oral suspension")),
    ("oral solution", _w(r"solution buvable", r"soluzione orale", r"solucion oral", r"oral solution", r"loesung zum einnehmen") + "|" + _cjk("内用液")),
    ("drops", _w(r"gouttes", r"gocce", r"gotas", r"tropfen", r"drops")),
    ("granules", _w(r"granules?", r"granulat\w*", r"granulado", r"granulato") + "|" + _cjk("顆粒")),
    ("powder", _w(r"poudre", r"polvere", r"polvo", r"powder", r"pulver") + "|" + _cjk("散")),
    ("cream", _w(r"creme", r"crema", r"cream") + "|" + _cjk("クリーム")),
    ("ointment", _w(r"pommade", r"pomata", r"pomada", r"salbe", r"ointment") + "|" + _cjk("軟膏")),
    ("gel", _w(r"gel", r"gele") + "|" + _cjk("ゲル")),
    ("patch", _w(r"patch", r"patchs", r"dispositif transdermique", r"cerotto", r"cerotti", r"parches?", r"pflaster", r"transdermal patch") + "|" + _cjk("テープ", "パップ", "貼付")),
    ("suppository", _w(r"suppositoires?", r"supposte", r"supposta", r"supositorios?", r"zapfchen", r"zaepfchen", r"suppository") + "|" + _cjk("坐剤", "坐薬")),
    ("solution", _w(r"solution", r"soluzione", r"solucion", r"loesung", r"losung") + "|" + _cjk("液")),
]
_FORM_RE = [(form, re.compile(pattern, re.IGNORECASE)) for form, pattern in _FORMS]

CANONICAL_FORMS = [form for form, _ in _FORMS]


def compile_extra(patterns: list[tuple[str, list[str]]]) -> list[tuple[str, re.Pattern[str]]]:
    """Build source-specific abbreviation patterns: [(canonical_form, [token, ...]), ...]."""
    return [(form, re.compile(_w(*tokens), re.IGNORECASE)) for form, tokens in patterns]


def normalize_form(*texts: str | None, extra: list[tuple[str, re.Pattern[str]]] | None = None) -> str | None:
    """Return the first canonical form found, trying texts in order of reliability."""
    for text in texts:
        folded = fold(text)
        if not folded:
            continue
        for form, regex in (extra or []) + _FORM_RE:
            if regex.search(folded):
                return form
    return None
