"""US – CMS Medicare Part B ASP drug payment limit files (quarterly ZIP).

HCPCS codes are billing codes, NOT national products: records are stored with product_level=billing_code.
The NDC↔HCPCS crosswalk ZIP of the same quarter is kept in the raw snapshot (role 'crosswalk').
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime

from app.adapters._common import join_notes
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError
from app.core.parsing import (
    clean_str,
    decode_bytes,
    dedupe_headers,
    find_header_row,
    html_links,
    iter_xls_rows,
    month_number,
    parse_decimal,
    profile_rows,
)
from app.core.schema import ExpectedSchema
from app.core.types import (
    ArtifactFile,
    NormalizedRecord,
    ParsedDataset,
    ParsedRecord,
    PriceFact,
    RawSnapshot,
    SourceArtifact,
    SourceProductData,
)

PAGE = "https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files"
QUARTER_RE = re.compile(r"/files/zip/(?P<month>january|april|july|october)-(?P<year>\d{4})-(?P<rest>[^\"']*)\.zip$", re.IGNORECASE)
PERIOD_RE = re.compile(r"Effective\s+(\w+ \d{1,2}, \d{4})\s+through\s+(\w+ \d{1,2}, \d{4})", re.IGNORECASE)
REQUIRED = ["HCPCS Code", "Short Description", "HCPCS Code Dosage", "Payment Limit"]


def _parse_long_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%B %d, %Y").date()


class UsAspAdapter(SourceAdapter):
    adapter_id = "us_asp"
    parser_type = "zip/csv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        payment: dict[tuple[int, int], str] = {}
        crosswalk: dict[tuple[int, int], str] = {}
        for url, _ in html_links(page.text, PAGE):
            m = QUARTER_RE.search(url)
            if not m:
                continue
            rest = m.group("rest").lower()
            key = (int(m.group("year")), month_number(m.group("month")) or 0)
            if any(x in rest for x in ("noc", "vaccine")):
                continue
            if "crosswalk" in rest:
                crosswalk.setdefault(key, url)
            elif "payment-limit" in rest or "asp-pricing" in rest:
                payment.setdefault(key, url)
        if not payment:
            raise DiscoveryError("No quarterly ASP payment limit ZIP link found on the CMS page", details={"page": PAGE})
        key = max(payment)
        files = [ArtifactFile(url=payment[key], filename=payment[key].rsplit("/", 1)[-1], role="main")]
        notes = []
        if key in crosswalk:
            files.append(ArtifactFile(url=crosswalk[key], filename=crosswalk[key].rsplit("/", 1)[-1], role="crosswalk"))
        else:
            notes.append("No NDC-HCPCS crosswalk link found for the selected quarter")
        quarter_start = date(key[0], key[1], 1)
        return SourceArtifact(self.source_id, files, published_at=quarter_start, edition=f"{quarter_start:%B %Y}", discovered_from=PAGE, notes=notes)

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", ["hcpcs_code", "short_description", "hcpcs_code_dosage", "payment_limit"], typed_columns=["payment_limit"])]

    def _read_member(self, zf: zipfile.ZipFile) -> tuple[str, list[list]]:
        names = zf.namelist()
        csvs = [n for n in names if n.lower().endswith(".csv") and "payment limit" in n.lower()]
        if csvs:
            text, _ = decode_bytes(zf.read(csvs[0]))
            return csvs[0], list(csv.reader(io.StringIO(text)))
        xls = [n for n in names if n.lower().endswith((".xls", ".xlsx")) and "payment limit" in n.lower()]
        if xls:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / Path(xls[0]).name
                p.write_bytes(zf.read(xls[0]))
                return xls[0], [[("" if v is None else str(v)) for v in row] for row in iter_xls_rows(p)]
        raise ParserError("ASP ZIP does not contain a 'Payment Limit' CSV/XLS file", details={"members": names})

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        path = snapshot.file("main").path
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ParserError(f"{path.name} is not a valid ZIP archive") from exc
        with zf:
            member, rows = self._read_member(zf)
        idx, raw_header = find_header_row(rows, REQUIRED)
        preamble = " ".join(" ".join(c for c in r if c) for r in rows[:idx])
        m = PERIOD_RE.search(preamble)
        period = (_parse_long_date(m.group(1)), _parse_long_date(m.group(2))) if m else (None, None)
        header = dedupe_headers(raw_header)
        while header and header[-1].startswith("col_"):
            header.pop()
        records, dict_rows = [], []
        for i, row in enumerate(rows[idx + 1 :]):
            data = dict(zip(header, [c.strip() for c in row]))
            if not clean_str(data.get("hcpcs_code")):
                continue
            dict_rows.append(data)
            records.append(ParsedRecord(row_ref=f"{member}:{idx + 2 + i}", key=data["hcpcs_code"], data=data))
        issues = []
        if period == (None, None):
            issues.append(self.issue(ParserError.error_type, "Effective period ('Effective … through …') not found in file preamble", stage="parse"))
        profile = profile_rows("main", header, dict_rows, row_count=len(records))
        return ParsedDataset(self.source_id, snapshot, records, [profile], total_rows=len(records), issues=issues, context={"period": period, "member": member})

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        start, end = dataset.context.get("period", (None, None))
        dosage = clean_str(d.get("hcpcs_code_dosage"))
        coinsurance = clean_str(d.get("co_insurance_percentage"))
        awp = clean_str(d.get("vaccine_awp"))
        product = SourceProductData(
            product_code=d["hcpcs_code"],
            code_type="HCPCS",
            product_level=ProductLevel.BILLING_CODE,
            name=clean_str(d.get("short_description")),
            strength_text=dosage,
            reimbursement_category="Medicare Part B",
            reimbursement_rate=f"co-insurance {coinsurance}%" if coinsurance else None,
            notes=join_notes(f"vaccine AWP {awp}%" if awp else None, clean_str(d.get("notes"))),
        )
        amount = parse_decimal(d.get("payment_limit"), decimal_sep=".")
        prices = []
        if amount is not None:
            prices.append(
                PriceFact(
                    amount=amount,
                    currency="USD",
                    price_type=PriceBasis.REIMBURSEMENT,
                    price_variant="asp_payment_limit",
                    price_status=PriceStatus.REGULATED,
                    price_unit="billing_unit",
                    unit_text=dosage or "HCPCS billing unit",
                    vat_included=None,
                    valid_from=start,
                    valid_to=end,
                    date_basis="explicit" if start else None,
                    coverage_scope=self.config.coverage_scope,
                    notes="Medicare Part B payment limit (generally 106% of ASP)",
                )
            )
        return NormalizedRecord(record.row_ref, product, prices)
