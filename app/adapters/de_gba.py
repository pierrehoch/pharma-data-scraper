"""Historical G-BA pack-price disclosures, not a German marketed-pack price list.

The default traverses the entire historical procedure catalogue. An explicit
``options.procedure_urls`` list restricts discovery for targeted imports.
German original TrG PDFs are preferred over translations of the same decision.
Rows describe substances/presentations, often cheapest alternatives, not PZNs or
necessarily a unique brand. Reference dates are NOT current-price validity claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from statistics import median
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, ErrorType, PriceBasis, PriceStatus, ProductLevel, Severity
from app.core.errors import DiscoveryError, DownloadError, ParserError, SchemaChangedError
from app.core.parsing import profile_rows
from app.core.schema import ExpectedSchema
from app.core.snapshots import DownloadedFile
from app.core.types import (
    ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, PriceFact,
    RawSnapshot, SnapshotFile, SourceArtifact, SourceProductData,
)
from app.observability import Progress, get_logger

PAGE = "https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/"
PROCEDURE_RE = re.compile(r"/bewertungsverfahren/nutzenbewertung/\d+/")
DOCUMENT_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})_.*_TrG(?P<english>_EN)?\.pdf$", re.I)
COLUMNS = ["document_url", "decision_date", "reference_date", "ingredient", "strength", "pack", "prices"]
MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
}
DATE_RE = r"(\d{1,2})\.?\s+([A-Za-zäÄ]+)\s+(\d{4})"
MONEY_RE = re.compile(r"(?<![\d.,])(?:(?:\d{1,3}(?:[ .]\d{3})*,\d{2}|\d+,\d{2})(?:\s*€)?|(?:\d{1,3}(?:[ .]\d{3})+|\d+)\s*€)")
EN_NUMBER = r"(?:\d{1,3}(?:[ ,]\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)(?![\d.,])"
EN_MONEY_RE = re.compile(rf"(?:€\s*{EN_NUMBER}|(?<![\d.,]){EN_NUMBER}\s*€)")
STRENGTH_RE = re.compile(r"\d+(?:[ .,]\d+)*\s*(?:mg|µg|μg|ug|mcg|g|ml|IU|I\.E\.)(?:/\w+)?\b", re.I)
log = get_logger(__name__)


def _official(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc == "www.g-ba.de"


def _english(filename: str) -> bool:
    return bool(re.search(r"(?:_EN_TrG|_TrG_EN)\.pdf$", filename, re.I))


def _decision_date(first_page: str, filename_date: date) -> date:
    header = re.split(r"\bInhalt\b|\bContents\b", first_page, maxsplit=1)[0]
    dates = {
        date(int(y), MONTHS[m.lower()], int(d))
        for d, m, y in re.findall(rf"\b(?:Vom|of)\s+{DATE_RE}", header, re.I)
        if m.lower() in MONTHS
    }
    if len(dates) > 1:
        raise SchemaChangedError("G-BA PDF: conflicting explicit decision dates")
    decision = dates.pop() if dates else filename_date
    if decision > date.today():
        raise SchemaChangedError(f"G-BA PDF: future decision date {decision}; filename is not reliable publication evidence")
    if decision != filename_date:
        log.warning("G-BA filename date %s corrected to explicit PDF decision date %s", filename_date, decision)
    return decision


def _reference_date(text: str) -> date:
    patterns = [
        rf"Stand\s+Lauer-Taxe:\s*{DATE_RE}",
        rf"Lauer-Taxe\s*\(Stand:\s*{DATE_RE}",
        rf"Lauer-Taxe[^;\n]{{0,60}}?(?:last revised|as of|last updated):?\s*{DATE_RE}",
    ]
    for pattern in patterns:
        found = {
            date(int(y), MONTHS[m.lower()], int(d))
            for d, m, y in re.findall(pattern, text, re.I) if m.lower() in MONTHS
        }
        if len(found) > 1:
            raise SchemaChangedError("G-BA PDF: conflicting Lauer-Taxe reference dates")
        if found:
            return found.pop()
    raise SchemaChangedError("G-BA PDF: no explicit Lauer-Taxe price reference date")


def _join(parts: list[str]) -> str:
    return re.sub(r"-\s+", "", " ".join(parts)).strip()


def _pack_rows(pdf, *, english: bool = False) -> list[dict]:
    """Use table header coordinates; treatment/annual-cost tables never qualify."""
    rows = []
    money_re = EN_MONEY_RE if english else MONEY_RE
    pack_x = None
    mode = None
    designation_prefix = []
    for page_no, original in enumerate(pdf.pages, 1):
        if mode is None and not re.search(r"Packungs|Packaging|Package", original.extract_text() or ""):
            continue
        sizes = [c["size"] for c in original.chars if c.get("text", "").strip()]
        if not sizes:
            continue
        # Superscript footnote 3 must not become the leading digit of €227,500.
        body_size = median(sizes)
        page = original.filter(lambda obj: obj.get("object_type") != "char" or obj["size"] >= body_size * .8)
        lines = page.extract_text_lines()
        words = page.extract_words()
        pending = None
        designation_prefix = []
        for index, line in enumerate(lines):
            text = line["text"]
            line_words = [w for w in words if line["top"] <= (w["top"] + w["bottom"]) / 2 <= line["bottom"]]
            if re.search(r"Bezeichnung|Designation", text) and re.search(r"Packungs|Packaging|Package", text):
                header = " ".join(l["text"] for l in lines[index:index + 6]).lower()
                pack_words = [w for w in line_words if re.match(r"Packungs|Packaging|Package", w["text"])]
                pack_x = pack_words[0]["x0"] - 4 if pack_words else None
                if "klinik" in header or "clinic" in header:
                    mode = "clinic"
                elif ("rabatt" in header or "rebate" in header) and ("apothe" in header or "pharmacy" in header):
                    mode = "retail"
                else:
                    mode = None
                pending = None
                continue
            if re.match(r"Abkürzungen|Abbreviations|Stand Lauer|Kosten für|Costs for|\d+\.\d+\s", text):
                mode, pending = None, None
                designation_prefix = []
            if mode is None or pack_x is None:
                continue
            left = " ".join(w["text"] for w in line_words if w["x0"] < pack_x)
            right = " ".join(w["text"] for w in line_words if w["x0"] >= pack_x)
            amounts = list(money_re.finditer(right))
            if amounts:
                if not left and designation_prefix:
                    left = _join(designation_prefix)
                designation_prefix = []
                pack = right[:amounts[0].start()].strip()
                if not re.match(r"^\d+\s+\S+", pack):
                    raise SchemaChangedError(f"G-BA p{page_no}: price row has no explicit pack: {text}")
                expected = 4 if mode == "retail" else 3
                if len(amounts) != expected or not left:
                    raise SchemaChangedError(f"G-BA p{page_no}: expected {expected} pack-price columns: {text}")
                values = []
                for match in amounts:
                    number = match.group().replace("€", "").replace(" ", "")
                    number = number.replace(",", "") if english else number.replace(".", "").replace(",", ".")
                    values.append(Decimal(number))
                if values[0] <= 0 or values[-1] <= 0:
                    raise SchemaChangedError(f"G-BA p{page_no}: non-positive pack cost")
                if mode == "retail" and abs(values[0] - values[1] - values[2] - values[3]) > Decimal(".02"):
                    raise SchemaChangedError(f"G-BA p{page_no}: statutory-rebate columns do not reconcile")
                if mode == "clinic" and abs(values[0] + values[1] - values[2]) > Decimal(".02"):
                    raise SchemaChangedError(f"G-BA p{page_no}: clinic purchase cost plus VAT does not reconcile")
                pending = {
                    "designation_parts": [left], "pack_parts": [pack], "mode": mode,
                    "prices": values, "page": page_no, "raw_row": text,
                    "price_x": [w for w in line_words if w["x0"] >= pack_x][len(pack.split())]["x0"],
                }
                rows.append(pending)
            elif left:
                following = lines[index + 1] if index + 1 < len(lines) else None
                next_words = [
                    w for w in words if following and following["top"] <= (w["top"] + w["bottom"]) / 2 <= following["bottom"]
                ]
                price_follows = bool(next_words) and all(w["x0"] >= pack_x for w in next_words) and bool(
                    money_re.search(" ".join(w["text"] for w in next_words))
                )
                if pending and not price_follows and (
                    STRENGTH_RE.search(left) or pending["designation_parts"][-1].endswith((" I", " /", "-"))
                ):
                    pending["designation_parts"].append(left)
                else:
                    designation_prefix = [left]
                    pending = None
            elif pending and not left and right and (
                pending["pack_parts"][-1].endswith("-") or (
                    pending["mode"] == "clinic" and all(w["x0"] < pending["price_x"] for w in line_words)
                )
            ):
                pending["pack_parts"].append(right)
            else:
                pending = None
    for row in rows:
        row["designation"] = _join(row.pop("designation_parts"))
        row["pack"] = _join(row.pop("pack_parts"))
        row["strength"] = " / ".join(STRENGTH_RE.findall(row["designation"])) or None
        ingredient = STRENGTH_RE.sub("", row["designation"])
        row["ingredient"] = re.sub(r"\s+", " ", ingredient).strip(" /")
    return rows


class DeGbaAdapter(SourceAdapter):
    adapter_id = "de_gba"
    parser_type = "pdf"
    parser_version = "2.1"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    limitations = (
        "Full historical G-BA catalogue discovery; pack-price extraction is layout-dependent, not a current German price list "
        "or confidential AMNOG reimbursement amounts. No PZN or unique brand association; "
        "annual treatment costs and therapy totals are excluded. Unavailable/unparseable documents are reported explicitly."
    )

    def _catalogue_procedures(self) -> list[str]:
        first = BeautifulSoup(self.ctx.http.get_text(PAGE).text, "html.parser")
        counter = first.select_one(".gba-entity-list__results-counter")
        count = re.fullmatch(r"\s*([\d.]+)\s+Ergebnis(?:se)?\s*", counter.get_text() if counter else "")
        if not count:
            raise DiscoveryError("G-BA catalogue: missing result count; refusing incomplete discovery")
        expected = int(count[1].replace(".", ""))
        last = 1
        for anchor in first.select("a[href]"):
            url = urlparse(urljoin(PAGE, anchor["href"]))
            query = parse_qs(url.query)
            if _official(url.geturl()) and url.path == urlparse(PAGE).path and query.get("sort") == ["beginn"] and query.get("direction") == ["desc"]:
                number = query.get("seite", [""])[0]
                if number.isdigit():
                    last = max(last, int(number))
        procedures: dict[str, None] = {}
        progress = Progress("G-BA catalogue", total=last, unit="pages", check_every=1)
        for page_no in range(1, last + 1):
            soup = first if page_no == 1 else BeautifulSoup(
                self.ctx.http.get_text(f"{PAGE}?sort=beginn&direction=desc&seite={page_no}").text, "html.parser",
            )
            table = soup.select_one("table#liste")
            rows = table.select("tbody tr") if table else []
            if not rows:
                raise DiscoveryError(f"G-BA catalogue page {page_no}: no procedure rows")
            for row in rows:
                links = {
                    urljoin(PAGE, anchor["href"]) for anchor in row.select("a[href]")
                    if _official(urljoin(PAGE, anchor["href"])) and PROCEDURE_RE.fullmatch(urlparse(urljoin(PAGE, anchor["href"])).path)
                }
                if len(links) != 1:
                    raise DiscoveryError(f"G-BA catalogue page {page_no}: ambiguous/missing procedure link")
                procedure = links.pop()
                if procedure in procedures:
                    raise DiscoveryError(f"G-BA catalogue repeats {procedure}; pagination changed during discovery")
                procedures[procedure] = None
            progress.update(procedures=len(procedures))
        progress.finish(procedures=len(procedures))
        if len(procedures) != expected:
            raise DiscoveryError(f"G-BA catalogue: expected {expected} procedures, discovered {len(procedures)}; retry after catalogue changes")
        return list(procedures)

    def discover(self) -> SourceArtifact:
        urls = self.option("procedure_urls")
        targeted = urls is not None
        if targeted and (not isinstance(urls, list) or not urls):
            raise DiscoveryError("G-BA procedure_urls must be a non-empty list, or omitted for the full catalogue")
        if urls is None:
            urls = self._catalogue_procedures()
        documents = {}
        self._discovery_report = {"scope": "targeted" if targeted else "full_catalogue", "procedures": [], "failures": []}
        progress = Progress("G-BA procedures", total=len(urls), unit="procedures", check_every=1)
        for procedure in urls:
            if not isinstance(procedure, str) or not _official(procedure) or not PROCEDURE_RE.fullmatch(urlparse(procedure).path):
                raise DiscoveryError(f"G-BA: invalid official procedure URL {procedure!r}")
            try:
                response = self.ctx.http.get_text(procedure)
            except DownloadError as exc:
                if targeted:
                    raise
                log.warning("G-BA procedure discovery failed: %s: %s", procedure, exc)
                self._discovery_report["failures"].append({"stage": "discovery", "url": procedure, "message": str(exc)})
                progress.update(documents=len(documents))
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            selected = {}
            for a in soup.select("a[href]"):
                url = urljoin(procedure, a["href"])
                filename = unquote(PurePosixPath(urlparse(url).path).name)
                match = DOCUMENT_RE.fullmatch(filename)
                if not _official(url) or not urlparse(url).path.startswith("/downloads/") or not match:
                    continue
                try:
                    decision_date = date.fromisoformat(match["date"])
                except ValueError:
                    message = f"Unusable decision date in official justification filename: {filename}"
                    log.warning("G-BA %s", message)
                    self._discovery_report["failures"].append({"stage": "discovery", "url": url, "message": message})
                    continue
                # Translations can translate the ingredient name in the filename.
                document_id = re.fullmatch(r"/downloads/\d+-\d+-(\d+)/[^/]+", urlparse(url).path)
                if not document_id:
                    raise DiscoveryError(f"G-BA unrecognised justification document path: {url}")
                identity = (document_id[1], match["date"])
                if identity not in selected or not _english(filename):
                    selected[identity] = ArtifactFile(
                        url, filename, role="document", published_at=decision_date,
                    )
            if not selected and targeted:
                raise DiscoveryError(f"G-BA procedure has no official TrG justification PDFs: {procedure}")
            self._discovery_report["procedures"].append({"url": procedure, "documents": [f.url for f in selected.values()]})
            documents.update(selected)
            progress.update(documents=len(documents))
        progress.finish(documents=len(documents))
        files = sorted(documents.values(), key=lambda f: f.url)
        if not files:
            raise DiscoveryError("G-BA: no justification documents discovered")
        no_documents = sum(not p["documents"] for p in self._discovery_report["procedures"])
        return SourceArtifact(
            self.source_id, files, discovered_from=PAGE,
            edition=f"{len(urls)} procedures / {len(files)} historical justifications",
            partial=targeted,
            notes=[
                f"{self._discovery_report['scope']}: {len(urls)} procedures, {len(files)} distinct original-preferred PDFs; "
                f"{no_documents} procedures without a justification, {len(self._discovery_report['failures'])} discovery failures.",
                "Historical reference disclosures, not current market coverage. Generated catalogue_manifest.json preserves discovery and download outcomes.",
            ],
        )

    def download(self, artifact: SourceArtifact) -> RawSnapshot:
        staging = self.ctx.store.staging_dir(self.source_id)
        downloaded = []
        report = getattr(self, "_discovery_report", {"scope": "explicit_artifact", "procedures": [], "failures": []})
        progress = Progress("G-BA justifications", total=len(artifact.files), unit="documents", check_every=1)
        for index, file in enumerate(artifact.files):
            try:
                result = self.ctx.http.download(file.url, staging / f"{index:04d}_{file.filename}")
            except DownloadError as exc:
                log.warning("G-BA document download failed: %s: %s", file.url, exc)
                report["failures"].append({"stage": "download", "url": file.url, "message": str(exc)})
            else:
                downloaded.append(DownloadedFile(file, result.path, result.final_url, result.content_type, file.filename))
            progress.update(downloaded=len(downloaded), failed=len(report["failures"]))
        progress.finish(downloaded=len(downloaded), failed=len(report["failures"]))
        manifest = staging / "catalogue_manifest.json"
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        downloaded.append(DownloadedFile(
            ArtifactFile(PAGE, manifest.name, role="manifest"), manifest, PAGE, "application/json", manifest.name,
        ))
        return self.ctx.store.save(
            self.source_id, artifact, downloaded, parser_version=self.parser_version,
            previous_id=self.ctx.previous_snapshot_id, previous_hashes=self.ctx.previous_snapshot_hashes,
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("pack_prices", COLUMNS)]

    def _document_records(self, file: SnapshotFile) -> dict[str, ParsedRecord]:
        import pdfplumber

        match = DOCUMENT_RE.fullmatch(file.filename)
        if not match:
            raise ParserError(f"G-BA: unrecognised dated justification filename {file.filename}")
        decision_date = date.fromisoformat(match["date"])
        with pdfplumber.open(file.path) as pdf:
            if not pdf.pages:
                raise ParserError(f"G-BA PDF has no pages: {file.filename}")
            decision_date = _decision_date(pdf.pages[0].extract_text() or "", decision_date)
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            reference_date = _reference_date(text)
            rows = _pack_rows(pdf, english=_english(file.filename))
        if not rows:
            raise SchemaChangedError("No supported explicit pack-price table found (may be a procedural/non-price justification)")
        records = {}
        for row in rows:
            row.update(document_url=file.source_url, decision_date=decision_date, reference_date=reference_date)
            identity = "|".join([
                self.source_id, urlparse(file.source_url).path,
                row["ingredient"], row["strength"] or "", row["pack"],
            ])
            key = hashlib.sha256(identity.encode()).hexdigest()[:32]
            row_ref = f"{file.filename}:p{row['page']}:{key}"
            if key in records and records[key].data["prices"] != row["prices"]:
                raise SchemaChangedError(f"G-BA conflicting disclosures for {row_ref}")
            records.setdefault(key, ParsedRecord(row_ref, key, row, role="pack_prices"))
        return records

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        from pdfminer.pdfparser import PDFSyntaxError
        from pdfplumber.utils.exceptions import PdfminerException

        records = {}
        issues = []
        manifest = next((f for f in snapshot.files if f.role == "manifest"), None)
        discovery = json.loads(manifest.path.read_text(encoding="utf-8")) if manifest else {"failures": []}
        for failure in discovery["failures"]:
            error_type = ErrorType.DISCOVERY_ERROR if failure["stage"] == "discovery" else ErrorType.DOWNLOAD_ERROR
            issues.append(self.issue(error_type, f"G-BA {failure['stage']} failed for {failure['url']}: {failure['message']}",
                                     stage="parse", url=failure["url"]))
        files = [f for f in snapshot.files if f.role == "document"]
        progress = Progress("G-BA price extraction", total=len(files), unit="documents", check_every=1)
        parsed_documents = 0
        failed_documents = []
        for f in files:
            try:
                document_records = self._document_records(f)
            except (SchemaChangedError, ParserError, PDFSyntaxError, PdfminerException, ValueError, OSError) as exc:
                message = f"G-BA excluded {f.filename}: {exc}"
                log.warning("%s", message)
                issues.append(self.issue(ErrorType.PARSER_ERROR, message, stage="parse", row_ref=f.filename, url=f.source_url))
                failed_documents.append({"filename": f.filename, "url": f.source_url, "reason": str(exc)})
                progress.update(parsed=parsed_documents, excluded=len(failed_documents), records=len(records))
                continue
            records.update(document_records)
            parsed_documents += 1
            progress.update(parsed=parsed_documents, excluded=len(failed_documents), records=len(records))
        progress.finish(parsed=parsed_documents, excluded=len(failed_documents), records=len(records))
        if not records:
            raise ParserError("G-BA snapshot contains no justification pack-price records")
        issues.append(self.issue(
            ErrorType.UNSUPPORTED_SOURCE,
            f"G-BA document coverage: {parsed_documents}/{len(files)} downloaded justifications parsed; "
            f"{len(failed_documents)} excluded, {len(discovery['failures'])} discovery/download failures. "
            "Full catalogue discovery is not complete PDF price extraction.",
            severity=Severity.INFO, stage="parse", excluded_documents=failed_documents,
        ))
        rows = [r.data for r in records.values()]
        return ParsedDataset(
            self.source_id, snapshot, list(records.values()),
            [profile_rows("pack_prices", COLUMNS, rows, row_count=len(rows))],
            total_rows=len(rows), truncated=snapshot.partial, issues=issues,
            context={"documents_parsed": parsed_documents, "documents_excluded": failed_documents},
        )

    def record_matches_product_ids(self, record: ParsedRecord, product_ids: set[str]) -> bool:
        return record.key in product_ids or record.data["ingredient"].casefold() in {s.casefold() for s in product_ids}

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord:
        d = record.data
        quantity, unit = d["pack"].split(maxsplit=1)
        notes = (
            f"Historical G-BA disclosure; decision {d['decision_date']}; Lauer-Taxe price reference "
            f"{d['reference_date']} (not evidence of validity from that day or of a current price). "
            "Substance-level presentation; brand/manufacturer/PZN not established, may represent a "
            "cheapest alternative or Festbetrag. No confidential negotiated price inferred. "
            f"Source p{d['page']}: {d['document_url']}"
        )
        product = SourceProductData(
            product_code=record.key or "", code_type="GBA_DISCLOSURE",
            product_level=ProductLevel.REGULATORY_ENTRY,
            name=d["designation"], ingredient=d["ingredient"], strength_text=d["strength"],
            pack_text=d["pack"], pack_quantity=Decimal(quantity), pack_unit=unit,
            detail_url=d["document_url"], notes=notes,
        )
        common = dict(
            currency="EUR", price_status=PriceStatus.REFERENCE, price_unit="pack",
            pack_quantity=Decimal(quantity), pack_unit=unit, unit_text=d["pack"],
            valid_from=d["reference_date"], date_basis="reference_date",
            published_at=d["decision_date"], coverage_scope="national", notes=notes,
        )
        if d["mode"] == "retail":
            prices = [
                PriceFact(
                    amount=d["prices"][0], price_type=PriceBasis.RETAIL,
                    price_variant="apothekenverkaufspreis", statutory_reduction_included=False, **common,
                ),
                PriceFact(
                    amount=d["prices"][3], price_type=PriceBasis.RETAIL,
                    price_variant="net_after_statutory_rebates", statutory_reduction_included=True,
                    **{**common, "notes": notes + f"; §130 rebate {d['prices'][1]} EUR; §130a rebate {d['prices'][2]} EUR"},
                ),
            ]
        else:
            prices = [PriceFact(
                amount=d["prices"][2], price_type=PriceBasis.WHOLESALE,
                price_variant="clinic_acquisition_including_vat", vat_included=True,
                **{**common, "notes": notes + f"; clinic purchase cost before VAT {d['prices'][0]} EUR; disclosed VAT {d['prices'][1]} EUR; final cost {d['prices'][2]} EUR"},
            )]
        return NormalizedRecord(record.row_ref, product, prices)
