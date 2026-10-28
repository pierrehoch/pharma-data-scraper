"""Germany – GKV-Spitzenverband overview of AMNOG reimbursement-amount negotiations (§130b SGB V).

PARTIALLY SUPPORTED: the public table gives substance, trade names, company, negotiation status and date –
the negotiated Erstattungsbeträge themselves are NOT published there, so no price is created (never invented).
The paginated HTML table is fetched politely page by page (stops early for --limit runs).
"""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError
from app.core.parsing import clean_str, parse_date, profile_rows, slug
from app.core.schema import ExpectedSchema
from app.core.snapshots import DownloadedFile
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData

PAGE = "https://www.gkv-spitzenverband.de/krankenversicherung/arzneimittel/verhandlungen_nach_amnog/ebv_130b/ebv_nach_130b.jsp"
EXPECTED_HEADER = ["wirkstoff", "handelsnamen", "pharmazeutischer_unternehmer", "status", "letzte_aktualisierung"]
ROWS_PER_PAGE_GUESS = 10


def _page_url(base: str, page_no: int) -> str:
    parts = urlparse(base)
    query = parse_qs(parts.query)
    query["pageNo"] = [str(page_no)]
    query.setdefault("submitted", ["true"])
    return urlunparse(parts._replace(query=urlencode(query, doseq=True), fragment=""))


def _table_rows(html: str, base_url: str) -> tuple[list[str], list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        header = [slug(c.get_text(" ", strip=True)) for c in trs[0].find_all(["th", "td"])]
        if "wirkstoff" not in header:
            continue
        rows = []
        for tr in trs[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) != len(header):
                continue
            row = {h: c.get_text(" ", strip=True) for h, c in zip(header, cells)}
            link = tr.find("a", href=True)
            row["detail_url"] = urljoin(base_url, re.sub(r";jsessionid=[^?]*", "", link["href"])) if link else None
            rows.append(row)
        return header, rows
    raise ParserError("AMNOG page: no table with a 'Wirkstoff' column found")


class DeAmnogAdapter(SourceAdapter):
    adapter_id = "de_amnog"
    parser_type = "html"
    parser_version = "1.0"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    limitations = "Negotiation status only: the negotiated reimbursement amounts are not published in the public table."

    def discover(self) -> SourceArtifact:
        first = self.ctx.http.get_text(PAGE)
        pages = [int(n) for n in re.findall(r"pageNo=(\d+)", first.text)]
        last = max(pages) if pages else 1
        if not pages and "Wirkstoff" not in first.text:
            raise DiscoveryError("AMNOG overview page has no table/pagination – layout changed?")
        files = [ArtifactFile(url=_page_url(PAGE, n), filename=f"amnog_page_{n:03d}.html", role=f"page_{n:03d}") for n in range(1, last + 1)]
        return SourceArtifact(self.source_id, files, published_at=None, edition=f"{last} pages", discovered_from=PAGE)

    def download(self, artifact: SourceArtifact) -> RawSnapshot:
        """Fetch pages sequentially with a delay; stop early when a --limit run has enough rows."""
        staging = self.ctx.store.staging_dir(self.source_id)
        downloaded = []
        delay = float(self.option("request_delay_seconds", 1.0))
        stop = self.hints.early_stop_at
        rows_seen = 0
        for f in artifact.files:
            result = self.ctx.http.download(f.url, staging / f.filename)
            downloaded.append(DownloadedFile(f, result.path, result.final_url, result.content_type, f.filename))
            rows_seen += ROWS_PER_PAGE_GUESS
            if stop is not None and rows_seen >= stop:
                artifact.partial = True
                artifact.notes.append(f"limited run: stopped after {len(downloaded)} of {len(artifact.files)} pages")
                break
            if delay:
                time.sleep(delay)
        return self.ctx.store.save(
            self.source_id, artifact, downloaded, parser_version=self.parser_version,
            previous_id=self.ctx.previous_snapshot_id, previous_hashes=self.ctx.previous_snapshot_hashes,
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("table", EXPECTED_HEADER)]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        records, rows, header = [], [], EXPECTED_HEADER
        for f in sorted(snapshot.files_with_role_prefix("page_"), key=lambda x: x.role):
            header, page_rows = _table_rows(f.path.read_text(encoding="utf-8", errors="replace"), PAGE)
            for i, row in enumerate(page_rows):
                key = hashlib.sha1(f"{row.get('wirkstoff')}|{row.get('handelsnamen')}".encode()).hexdigest()[:16]
                rows.append(row)
                records.append(ParsedRecord(row_ref=f"{f.filename}:{i + 1}", key=key, data=row, role="table"))
        profile = profile_rows("table", header, rows, row_count=len(rows))
        return ParsedDataset(self.source_id, snapshot, records, [profile], truncated=snapshot.partial, total_rows=len(rows))

    def record_matches_product_ids(self, record: ParsedRecord, product_ids: set[str]) -> bool:
        wanted = {p.lower() for p in product_ids}
        return record.key in product_ids or (record.data.get("wirkstoff") or "").lower() in wanted

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        updated = parse_date(d.get("letzte_aktualisierung"), ["%d.%m.%Y"]) if clean_str(d.get("letzte_aktualisierung")) else None
        product = SourceProductData(
            product_code=record.key or "",
            code_type="AMNOG_ENTRY",
            product_level=ProductLevel.REGULATORY_ENTRY,
            name=clean_str(d.get("handelsnamen")),
            brand_name=clean_str(d.get("handelsnamen")),
            ingredient=clean_str(d.get("wirkstoff")),
            manufacturer=clean_str(d.get("pharmazeutischer_unternehmer")),
            marketing_status=clean_str(d.get("status")),
            notes=f"letzte Aktualisierung {updated.isoformat()}" if updated else None,
            detail_url=d.get("detail_url"),
        )
        return NormalizedRecord(record.row_ref, product, [])
