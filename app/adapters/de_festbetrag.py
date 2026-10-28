"""Germany – Arzneimittel-Festbeträge (BfArM PDF, updated every 2 weeks by GKV-Spitzenverband).

PARTIALLY SUPPORTED:
  * Only medicines belonging to a Festbetrag group are listed (no full German price list exists publicly).
  * Data are extracted from a PDF (parser_type = pdf): layout changes are detected (header check + share of
    unparseable lines) and reported as SCHEMA_CHANGED.
  * Legal: GKV-SV states the lists are not provided in a processable format for usage-rights reasons; PZN-level
    prices originate from IFA. Internal use only pending legal review.
Columns per line: Wirkstoffmenge, Vergleichsgröße (w), Packungsgröße, Darreichungsform (IFA code), Preis (AVP),
Festbetrag, Differenz, Arzneimittelname, PZN.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from app.adapters._common import pack_price, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import AdapterStatus, PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError, SchemaChangedError
from app.core.parsing import html_links, parse_decimal, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, RawSnapshot, SourceArtifact, SourceProductData
from app.normalization.forms import compile_extra, normalize_form
from app.normalization.packs import FORM_TO_PACK_UNIT, Pack
from app.normalization.text import extract_brand
from app.observability import Progress

PAGE = "https://www.bfarm.de/DE/Arzneimittel/Arzneimittelinformationen/Festbetraege-und-Zuzahlungen/Festbetraege/_node.html"
PDF_RE = re.compile(r"festbetraege-(\d{8})\.pdf", re.IGNORECASE)
MONEY = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
NUM = r"\d+(?:\.\d{3})*(?:,\d+)?"
LINE_RE = re.compile(
    rf"^(?:(?P<wirkstoff>[^\d\s].*?)\s+)?(?P<menge>{NUM})\s+(?P<w>{NUM})\s+(?P<pack>{NUM})\s+(?P<form>[A-ZÄÖÜ]{{2,6}})\s+"
    rf"(?P<preis>{MONEY})\s*(?P<festbetrag>{MONEY})\s*(?P<diff>{MONEY})\s+(?P<name>.+?)\s+(?P<pzn>\d{{7,8}})$"
)
# '1 Abirateron, Gruppe 1', '1 Amiodaron', '1 5-Fluorouracil, Gruppe 1' (first token must contain a letter)
GROUP_RE = re.compile(r"^(?P<stufe>[123])\s+(?P<group>\S*[A-Za-zÄÖÜäöü]\S*.*)$")
STAND_RE = re.compile(r"Stand:\s*(\d{2}\.\d{2}\.\d{4})")
FOOTER_RE = re.compile(r"Seite\s+\d+\s+von\s+\d+")
COLUMNS = ["stufe", "festbetragsgruppe", "wirkstoff", "wirkstoffmenge", "vergleichsgroesse", "packungsgroesse", "darreichungsform", "preis", "festbetrag", "differenz", "arzneimittelname", "pzn"]
HEADER_WORDS = ("Festbetragsgruppe", "PZN", "Preis")
MAX_UNPARSED_SHARE = 0.2
# IFA dosage form codes (heuristic mapping of the most frequent codes; unknown codes stay unmapped)
IFA_FORMS = compile_extra(
    [
        ("prolonged-release tablet", [r"reta", r"rtab", r"ret"]),
        ("gastro-resistant tablet", [r"mtab", r"msr", r"tmr"]),
        ("effervescent tablet", [r"btab", r"bta"]),
        ("orodispersible tablet", [r"smta", r"smt", r"tabl schm"]),
        ("tablet", [r"tabl", r"ftbl", r"tab", r"fta", r"ftab", r"lutb", r"kata"]),
        ("capsule", [r"kaps", r"hkp", r"hkap", r"wkap", r"wkp", r"kap"]),
        ("powder for injection", [r"pifl", r"pilf", r"plij", r"trsk"]),
        ("injection", [r"ijlg", r"iflg", r"ifij", r"infl", r"amp", r"fsp", r"fert", r"pen", r"isu", r"ilo"]),
        ("eye drops", [r"augt", r"atr"]),
        ("nasal spray", [r"nasp", r"nspr"]),
        ("inhalation", [r"dosa", r"ihp", r"ilsg", r"ihlo", r"inhp"]),
        ("syrup", [r"saft", r"siru"]),
        ("oral suspension", [r"susp", r"tsus"]),
        ("oral solution", [r"lsg", r"loes", r"lse", r"tlsg"]),
        ("drops", [r"trop", r"tro"]),
        ("granules", [r"gran", r"grae"]),
        ("powder", [r"plv", r"pulv"]),
        ("cream", [r"crem", r"cre"]),
        ("ointment", [r"salb", r"sal"]),
        ("gel", [r"gel"]),
        ("patch", [r"pfl", r"tpf", r"tdp"]),
        ("suppository", [r"supp", r"zapf"]),
    ]
)


class DeFestbetragAdapter(SourceAdapter):
    adapter_id = "de_festbetrag"
    parser_type = "pdf"
    parser_version = "1.0"
    status = AdapterStatus.PARTIALLY_SUPPORTED
    limitations = (
        "Festbetrag-group medicines only (PDF extraction); no complete German pack-level list is public "
        "(IFA/Lauer-Taxe are commercial). Internal use only pending legal review."
    )

    def discover(self) -> SourceArtifact:
        page = self.ctx.http.get_text(PAGE)
        found = []
        for url, _ in html_links(page.text, PAGE):
            m = PDF_RE.search(url)
            if m:
                found.append((datetime.strptime(m.group(1), "%Y%m%d").date(), url))
        if not found:
            raise DiscoveryError("BfArM page: no 'festbetraege-YYYYMMDD.pdf' link found", details={"page": PAGE})
        stand, url = max(found)
        return SourceArtifact(
            self.source_id,
            [ArtifactFile(url=url, filename=f"festbetraege-{stand:%Y%m%d}.pdf", role="main", published_at=stand)],
            published_at=stand,
            edition=f"Stand {stand:%d.%m.%Y}",
            discovered_from=PAGE,
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", COLUMNS, expected_column_count=len(COLUMNS), typed_columns=["preis", "festbetrag", "pzn"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        import pdfplumber

        f = snapshot.file("main")
        stop = self.hints.early_stop_at
        records: list[ParsedRecord] = []
        rows: list[dict] = []
        unparsed: list[str] = []
        stufe, group = None, None
        stand = None
        truncated = False
        candidate_lines = 0
        try:
            pdf = pdfplumber.open(f.path)
        except Exception as exc:  # noqa: BLE001
            raise ParserError(f"Cannot open PDF {f.filename}: {exc}") from exc
        with pdf:
            first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
            missing = [w for w in HEADER_WORDS if w not in first_text]
            if missing:
                raise SchemaChangedError(f"Festbetrag PDF layout changed: header words {missing} not found on page 1")
            m = STAND_RE.search(first_text)
            if m:
                stand = datetime.strptime(m.group(1), "%d.%m.%Y").date()
            progress = Progress(f"read {f.filename}", total=len(pdf.pages), unit="pages", check_every=1)
            for page_no, page in enumerate(pdf.pages, start=1):
                progress.update(lines=len(records), unparsed=len(unparsed))
                lines = (page.extract_text() or "").splitlines()
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    i += 1
                    if not line or line.startswith(("Festbetragsarzneimittel", "Wirkstoff-", "Stufe Festbetragsgruppe")) or FOOTER_RE.search(line):
                        continue
                    g = GROUP_RE.match(line)
                    if g and not LINE_RE.match(line) and not re.search(r"\d{7,8}$", line):
                        stufe, group = int(g.group("stufe")), g.group("group").strip()
                        continue
                    candidate_lines += 1
                    if i < len(lines) and re.fullmatch(r"\d{1,7}", lines[i].strip()):
                        line = line + lines[i].strip()  # PZN wrapped onto the next line
                        i += 1
                    m = LINE_RE.match(line)
                    if not m:
                        unparsed.append(f"p{page_no}: {line[:120]}")
                        continue
                    row = {"stufe": stufe, "festbetragsgruppe": group, **{
                        "wirkstoff": (m.group("wirkstoff") or "").strip() or None,
                        "wirkstoffmenge": m.group("menge"),
                        "vergleichsgroesse": m.group("w"),
                        "packungsgroesse": m.group("pack"),
                        "darreichungsform": m.group("form"),
                        "preis": m.group("preis"),
                        "festbetrag": m.group("festbetrag"),
                        "differenz": m.group("diff"),
                        "arzneimittelname": m.group("name").strip(),
                        "pzn": m.group("pzn").zfill(8),
                    }}
                    rows.append(row)
                    records.append(ParsedRecord(row_ref=f"{f.filename}:p{page_no}", key=row["pzn"], data=row))
                    if stop is not None and len(records) >= stop:
                        truncated = True
                        break
                if truncated:
                    break
            progress.finish(lines=len(records), unparsed=len(unparsed))
        if candidate_lines and len(unparsed) / candidate_lines > MAX_UNPARSED_SHARE:
            raise SchemaChangedError(
                f"Festbetrag PDF layout changed: {len(unparsed)}/{candidate_lines} lines could not be parsed",
                details={"examples": unparsed[:10]},
            )
        issues = []
        if unparsed:
            issues.append(self.issue(ParserError.error_type, f"{len(unparsed)} PDF lines not recognised (kept out)", stage="parse", examples=unparsed[:20]))
        profile = profile_rows("main", COLUMNS, rows, row_count=len(rows))
        return ParsedDataset(
            self.source_id, snapshot, records, [profile], truncated=truncated, total_rows=len(records),
            row_errors=len(unparsed), issues=issues, context={"stand": stand},
        )

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        name = d["arzneimittelname"]
        group = d.get("festbetragsgruppe") or ""
        # Stufe 1 groups share one active ingredient (= group name); Stufe 2/3 lines carry their own 'Wirkstoff'.
        ingredient = d.get("wirkstoff") or (group.split(",")[0].strip() if d.get("stufe") == 1 else None)
        qty = parse_decimal(d["packungsgroesse"], decimal_sep=",")
        form = normalize_form(d["darreichungsform"], name, extra=IFA_FORMS)
        pack = Pack(qty, FORM_TO_PACK_UNIT.get(form or "") or "unit", d["packungsgroesse"]) if qty else None
        stand = dataset.context.get("stand") or dataset.snapshot.published_at
        product = SourceProductData(
            product_code=d["pzn"],
            code_type="PZN",
            product_level=ProductLevel.PRESENTATION,
            name=name,
            brand_name=extract_brand(name),
            ingredient=ingredient,
            strength_text=d["wirkstoffmenge"],
            dosage_form=d["darreichungsform"],
            pack_text=d["packungsgroesse"],
            group_name=group or None,
            reimbursement_category=f"Festbetragsgruppe Stufe {d['stufe']}" if d.get("stufe") else None,
            notes=f"Vergleichsgröße {d['vergleichsgroesse']}",
            **parse_fields(strength_text=name, pack=pack),
        )
        common = dict(currency="EUR", product=product, valid_from=stand, coverage_scope=self.config.coverage_scope)
        prices = [
            pack_price(parse_decimal(d["preis"], decimal_sep=","), price_type=PriceBasis.RETAIL, status=PriceStatus.OBSERVED,
                       vat_included=True, variant="apothekenverkaufspreis",
                       notes=f"Differenz zum Festbetrag {d['differenz']} €", **common),
            pack_price(parse_decimal(d["festbetrag"], decimal_sep=","), price_type=PriceBasis.REIMBURSEMENT, status=PriceStatus.CEILING,
                       vat_included=True, variant="festbetrag", **common),
        ]
        return NormalizedRecord(record.row_ref, product, [p for p in prices if p is not None and p.amount > Decimal(0)])
