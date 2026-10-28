"""Canada (Ontario only) – Ontario Drug Benefit Formulary / Comparative Drug Index XML data extract.

coverage_scope = province:ON – these prices must never be presented as Canada-wide.
Discovery uses the current Ontario publication page, not the stale CKAN download.
Published unit prices and ministry payments are separate observations; a listed
product is not necessarily an ODB benefit.
"""

from __future__ import annotations

import re
from datetime import datetime
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from app.adapters._common import join_notes, parse_fields
from app.core.adapter import SourceAdapter
from app.core.enums import PriceBasis, PriceStatus, ProductLevel
from app.core.errors import DiscoveryError, ParserError
from app.core.parsing import clean_str, html_links, parse_date, parse_decimal, profile_rows
from app.core.schema import ExpectedSchema
from app.core.types import ArtifactFile, NormalizedRecord, ParsedDataset, ParsedRecord, PriceFact, RawSnapshot, SourceArtifact, SourceProductData

PAGE = "https://www.ontario.ca/document/ontario-drug-benefit-odb-formulary-comparative-drug-index-cdi-and-monthly-formulary-0"
PRICE_TAGS = ("individualprice", "price", "drugbenefitprice", "unitprice")
CONTEXT_TAGS = {"genericname": "generic_name", "strength": "strength", "dosageform": "dosage_form", "name": "name"}
COLUMNS = ["din", "name", "generic_name", "strength", "dosage_form", "manufacturer", "price", "ministry_payment", "listing_date"]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class CaOnOdbAdapter(SourceAdapter):
    adapter_id = "ca_on_odb"
    parser_type = "xml"
    parser_version = "2.0"
    limitations = "Ontario only; published unit prices do not imply benefit eligibility."

    def discover(self) -> SourceArtifact:
        override = self.ctx.settings.ca_on_odb_xml_url
        if override:
            return SourceArtifact(self.source_id, [ArtifactFile(url=override, filename="odb_formulary.xml", role="main")], discovered_from="CA_ON_ODB_XML_URL")
        page_url = self.option("publication_page", PAGE)
        page = self.ctx.http.get_text(page_url)
        links = [(url, label) for url, label in html_links(page.text, page_url) if url.lower().endswith(".xml")]
        if len(links) != 1:
            raise DiscoveryError(f"Ontario publication page: expected one XML extract, found {len(links)}")
        url, _ = links[0]
        soup = BeautifulSoup(page.text, "html.parser")
        anchor = soup.find("a", href=lambda href: bool(href and href.lower().endswith(".xml")))
        text = anchor.parent.get_text(" ", strip=True) if anchor is not None else ""
        effective = re.search(r"Effective\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", text, re.IGNORECASE)
        if not effective:
            raise DiscoveryError("Ontario XML extract link has no effective date")
        published = datetime.strptime(effective.group(1), "%B %d, %Y").date()
        return SourceArtifact(
            self.source_id,
            [ArtifactFile(url=url, filename="odb_formulary.xml", role="main", published_at=published)],
            published_at=published,
            discovered_from=page_url,
            edition=f"Effective {published.isoformat()}",
        )

    def expected_schemas(self) -> list[ExpectedSchema]:
        return [ExpectedSchema("main", ["din", "name", "price"])]

    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        path = snapshot.file("main").path
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise ParserError(f"ODB extract is not well-formed XML: {exc}") from exc
        rows: list[dict] = []
        records: list[ParsedRecord] = []
        manufacturers = {
            elem.get("id"): clean_str(elem.text)
            for elem in root.iter()
            if _local(elem.tag) == "manufacturer" and elem.get("id")
        }

        def walk(elem: ET.Element, context: dict) -> None:
            ctx = dict(context)
            if _local(elem.tag) == "genericname":
                ctx["generic_name"] = next(
                    (clean_str(child.text) for child in elem if _local(child.tag) == "name"),
                    clean_str(elem.text),
                )
            for child in elem:
                tag = _local(child.tag)
                if tag in CONTEXT_TAGS and tag != "name" and (child.text or "").strip() and len(child) == 0:
                    ctx[CONTEXT_TAGS[tag]] = child.text.strip()
            for child in elem:
                tag = _local(child.tag)
                if tag == "drug" and child.get("id"):
                    fields = {_local(c.tag): (c.text or "").strip() for c in child}
                    price = next((fields[t] for t in PRICE_TAGS if fields.get(t)), None)
                    row = {
                        "din": child.get("id").strip(),
                        "name": fields.get("name"),
                        "generic_name": ctx.get("generic_name"),
                        "strength": fields.get("strength") or ctx.get("strength"),
                        "dosage_form": fields.get("dosageform") or ctx.get("dosage_form"),
                        "manufacturer": manufacturers.get(fields.get("manufacturerid")) or fields.get("manufacturerid") or fields.get("manufacturer"),
                        "price": price,
                        "ministry_payment": fields.get("amountmohltcpays"),
                        "listing_date": fields.get("listingdate"),
                        "attributes": {k: v for k, v in child.attrib.items() if k != "id"},
                    }
                    rows.append(row)
                    records.append(ParsedRecord(row_ref=f"drug[{row['din']}]", key=row["din"], data=row))
                elif len(child):
                    walk(child, ctx)

        walk(root, {})
        if not records:
            raise ParserError("ODB extract parsed but no <drug id='DIN'> element found – structure differs from expectations")
        current = root.get("effectiveDate") or root.get("date")
        effective_date = parse_date(current, ["%Y-%m-%d"]) if current else snapshot.published_at
        profile = profile_rows("main", COLUMNS, rows, row_count=len(rows))
        return ParsedDataset(self.source_id, snapshot, records, [profile], total_rows=len(records), context={"effective_date": effective_date})

    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        d = record.data
        listing = parse_date(d.get("listing_date"), ["%Y-%m-%d", "%Y%m%d"]) if clean_str(d.get("listing_date")) else None
        product = SourceProductData(
            product_code=d["din"],
            code_type="DIN",
            product_level=ProductLevel.PRODUCT,
            name=clean_str(d.get("name")),
            brand_name=clean_str(d.get("name")),
            ingredient=clean_str(d.get("generic_name")),
            strength_text=clean_str(d.get("strength")),
            dosage_form=clean_str(d.get("dosage_form")),
            manufacturer=clean_str(d.get("manufacturer")),
            reimbursement_category="Not an ODB benefit" if (d.get("attributes") or {}).get("notABenefit") == "Y" else "Ontario Drug Benefit listing",
            marketing_start_date=listing,
            notes=join_notes(*(f"{k}: {v}" for k, v in (d.get("attributes") or {}).items() if v)),
            **parse_fields(strength_text=d.get("strength")),
        )
        prices = []
        for column, basis, variant, label in [
            ("price", PriceBasis.EX_FACTORY, "individual_price", "Published individualPrice (Drug Benefit Price: manufacturer list price to pharmacies) per official unit; not proof of reimbursement eligibility"),
            ("ministry_payment", PriceBasis.REIMBURSEMENT, "amount_moh_pays", "amountMOHLTCPays per official unit"),
        ]:
            if not clean_str(d.get(column)):
                continue
            amount = parse_decimal(d[column], decimal_sep=".")
            if amount is None or amount <= 0:
                raise ParserError(f"ODB {d['din']}: invalid {column} {d[column]!r}")
            prices.append(
                PriceFact(
                    amount=amount,
                    currency="CAD",
                    price_type=basis,
                    price_variant=variant,
                    price_status=PriceStatus.REGULATED,
                    price_unit="unit",
                    unit_text="Official unit (tablet/capsule/mL/g)",
                    vat_included=False,
                    valid_from=dataset.context.get("effective_date"),
                    date_basis="edition_date",
                    coverage_scope=self.config.coverage_scope,
                    notes=label,
                )
            )
        return NormalizedRecord(record.row_ref, product, prices)
