"""Robust low-level parsing helpers shared by adapters (no source-specific logic here)."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.core.errors import EncodingError, ParserError
from app.core.types import TableProfile

# ----------------------------------------------------------------------------- encodings


def decode_bytes(data: bytes, *, declared: str | None = None) -> tuple[str, str]:
    """Decode bytes, validating the encoding instead of assuming it.

    Order: BOM → declared → strict UTF-8 → cp1252 → latin-1. Returns (text, encoding).
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    candidates = [declared] if declared else []
    candidates += ["utf-8", "cp1252", "latin-1"]
    for enc in candidates:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" in text[:10000]:
            raise EncodingError(f"Binary content (NUL bytes) found while decoding as {enc}")
        return text, enc
    raise EncodingError("Unable to decode content with utf-8, cp1252 or latin-1")


def read_text_file(path: Path, *, declared: str | None = None) -> tuple[str, str]:
    return decode_bytes(path.read_bytes(), declared=declared)


# ----------------------------------------------------------------------------- delimited text

DELIMITERS = [",", ";", "\t", "|"]


def sniff_delimiter(text: str, *, expected: str | None = None, sample_lines: int = 50) -> str:
    """Choose the delimiter producing the most consistent multi-column split.

    If `expected` is given, it is validated (not assumed): a ParserError is raised if
    another delimiter clearly fits better.
    """
    lines = [line for line in text.splitlines()[: sample_lines * 2] if line.strip()][:sample_lines]
    if not lines:
        raise ParserError("Empty file: cannot detect delimiter")
    scores: dict[str, tuple[int, int]] = {}
    for delim in DELIMITERS:
        counts = [len(row) for row in csv.reader(lines, delimiter=delim)]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        consistency = counts.count(mode)
        scores[delim] = (consistency if mode > 1 else 0, mode)
    best = max(scores, key=lambda d: scores[d])
    if expected is not None:
        if scores.get(expected, (0, 0))[0] == 0:
            raise ParserError(
                f"Expected delimiter {expected!r} does not split the file into columns (best guess {best!r})",
                details={"scores": {k: v for k, v in scores.items()}},
            )
        return expected
    if scores[best][0] == 0:
        raise ParserError("Could not detect a column delimiter", details={"scores": scores})
    return best


