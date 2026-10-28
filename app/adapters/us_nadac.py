"""US – Medicaid NADAC (National Average Drug Acquisition Cost).

Discovery uses the data.medicaid.gov (DKAN) search API: the dataset of the most recent year is selected
dynamically. Full runs download the CSV distribution; limited runs (--limit) may use the DKAN datastore
query API to fetch only the most recent rows (the snapshot is then flagged `partial`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlencode

from app.adapters._common import join_notes, parse_fields, read_table
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError
from app.core.parsing import clean_str, parse_date, parse_decimal, profile_rows
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
from app.observability import Progress, get_logger

log = get_logger(__name__)

SEARCH_URL = "https://data.medicaid.gov/api/1/search?fulltext=NADAC&page-size=50"
DATASTORE_URL = "https://data.medicaid.gov/api/1/datastore/query/{dataset_id}/0"
TITLE = re.compile(r"^NADAC \(National Average Drug Acquisition Cost\) (\d{4})$")
REQUIRED = ["ndc_description", "ndc", "nadac_per_unit", "effective_date", "pricing_unit", "as_of_date"]
DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d"]
UNIT = {"EA": "unit", "ML": "ml", "GM": "g"}
CLASSIFICATION = {"B": "brand", "G": "generic", "B-ANDA": "brand approved under ANDA"}


class UsNadacAdapter(SourceAdapter):
    adapter_id = "us_nadac"
    parser_type = "csv"
    parser_version = "1.0"

    def discover(self) -> SourceArtifact:
        payload = self.ctx.http.get_json(SEARCH_URL)
        results = payload.get("results") or {}
        candidates = []
        for item in results.values():
            m = TITLE.match((item.get("title") or "").strip())
            if not m:
                continue
            dists = item.get("distribution") or []
            urls = [(d.get("data") or d).get("downloadURL") for d in dists]
            urls = [u for u in urls if u and u.endswith(".csv")]
            if urls:
                candidates.append((int(m.group(1)), item, urls[0]))
        if not candidates:
            raise DiscoveryError("No 'NADAC (National Average Drug Acquisition Cost) <year>' dataset found via DKAN search")
        year, item, csv_url = max(candidates, key=lambda c: c[0])
        modified = item.get("modified")
        published = datetime.fromisoformat(modified.replace("Z", "+00:00")).date() if modified else None
        dataset_id = item.get("identifier")
        files = [ArtifactFile(url=csv_url, filename=csv_url.rsplit("/", 1)[-1], role="main", published_at=published)]
        notes = [f"dataset {dataset_id} ({year})"]
        partial = False
        if self.hints.limit and self.option("sample_via_api_when_limited", True) and dataset_id and not self.hints.product_ids:
            query = urlencode(
                {
                    "limit": min(self.hints.limit, 500),
                    "offset": 0,
                    "sorts[0][property]": "as_of_date",
                    "sorts[0][order]": "desc",
                    "keys": "true",
                    "schema": "false",
                    "count": "false",
                }
            )
            files = [ArtifactFile(url=DATASTORE_URL.format(dataset_id=dataset_id) + "?" + query, filename="nadac_sample.json", role="main", published_at=published)]
            notes.append("limited run: DKAN datastore query (most recent rows) instead of the full CSV")
            partial = True
        return SourceArtifact(self.source_id, files, published_at=published, edition=str(year), discovered_from=SEARCH_URL, notes=notes, partial=partial)

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", REQUIRED, typed_columns=["ndc", "nadac_per_unit"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        f = snapshot.file("main")
        if f.filename.endswith(".json"):
            try:
                payload = json.loads(f.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ParserError(f"NADAC datastore response is not valid JSON: {exc}") from exc
            rows = payload.get("results")
            if rows is None:
                raise ParserError("NADAC datastore response has no 'results' key", details={"keys": list(payload)})
            columns = list(rows[0].keys()) if rows else REQUIRED
            records = [
                ParsedRecord(row_ref=f"{f.filename}:{i + 1}", key=clean_str(r.get("ndc")), data={k: ("" if v is None else str(v)) for k, v in r.items()})
                for i, r in enumerate(rows)
            ]
            profile = profile_rows("main", columns, [r.data for r in records], row_count=len(records))
            total, row_errors = len(records), 0
        else:
            latest_only = self.option("latest_as_of_only", True)
            latest = self._latest_as_of(f.path) if latest_only else None
            records, profile, meta = read_table(
                f.path, role="main", delimiter=",", header_required=["NDC", "NADAC Per Unit"], key_column="ndc",
                stop_after=self.hints.early_stop_at,
                row_filter=(lambda r: r.get("as_of_date") == latest) if latest else None,
            )
            total, row_errors = meta["total_rows"], meta["row_errors"]
        truncated = snapshot.partial
        if self.option("latest_as_of_only", True) and records and f.filename.endswith(".json"):
            dates = {r.data.get("as_of_date") for r in records}
            latest_date = max((parse_date(d, DATE_FORMATS) for d in dates if d), default=None)
            records = [r for r in records if parse_date(r.data.get("as_of_date"), DATE_FORMATS) == latest_date]
        return ParsedDataset(self.source_id, snapshot, records, [profile], truncated=truncated, total_rows=total, row_errors=row_errors)

    @staticmethod
    def _latest_as_of(path) -> str | None:  # noqa: ANN001
        """First pass over the CSV to find the most recent 'As of Date' (raw string) without loading rows."""
        import csv

        log.info("Scanning %s for the most recent 'As of Date'…", path.name)
        progress = Progress(f"scan {path.name}", unit="rows", check_every=5000)
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.reader(fh)
            idx = None
            best, best_raw = None, None
            for row in reader:
                progress.update()
                if idx is None:
                    norm = [c.strip().lower() for c in row]
                    if "as of date" in norm:
                        idx = norm.index("as of date")
                    continue
                if len(row) <= idx or not row[idx].strip():
                    continue
                try:
                    d = parse_date(row[idx], DATE_FORMATS)
                except ParserError:
                    continue
                if best is None or d > best:
                    best, best_raw = d, row[idx].strip()
        log.info("Most recent 'As of Date' is %s: only those rows are kept", best_raw)
        return best_raw

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        desc = clean_str(d.get("ndc_description")) or ""
        # NADAC descriptions are "<INGREDIENT> <STRENGTH> <FORM>", e.g. "ATORVASTATIN 10 MG TABLET"
        ingredient = re.split(r"\s\d", desc, maxsplit=1)[0].strip() or None
        unit_raw = clean_str(d.get("pricing_unit")) or ""
        otc = (clean_str(d.get("otc")) or "").upper()
        classification = clean_str(d.get("classification_for_rate_setting"))
        generic_nadac = clean_str(d.get("corresponding_generic_drug_nadac_per_unit"))
        product = SourceProductData(
            product_code=d["ndc"],
            code_type="NDC11",
            product_level=ProductLevel.PRESENTATION,
            name=desc,
            ingredient=ingredient,
            legal_status={"Y": "OTC", "N": "Rx"}.get(otc),
            product_type=CLASSIFICATION.get(classification or "", classification),
            **parse_fields(strength_text=desc),
        )
        amount = parse_decimal(d.get("nadac_per_unit"), decimal_sep=".")
        prices = []
        if amount is not None:
            prices.append(
                PriceFact(
                    amount=amount,
                    currency="USD",
                    price_type=PriceBasis.WHOLESALE,
                    price_variant="nadac",
                    price_status=PriceStatus.OBSERVED,
                    price_unit=UNIT.get(unit_raw.upper(), unit_raw.lower() or "unit"),
                    unit_text=unit_raw,
                    vat_included=None,
                    valid_from=parse_date(d.get("effective_date"), DATE_FORMATS),
                    date_basis="explicit",
                    published_at=parse_date(d.get("as_of_date"), DATE_FORMATS),
                    coverage_scope=self.config.coverage_scope,
                    notes=join_notes(
                        "retail community pharmacy acquisition cost survey",
                        f"pharmacy type {d['pharmacy_type_indicator']}" if clean_str(d.get("pharmacy_type_indicator")) else None,
                        f"explanation code {d['explanation_code']}" if clean_str(d.get("explanation_code")) else None,
                        f"corresponding generic NADAC {generic_nadac}" if generic_nadac else None,
                    ),
                )
            )
        return NormalizedRecord(record.row_ref, product, prices)
