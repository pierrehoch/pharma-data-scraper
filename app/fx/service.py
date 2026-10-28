"""Exchange rates: separate store, never applied to original prices. Rate types are never mixed."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import FxRateType
from app.core.http import HttpClientProtocol
from app.db.models import FxRate

ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_90D = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_HIST_ZIP = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
ECB_SOURCE = "ECB euro foreign exchange reference rates"
MAX_STALENESS_DAYS = 10


@dataclass
class ConvertedPrice:
    original_price: Decimal
    original_currency: str
    converted_price: Decimal
    target_currency: str
    fx_rate: Decimal
    fx_date: date
    fx_source: str
    fx_rate_type: str


def parse_ecb_xml(xml_bytes: bytes) -> list[tuple[date, str, Decimal]]:
    root = ET.fromstring(xml_bytes)
    out = []
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time"):
            day = datetime.strptime(cube.get("time"), "%Y-%m-%d").date()
            for rate in cube:
                if rate.get("currency") and rate.get("rate"):
                    out.append((day, rate.get("currency"), Decimal(rate.get("rate"))))
    return out


def parse_ecb_hist_csv(zip_bytes: bytes) -> list[tuple[date, str, Decimal]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8")
    lines = text.strip().splitlines()
    header = [h.strip() for h in lines[0].split(",")]
    out = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        day = datetime.strptime(cells[0], "%Y-%m-%d").date()
        for cur, val in zip(header[1:], cells[1:]):
            if cur and val and val != "N/A":
                out.append((day, cur, Decimal(val)))
    return out


def store_rates(session: Session, rows: list[tuple[date, str, Decimal]], *, source: str = ECB_SOURCE) -> int:
    existing = {
        (r.quote_currency, r.rate_date)
        for r in session.scalars(
            select(FxRate).where(FxRate.base_currency == "EUR", FxRate.source == source, FxRate.rate_type == FxRateType.SPOT.value)
        )
    }
    added = 0
    for day, cur, rate in rows:
        if (cur, day) in existing:
            continue
        session.add(FxRate(base_currency="EUR", quote_currency=cur, rate=rate, rate_date=day, rate_type=FxRateType.SPOT.value, source=source))
        existing.add((cur, day))
        added += 1
    session.flush()
    return added


def update_from_ecb(session: Session, http: HttpClientProtocol, *, history: bool = False) -> int:
    from pathlib import Path
    import tempfile

    if history:
        with tempfile.TemporaryDirectory() as tmp:
            res = http.download(ECB_HIST_ZIP, Path(tmp) / "hist.zip")
            rows = parse_ecb_hist_csv(res.path.read_bytes())
    else:
        rows = parse_ecb_xml(http.get_text(ECB_90D).text.encode("utf-8"))
    return store_rates(session, rows)


def _eur_rate(session: Session, currency: str, on: date, rate_type: str) -> tuple[Decimal, date, str] | None:
    if currency == "EUR":
        return Decimal(1), on, ECB_SOURCE
    row = session.scalars(
        select(FxRate)
        .where(
            FxRate.base_currency == "EUR",
            FxRate.quote_currency == currency,
            FxRate.rate_type == rate_type,
            FxRate.rate_date <= on,
        )
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    ).first()
    if row is None or (on - row.rate_date).days > MAX_STALENESS_DAYS:
        return None
    return Decimal(str(row.rate)), row.rate_date, row.source


def convert(
    session: Session,
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    on: date,
    *,
    rate_type: FxRateType = FxRateType.SPOT,
) -> ConvertedPrice | None:
    """Convert via EUR cross rates of the requested type only. Returns None when no rate is available."""
    if from_currency == to_currency:
        return ConvertedPrice(amount, from_currency, amount, to_currency, Decimal(1), on, "identity", rate_type.value)
    src = _eur_rate(session, from_currency, on, rate_type.value)
    dst = _eur_rate(session, to_currency, on, rate_type.value)
    if src is None or dst is None:
        return None
    rate = dst[0] / src[0]
    converted = (Decimal(amount) * rate).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    fx_date = min(src[1], dst[1])
    return ConvertedPrice(Decimal(amount), from_currency, converted, to_currency, rate.quantize(Decimal("0.0000000001")), fx_date, src[2], rate_type.value)