def read_delimited(text: str, delimiter: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def strip_accents(text: str) -> str:
    """Remove diacritics from Latin letters only (Japanese dakuten etc. are preserved)."""
    out = []
    for ch in text:
        decomposed = unicodedata.normalize("NFD", ch)
        if len(decomposed) > 1 and decomposed[0] < "\u0250":
            ch = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(ch)
    return "".join(out)


def slug(value: Any) -> str:
    """Normalize a column name: NFKC, accents removed, lowercase, non-alphanumerics → '_'."""
    if value is None:
        return ""
    text = strip_accents(unicodedata.normalize("NFKC", str(value)).strip()).lower()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    return text.strip("_")


def dedupe_headers(headers: Sequence[Any]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(headers):
        name = slug(h) or f"col_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def find_header_row(rows: Iterable[Sequence[Any]], required: Sequence[str], *, max_scan: int = 80) -> tuple[int, list[str]]:
    """Locate the header row containing all `required` column names (compared as slugs)."""
    req = [slug(r) for r in required]
    for idx, row in enumerate(rows):
        if idx >= max_scan:
            break
        names = [slug(c) for c in row]
        if all(r in names for r in req):
            return idx, list(row)
    raise ParserError(f"Header row with columns {list(required)} not found in the first {max_scan} rows")


def rows_to_dicts(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> Iterator[dict[str, Any]]:
    width = len(header)
    for row in rows:
        values = list(row)
        if len(values) < width:
            values += [None] * (width - len(values))
        yield dict(zip(header, values[:width]))


# ----------------------------------------------------------------------------- spreadsheets


def iter_xlsx_rows(path: Path, sheet: str | None = None) -> Iterator[tuple[Any, ...]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
        yield from ws.iter_rows(values_only=True)
    finally:
        wb.close()


def xlsx_sheet_names(path: Path) -> list[str]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def iter_xls_rows(path: Path, sheet: str | None = None) -> Iterator[tuple[Any, ...]]:
    import xlrd

    book = xlrd.open_workbook(str(path))
    ws = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
    for i in range(ws.nrows):
        yield tuple(ws.row_values(i))


# ----------------------------------------------------------------------------- values

_EMPTY = {"", "-", "--", "n/a", "na", "nd", "none", "null"}


def clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in _EMPTY:
        return None
    return re.sub(r"\s+", " ", text)


def parse_decimal(value: Any, *, decimal_sep: str | None = None) -> Decimal | None:
    """Parse numbers like '24,34', '2.264,69', '1,234.56', '1 234,5', '1,234,56' (BDPM bug), '€ 3,1'.

    `decimal_sep` disambiguates single separators (e.g. '1,234' is 1.234 if ',' else 1234).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParserError(f"Boolean is not a number: {value!r}")
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = clean_str(value)
    if text is None:
        return None
    if text.startswith("="):
        raise ParserError(f"Unevaluated spreadsheet formula instead of a number: {text!r}")
    text = re.sub(r"[^\d,.\-+]", "", unicodedata.normalize("NFKC", text))
    if not text or not re.search(r"\d", text):
        return None
    neg = text.startswith("-")
    text = text.lstrip("+-")
    n_comma, n_dot = text.count(","), text.count(".")
    if n_comma and n_dot:
        dec = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if dec == "," else ","
        text = text.replace(thousands, "").replace(dec, ".")
    elif n_comma > 1:
        head, _, tail = text.rpartition(",")
        text = head.replace(",", "") + "." + tail if len(tail) != 3 or decimal_sep == "," else text.replace(",", "")
    elif n_comma == 1:
        head, tail = text.split(",")
        if decimal_sep == "." and len(tail) == 3:
            text = head + tail
        else:
            text = head + "." + tail
    elif n_dot > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + ("." + tail if len(tail) != 3 or decimal_sep == "." else tail)
    elif n_dot == 1 and decimal_sep == ",":
        head, tail = text.split(".")
        if len(tail) == 3:
            text = head + tail
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ParserError(f"Invalid number: {value!r}") from exc
    return -result if neg else result


def parse_date(value: Any, formats: Sequence[str]) -> date | None:
    """Parse a date with explicit formats only (no ambiguous guessing between d/m and m/d)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_str(value)
    if text is None:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Unrecognized date {value!r} (expected formats {list(formats)})")


MONTHS = {
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # pt
    "janeiro": 1, "fevereiro": 2, "marco": 3, "maio": 5, "junho": 6, "julho": 7,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # it
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "settembre": 9, "ottobre": 10, "dicembre": 12,
}


def month_number(name: str) -> int | None:
    return MONTHS.get(slug(name))


# ----------------------------------------------------------------------------- profiling

_INT = re.compile(r"^-?\d+$")
_DEC = re.compile(r"^-?[\d.,\s]*\d[,.]\d+$")
_DATE = re.compile(r"^(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}-[A-Za-z]{3}-\d{4})$")


def infer_type(values: Iterable[Any]) -> str:
    kinds: dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            k = "text"
        elif isinstance(v, int):
            k = "integer"
        elif isinstance(v, (float, Decimal)):
            k = "decimal"
        elif isinstance(v, (date, datetime)):
            k = "date"
        else:
            s = str(v).strip()
            if not s:
                continue
            if _INT.match(s):
                k = "integer"
            elif _DEC.match(s):
                k = "decimal"
            elif _DATE.match(s):
                k = "date"
            else:
                k = "text"
        kinds[k] = kinds.get(k, 0) + 1
    if not kinds:
        return "empty"
    if set(kinds) <= {"integer", "decimal"} and "decimal" in kinds:
        return "decimal"
    total = sum(kinds.values())
    best = max(kinds, key=kinds.get)  # type: ignore[arg-type]
    # Identifier-like columns mixing numbers and codes are treated as text.
    return best if kinds[best] / total >= 0.9 else "text"


def profile_rows(role: str, columns: Sequence[str], rows: Sequence[dict[str, Any]], *, row_count: int, sample: int = 500) -> TableProfile:
    sample_rows = rows[:sample]
    types = {c: infer_type(r.get(c) for r in sample_rows) for c in columns}
    return TableProfile(role=role, columns=list(columns), column_types=types, row_count=row_count)


# ----------------------------------------------------------------------------- html


def html_links(html: str, base_url: str) -> list[tuple[str, str]]:
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        out.append((urljoin(base_url, a["href"].strip()), a.get_text(" ", strip=True)))
    return out
