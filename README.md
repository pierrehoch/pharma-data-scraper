# Pharma data scraper – drug price collection platform

Collects and historises public drug prices and product coverage from national and provincial sources. Each source fills a set of
generic product and price fields (left empty when the source does not publish them); the raw files, their
provenance and the full price history are kept.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions.

## Quick start (local, SQLite)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # then set DATABASE_URL=sqlite:///storage/local.db (or a PostgreSQL URL)
python -m app db upgrade        # Alembic migrations
python -m app sources           # registry + adapter status
python -m app ingest --all --limit 100
python -m app serve             # http://127.0.0.1:8000/dashboard  –  API docs: /docs
```

## Docker (PostgreSQL)

```bash
docker compose up -d postgres backend                       # migrations run at start-up
docker compose run --rm backend python -m app ingest --all --limit 100
open http://localhost:8000/dashboard
```

The dashboard is server-rendered by the backend (no separate frontend container).

## Ingestion commands

| Command | Effect |
|---|---|
| `python -m app ingest --all --limit 100` | every enabled source, first 100 records each |
| `python -m app ingest --source fr_bdpm --limit 10` | one source (repeat `--source` for several) |
| `python -m app ingest --country FR --limit 50` | all sources of a country (`UK` → `GB` accepted) |
| `python -m app ingest --source fr_bdpm --product-id 3400949497294` | one national identifier (repeatable) |
| `python -m app ingest --source fr_bdpm --dry-run --limit 10` | download/parse/validate/publish, then roll back the database |
| `python -m app ingest --source fr_bdpm --resume` | resume the last interrupted job from its snapshot + checkpoint |
| `--force-download` | download even if the same edition is already stored |
| `--accept-schema-change` | publish after a human reviewed a detected schema change |
| `python -m app fx update [--history]` | ECB exchange rates (stored separately, never applied to original prices) |
| `python -m app coverage` | Dupixent/dupilumab product and positive-price counts for every registered source |
| `python -m app coverage --query dupixent --query dupilumab --json` | OR-match terms; exact amounts, units, dates and source limitations |

`--limit` is applied after parsing and before normalisation/publication; streaming parsers
(PDF, large XLSX, NADAC API) receive it as a hint and stop early (dataset flagged `truncated`).

Exit code: 0 if every job ended `success`, `partial_success` or `unsupported`; 1 otherwise.

Logging: every stage logs its start/end with duration and metrics, and long steps (downloads, file reading,
PDF pages, normalisation, publication) log progress with rate and ETA every 5 s.
Global options go before the command: `python -m app -v --progress-interval 2 ingest --all`
(`-v` = debug, `--log-level`, `--log-format auto|text|json`). Live progress is also visible on the job page
of the dashboard (updated at each committed batch).

## Data model

* `product` – one row per item of a source (pack, product, billing code or regulatory entry), unique on
  `(source_id, product_code)`. Generic fields: identity (`product_code`, `code_type`, `product_level`, `country`),
  description (`name`, `brand_name`, `ingredient`, `strength_text/value/unit`, `dosage_form`, `route`,
  `pack_text/quantity/unit`, `manufacturer`, `atc_code`, `therapeutic_class`, `product_type`), related codes
  (`group_code`, `group_name`, `authorisation_number`, `ean`), status (`legal_status`, `authorisation_status`,
  `marketing_status`, `authorisation_date`, `marketing_start_date`, `marketing_end_date`), reimbursement
  (`reimbursement_rate`, `reimbursement_category`, `reimbursement_conditions`, `restrictions`, `orphan`),
  `notes`, `detail_url`. Text fields keep the source wording; only strength and pack size are parsed.
* `price` – append-only history: `price_type` (supply-chain level, see below), `price_variant`,
  `price_status`, `amount`, `currency`, `price_unit`/`unit_text`, `unit_price`, `vat_included`,
  `tax_rate`, `dispensing_fee`, `statutory_reduction_included`, `valid_from`/`valid_to`, `date_basis`,
  `published_at`, `coverage_scope`, `notes`, provenance (snapshot, row) and `is_current`.

### Price types

`price_type` is the level of the supply chain the price applies to, so that prices of different sources
can be compared. Maxima/caps are expressed by `price_status=ceiling`, the source's own concept by
`price_variant`, and taxes, fees and statutory rebates by `vat_included`, `tax_rate`, `dispensing_fee` and
`statutory_reduction_included`.

| `price_type` | Meaning | Sources (`price_variant`) |
|---|---|---|
| `ex_factory` | manufacturer selling price (ex-manufacturer) | `br_cmed` PF (`pf_*`), `it_aifa` Class H ex-factory, `es_cipm` proposed industrial price (PVL), `ca_on_odb` Drug Benefit Price (`individual_price`) |
| `wholesale` | purchase price of the pharmacy/hospital | `us_nadac` (`nadac`), `uk_dmd` AMPP (`nhs_indicative_price`), `de_gba` (`clinic_acquisition_including_vat`) |
| `retail` | pharmacy price to the public | `fr_bdpm` (`incl_/excl_dispensing_fee`), `it_aifa`, `es_nomenclator` (PVP IVA), `de_festbetrag` / `de_gba` (`apothekenverkaufspreis`, `net_after_statutory_rebates`), `br_cmed` PMC (`pmc_*`, ceiling) |
| `reimbursement` | amount paid or capped by a payer | `jp_nhi`, `uk_drug_tariff` / `uk_dmd` VMPP (`drug_tariff_basic_price`), `us_asp` (`asp_payment_limit`), `ca_on_odb` (`amount_moh_pays`), `ca_bc_pharmacare` (ceiling), `de_festbetrag` (`festbetrag`, ceiling), `es_nomenclator` (`precio_de_referencia`, `menor_precio_agrupacion_homogenea`, ceiling) |
| `public_procurement` | sales to public buyers / tenders | `cn_vbp`, `cn_nhsa`, `cn_gdmede` / eligible `cn_hubei` provincial listings (`guawang_pack_listing`) and awards (`awarded_pack_price`), `br_cmed` PMVG (`pmvg_*`), `it_aifa` (`prezzo_massimo_di_cessione`, ceiling) |
| `other` | none of the above | – |

Migration `0002` rewrites legacy values (`retail_public`, `acquisition_cost`, `list_price`,
`reimbursement_ceiling`, `maximum_consumer_price`, `maximum_government_price`, `medicare_payment`,
`tariff_reimbursement`, `procurement_price`) and recomputes series keys so histories continue.

## Multilingual ingredient search

Products keep their national identifiers and source ingredient wording. An additional **many-to-many
ingredient index** links them to stable application-owned ingredient IDs. A combination can therefore
be found through any verified active constituent, across brands, strengths, forms and countries.
This finds candidates; it does **not** establish clinical interchangeability or comparable price bases.

Use the dashboard's **Ingredient** control to enter a name or translated alias. A unique complete alias
selects the canonical ingredient; ambiguous or incomplete input presents choices. Ordinary **Search**
remains literal free-text search. Ingredient selection is retained in pagination and CSV downloads.

```text
GET /ingredients?q=paracetamol
GET /ingredients?q=アセトアミノフェン
GET /prices?ingredient_id=<id-returned-by-ingredients>
GET /prices.csv?ingredient_id=<same-id>
GET /ingredients/coverage
```

`ingredient_id` searches the whole documented ingredient family, even when a salt/hydrate is selected.
Only explicit parent relationships are followed; no salt names are inferred or stripped. Exact
source substances, evidence, mapping methods and resolution statuses remain visible in results and
product details. Existing country/source/price-type filters and history behavior still apply.
API results remain paginated; CSV exports all matching observations. Membership filtering does not
duplicate price rows when more than one constituent points to the selected family.

### Installing and maintaining dictionaries

Searches and normal ingestion use **only the installed local dictionary**, without remote lookups.
Dictionary updates are explicit, validated and transactionally installed; failed requests/imports do
not replace the last installed version. Raw downloads and validated imports are content-addressed
under `storage/ingredient_dictionary/` (next to the configured raw-storage directory).

```bash
python -m app db upgrade
python -m app ingredients sync --provider rxnorm
python -m app ingredients import data/ingredients/bootstrap.json
python -m app ingredients sync --provider wikidata --entity Q57055 --entity Q5315925
python -m app ingredients sync --provider wikidata --discover 200
python -m app ingredients backfill
python -m app ingredients report
```

RxNorm synchronization imports the active ingredient (`IN`) and precise ingredient (`PIN`) vocabulary.
For precise names present in the source corpus, it also retrieves documented parent-ingredient
relationships. Wikidata synchronization supplies multilingual labels/aliases and validates substance
identity using installed ingredient RxCUIs or UNIIs. Discovery is explicitly bounded (maximum 10,000
entities per invocation); unrelated/ambiguous entities are reported, never guessed. Individual entity
updates retain previously imported Wikidata entities. Network restrictions/rate limits may prevent a
refresh: use a reviewed JSON snapshot instead and inspect the coverage report.

The bundled bootstrap is a **small, reviewed starting set**, not a comprehensive multilingual
dictionary: acetaminophen/paracetamol, caffeine, morphine, ibuprofen, amoxicillin, omeprazole and
dupilumab. It contains dictionary-provided labels in English, French, German, Spanish, Italian,
Portuguese, Japanese and Chinese. RxNorm adds substantially more identities but is not a worldwide
translation dictionary. Completeness depends on installed aliases and source evidence.

Backfill is idempotent and commits in chunks; it changes ingredient annotations only, not products'
source wording or price history. Re-run it after installing new dictionaries or reviewed mappings.
New ingestion resolves locally during publication and replaces stale automatic links when ingredient
evidence changes.

### Mapping quality and reviewed corrections

The dashboard's **Ingredients** tab (`/dashboard/ingredients`) provides a review queue grouped by
source and exact ingredient wording, with source/status/text filters and product counts. Open a group
to inspect product examples and current mappings, search the installed canonical dictionary, and select
one or more ingredients. For combinations, select every active constituent. Provide an evidence URL
or review note before saving. Selections are kept in a per-tab browser draft while searching.

Saving applies the reviewed override to **all products with that exact wording in that source** in one
transaction; future ingestion uses it too. Other sources, original product fields and prices are not
changed. Use the **manual** status filter to edit or remove corrections; removal immediately re-runs
automatic resolution. Stale forms are rejected rather than overwriting a newer correction. Empty
ingredient labels cannot be mapped this way, and new canonical identities still require a verified
dictionary import. Reviewed mappings also remain available through the dictionary export command.

The dashboard has no user authentication: run it only in a trusted operator environment or behind
authenticated access. Review forms use same-site cookies and form tokens to protect against
cross-site submissions; these are not a substitute for access control.

Only unambiguous complete aliases, documented source IDs, reviewed overrides and exact approved
same-country code links are accepted. Source component boundaries are retained from BDPM, dm+d and
Health Canada. Health Canada DIN links may supply Ontario constituents; provincial `DIN_PIN` values
are deliberately excluded from automatic DIN reconciliation. ATC classes, broad labels such as
“Hemofiltrados”, brand similarity and fuzzy spelling are not proof of ingredient identity.

Resolution is `resolved`, `partial`, `ambiguous` or `unresolved`. A known constituent in a partially
resolved combination is searchable, with its partial status exposed. Reports include records without
ingredient data (notably HCPCS billing entries and some UK items). A coverage gap is **not** proof that
a given ingredient is absent from that country.

```bash
python -m app ingredients export reviewed.json
# Edit a copy, preserving evidence, then validate/import it:
python -m app ingredients import reviewed.json
python -m app ingredients backfill --source fr_bdpm
```

Imports are complete alias snapshots for their `provider`; use a distinct provider name for reviewed
additions so a remote provider refresh cannot remove them. Each document contains `provider`,
`revision`, `ingredients` and optional `overrides`. Ingredient records contain `name`, namespaced
`identifiers`, `aliases` (`label`, `language`) and `evidence`. Optional `family` (`namespace`, `value`)
requires `family_evidence`; conflicting/cyclic families are rejected. A source-label override specifies
`source_id`, exact non-empty `source_text`, constituent `ingredients` references and `evidence`.
Manual overrides survive automatic dictionary refresh. Exports refuse to overwrite an existing file.

Wikidata structured data is [CC0](https://www.wikidata.org/wiki/Wikidata:Licensing).
RxNorm is accessed under the [RxNav terms](https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html):
**This product uses publicly available data from the U.S. National Library of Medicine (NLM), National
Institutes of Health, Department of Health and Human Services; NLM is not responsible for the product
and does not endorse or recommend this or any other product.**

## Sources (status October 2026)

| Source | Country | Format | Adapter status | Notes |
|---|---|---|---|---|
| `us_nadac` | US | DKAN API + CSV | supported | latest "As of Date" only by default; limited runs use the datastore API |
| `us_asp` | US | ZIP/CSV | supported | HCPCS = billing code, not a product; NDC↔HCPCS crosswalk kept raw |
| `fr_bdpm` | FR | TSV ×3 (no header, mixed encodings) | supported | licence: no alteration, cite source+date |
| `de_festbetrag` | DE | PDF | **partially supported** | Festbetrag groups only; internal use (IFA data rights) |
| `de_amnog` | DE | HTML (50 pages) | **partially supported** | negotiation status only – negotiated amounts are not public |
| `de_gba` | DE | official TrG PDFs | **partially supported** | full historical catalogue discovery; layout-dependent pack-price extraction with explicit document exclusions; no current-price/PZN/unique brand claim |
| `it_aifa` | IT | CSV `;` cp1252 | supported | public price net of statutory reductions; Class H ex-factory |
| `es_nomenclator` | ES | CSV | supported | documentary status: informative |
| `es_cipm` | ES | official CIPM PDF archive | **partially supported** | all linked historical documents; explicit proposed maximum industrial prices, not final/effective financing or retail prices; exclusions audited |
| `es_cima` | ES | official JSON API | **partially supported** | targeted medicine/presentation reconciliation with national codes; product coverage only, no prices |
| `uk_dmd` | GB | TRUD API, ZIP/XML | supported (needs `TRUD_API_KEY`) | XML layout from the dm+d data model – not yet validated live |
| `uk_drug_tariff` | GB | CSV | supported | prices in pence, excl. VAT; VMPP ids shared with dm+d |
| `jp_nhi` | JP | XLSX ×4 | supported | price per official unit (1錠, 1g…) |
| `br_cmed` | BR | XLSX (PMC + PMVG) | supported | latest dated links accept single/double underscores before the date; CC BY-ND → internal only; PMVG formulas evaluated (`calculated`) |
| `ca_on_odb` | CA (ON) | XML | supported | discovers current XML from Ontario's publication page; separate published unit price and ministry-payment observations |
| `ca_bc_pharmacare` | CA (BC) | ZIP/CSV | supported | benefit ceilings per official unit; effective records in the file edition; DIN/PIN and provincial plans retained |
| `ca_hc_dpd` | CA | ZIP/CSV | supported | product master (DIN, ATC), no prices |
| `cn_nhsa` | CN | manual CSV | **manual_or_unsupported** | no anti-bot circumvention; drop VBP CSVs in `data/manual/cn_nhsa/` (see `TEMPLATE.csv.example`) |
| `cn_vbp` | CN | public NHSA PDFs | **partially supported** | historical GY-YD2024-2 **proposed** procurement prices; not final awards, retail prices, or complete China coverage |
| `cn_gdmede` | CN (Guangdong) | public JSON + attachments | **partially supported**, disabled by default | complete public drug-notice/query pagination; original-price export and exclusion audit; explicit CNY pack listings normalized separately |
| `cn_hubei` | CN (Hubei) | public catalogue + attachments | **partially supported**, disabled by default | full public archive enumeration; access failures audited; original-price export without guessing currency or units |

### Measuring coverage and interpreting prices

```bash
python -m app coverage --json > before.json
python -m app ingest --source ca_on_odb --source ca_bc_pharmacare --source jp_nhi
python -m app ingest --source de_gba --source cn_vbp --source es_cima --source es_cipm --source de_festbetrag
python -m app coverage --json > after.json
```

The default benchmark matches Dupixent, dupilumab and their Japanese names. Repeated `--query` terms
are alternatives (OR), not one combined drug name. Counts distinguish product-only matches from products
with positive prices; multiple price variants do not inflate the product count. Sources with no match
are shown explicitly. Items are not deduplicated across sources.

`is_current` means the latest stored observation in a source series, **not** proof that a medicine is
currently marketed, that a disclosure is recent, or that a procurement price is final. Use the JSON
details to inspect dates, units, price types and notes before comparing prices.

Ontario's old CKAN resource points to a retired hostname. Discovery now uses the current Ontario
publication page; `CA_ON_ODB_XML_URL` remains available for an operator-supplied extract. Its
`individualPrice` (Drug Benefit Price) is stored as `ex_factory/individual_price`, including entries explicitly marked
`notABenefit=Y`; it is not automatically a reimbursement entitlement. `amountMOHLTCPays` is a separate
`reimbursement/amount_moh_pays` observation. Ingredient groups and manufacturer names are resolved from
the XML hierarchy. Older imports labelled every individual price as reimbursement; newly imported
observations use the corrected, explicit series.

BC's file contains historical and repeated plan records. The adapter selects benefit records effective
on the **file edition date**, excludes `NB` (non-benefit), treats zero price fields as unpublished
placeholders, and merges identical price/date observations across plans. Max/LCA/RDP values remain
separate `reimbursement` series with `price_status=ceiling`; RDP plan exclusions are honoured. Prices are per the official
pricing unit, not assumed to be per pack or syringe. A DIN/PIN may be a provincial PIN rather than a DIN;
do not automatically join every eight-digit identifier to Health Canada.

NHI remains a Japanese reimbursement-price source. Refreshing its four official workbooks adds newly
listed codes but does not provide prescription acquisition prices or OTC retail prices.
The [Ministry's 2024 page](https://www.mhlw.go.jp/topics/2024/04/tp20240401-01.html)
is a historical list (through March 31, 2025), not an additional current-price source.
`jp_nhi` starts from the Ministry's index and selects the latest annual page and newest workbook
independently for oral, injectable, topical and dental medicines; the categories can have different
edition dates. Missing categories fail discovery rather than silently reducing coverage.

[Yakka Search](https://yakka-search.com/) is useful for checking Japanese product names and comparing
current and previous NHI prices, but its displayed `薬価` does not establish a separate acquisition,
ex-factory or commercial retail price. On October 22, 2026, its four Dupixent results matched the
official NHI amounts exactly: JPY 53,493 (300 mg syringe), 53,659 (300 mg pen), 39,549 (200 mg syringe)
and 39,706 (200 mg pen), per syringe/kit, not per mL or assumed multi-dose pack.
These four products are already covered by `jp_nhi`; no duplicate Yakka price feed is enabled.
The site's update date is not a medicine's effective price date, and old-price columns must not be
imported as current prices. Broader historical imports would need separate edition-aware ingestion.

Germany's Festbetrag parser also handles adjacent monetary columns in high-priced rows. It remains
limited to Festbetrag groups. `de_gba` supplements this with explicit pack-price tables from official
justifications, excluding annual therapy costs. By default it traverses **every page of the full
historical G-BA procedure catalogue**, checks the unique procedure count against the site's result
count, and visits every procedure for dated justifications. There is no two-medicine allowlist or
50-procedure cap. Catalogue pagination/layout errors stop discovery rather than silently truncating it.
The first historical import can take a long time: procedures and PDFs are fetched politely in sequence,
with progress counters at catalogue, procedure, download and extraction stages.

To restrict an import deliberately, set `de_gba.options.procedure_urls` in `app/registry/sources.yaml`
to a non-empty list of official procedure URLs. Omit this option to restore full-catalogue discovery.
Targeted snapshots are marked partial and do not replace the full catalogue's row-count baseline.
Procedures without a justification (including ongoing/exempted procedures) are retained in the
generated `catalogue_manifest.json` inside the raw snapshot. Individual procedure/download failures
are logged and preserved there; unsupported, corrupt or conflicting PDF tables are excluded as a whole
and reported as job warnings, without discarding other documents' valid prices. A document-coverage
summary in the job's errors/details lists all excluded PDFs and reasons. An import with no usable prices
fails explicitly. **Full catalogue discovery does not mean every historical PDF contains a supported
price table**; some older justifications are narrative only. `partial_success` is expected and signals
the source's limitations, not a failed import. `0 new` with `unchanged > 0` means existing observations
were seen again.

German originals are preferred to translations by document ID, not drug-name
spelling; both `_TrG_EN` and `_EN_TrG` translation filenames are recognised. Explicit PDF title dates
take precedence over filename typos, with corrections logged; unverified future dates are rejected.
Gross retail and net-after-statutory-rebate amounts remain separate; neither is a confidential
AMNOG negotiated amount. Clinic acquisition observations use the **final cost including VAT**, with
the before-VAT purchase cost and VAT recorded in notes and checked against the total. Grouped integer
amounts such as `3 200 EUR` are preserved without truncation. Disclosure product IDs are
stable hashes, **not PZNs**, and do not imply a unique brand. `date_basis=reference_date` marks the explicit
Lauer-Taxe reference date, not a proven current effective price.

`cn_vbp.options.documents` configures up to 50 public NHSA PDF URLs, with mandatory `batch` and
`status` (`proposed` or `final`), plus optional verified ISO dates `published_at`, `effective_from` and
`effective_to`. The default is a historical proposed-results document with 385 price rows; no effective
or publication date is invented from its batch number. Proposed prices use `reference` status and an
explicit `:proposed` variant/notes, not a final award or current Chinese retail price. Units follow the
PDF's billing unit; `瓶/盒` remains the ambiguous `bottle_or_box`, without pack conversions. Proposed
documents cannot establish effective validity dates. Supplier-only results lacking price columns fail
explicitly. The manual `cn_nhsa` path is unchanged; no protected portal is bypassed.

### Provincial Chinese disclosures: opt-in research, not a national tariff

`cn_gdmede` and `cn_hubei` default to **full public-catalogue enumeration**, without a document or
row cap. Guangdong enumerates every page of the drug-notice API (`label=collect`, `count=100`) and
the public procurement query (`rows=50`); received identities must match the advertised totals,
without duplicates, repeated pages, changing totals or premature empty pages. Hubei reads the full
`zcwj.json` public archive rather than only the ten entries displayed per page. Drug-related notices,
including historical/proposed notices and traditional specialties, are inspected for attachments and
inline tables. Non-drug notices/attachments and all unavailable documents are recorded in the audit.
This means the complete accessible catalogue, **not** a guarantee that every site section or
unpublished price is covered.

Both sources are disabled and require `options.internal_research_acknowledged: true` in
`app/registry/sources.yaml` before explicit ingestion:

```bash
python -m app ingest --source cn_gdmede --source cn_hubei --dry-run
```

Remove `--dry-run` to publish eligible observations and products. Even a dry run persists immutable
raw snapshots and a derived original-price extraction:

```text
storage/raw/<source>/<snapshot>/public_manifest.json
storage/raw/<source>/<snapshot>/download_report.json
storage/extractions/<source>/<snapshot>/prices.jsonl
storage/extractions/<source>/<snapshot>/prices.csv
storage/extractions/<source>/<snapshot>/coverage.json
storage/extractions/<source>/<snapshot>/scan_inventory.json
storage/extractions/<source>/<snapshot>/scan_inventory.csv
```

Paths follow the configured raw-storage directory. `prices.jsonl` preserves each price column,
its original value/label, source URL, row reference, product cells, publication/effectivity and
final/previous/proposed status. `prices.csv` provides the same observations with standard product
columns for combining both sources. Prices whose currency, pack/unit basis, price concept or date is not
stated by the source, Guangdong public-query values, and previous/proposed/withdrawn/future values are
**published with warnings** (`published=True`, `quality_flags` in the export; `quality_flags` and readable
`quality_warnings` in the API/CSV/dashboard). Nothing is guessed: an unknown basis is stored as
`price_unit=unknown` (no per-unit value derived), an unknown concept as `price_basis=other` with variant
`unclassified:<original label>`, and an implicit currency as CNY flagged `currency_inferred_cny`.
Non-current values (`not_current_value`) get a `:<status>` variant suffix, are never marked current and
never supersede an established price, but are shown by default alongside current prices. Use
`verified_only=true` (API) or *hide prices published with warnings* (dashboard) to see only fully
established prices. Raw herbal materials, templates, aggregates and daily costs remain unpublished.
Non-scalar cells and nonpositive values retain
explicit reasons; a malformed cell does not discard the other valid prices in the document.
`价格形成方式`, price scores, company identifiers and price-unit columns are not prices.
`value_kind` distinguishes prices, benchmark prices, daily treatment-cost benchmarks and aggregate monetary amounts.
Contract totals are retained for audit but never normalized into per-unit prices; benchmark
and selected/shortlisted variants remain distinct from final awards. Daily-cost or mixed daily-cost/bid-limit
columns are retained separately and never normalized as per-pack/unit prices. Training-manual examples
and template values are not published as prices. Mixed notices can attach diagnostic-reagent or
medical-device tariffs: explicit device-table headers and non-drug attachment labels exclude
those tables from drug-price exports and publication.
Clearly non-drug attachments remain listed with their exclusion reasons but are not downloaded.
Catalogue discovery does not require the word "drug" in notice titles: historical market-entry
prices, nationally organized procurement and medicine-specific notices can omit it.
Generic product tables still require a drug schema, drug context, national drug code or drug
authorisation evidence; unrelated supplies and devices are not inferred to be medicines.
The coverage report separates numeric values by kind, unparsed cells, normalizable observations,
per-document counts and exclusions.
These counts are observations/cells, not unique medicines or current offers.

Supported tables: XLSX, XLS, textual tabular PDF, CSV, DOCX and safely bounded ZIP/RAR/7z members.
RAR/7z use `libarchive-c` and the system libarchive library; a missing native library is explicitly
audited. File signatures identify OOXML workbooks/documents mislabeled as legacy XLS/DOC.
Legacy DOC tables use `textutil` when installed (macOS); otherwise the missing converter is audited.
This workflow is digital-only: no OCR is performed. The scan inventories identify image-only PDF
pages by official attachment URL, archive-member path where applicable and 1-based page number.
They inventory unprocessed pages, not evidence that every such page contains prices.
Images/image-only PDFs, unsupported layouts, corrupt or inaccessible files remain in the
audit rather than being presented as successfully extracted. Four concurrent requests retain the
shared HTTP client's per-host politeness delay and existing retry rules.

Hubei can return HTTP 412 for public notice HTML. No challenge, token, authentication or CA-certificate
bypass is attempted. An operator can supply `options.browser_notice_archive` pointing to a JSON
capture made through ordinary public-browser access:
`{"notices":[{"url":"https://ybj.hubei.gov.cn/...","status":200,"html":"...","attachments":[{"url":"https://ybj.hubei.gov.cn/...xlsx","fileName":"..."}]}]}`.
The capture is snapshotted; official catalogue titles/dates still drive interpretation. Missing
capture entries use ordinary HTTP and failures remain explicit. Guangdong interrupted discovery
can reuse already saved notice responses through `options.notice_cache_dir`; catalogues/query pages
are always fetched anew, and cache availability is recorded in the manifest.
To reparse/publish an already collected full snapshot without re-downloading thousands of files,
set `options.public_snapshot_dir` to its directory and run the same ingestion command. Source identity,
public-manifest presence and every raw SHA-256 are verified before replay; remove this option to
resume fresh discovery.

Set `enabled: true` only if they should also participate in `--all`/`--country CN`.
The acknowledgement is **not permission for commercial use**.
Review the [Guangdong declaration](https://www.gdmede.com.cn/about/laws) (written permission for
commercial extraction/derivatives) and the [Hubei declaration](https://ybj.hubei.gov.cn/wzsm/202301/t20230110_4483481.shtml)
before use beyond the agreed internal research. Original restricted workbooks are not bundled as
test fixtures; provincial tests build independently authored synthetic tables.

For the previous **bounded, reviewed-only mode**, set `options.documents` to a non-empty list
(maximum 100); this explicitly replaces full discovery. Each entry
requires an official HTTPS XLSX `url`, official `notice_url`, supported `kind`, `status`
(`final`/`proposed`) and reviewed ISO `published_at`; optional `effective_from` must come from the
notice, not the attachment path, batch number or publication date. Example:

```yaml
documents:
  - url: "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202610/P020261014552464734543.xlsx"
    notice_url: "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202610/t20261014_6032706.shtml"
    kind: hb_admission
    status: final
    published_at: "2026-10-12"
    effective_from: "2026-10-12"
```

Supported kinds: `gd_listing`, `hb_admission`, `hb_adjustment`, `hb_award_adjustment`.
Metadata is saved as `reviewed_documents.json` inside the immutable raw snapshot, so resume uses the
original interpretation even if the registry changes. Local identifiers, declaring companies, original
prices and all source columns remain in the raw workbook. Header changes, unknown layouts,
supplier-only lists, duplicate conflicting prices and invalid amounts fail explicitly.
Formatting-inflated Excel dimensions are bounded rather than expanded into millions of blank cells.

Guangdong's `包装挂网价格（元）` explicitly publishes a CNY pack price. Full extraction can also
use an explicit table currency caption (`金额单位：元`) and pricing-unit column, keeping retail
ceilings, reimbursement standards and procurement/awarded prices in distinct types/variants.
Withdrawn/abolished tables, proposals and previous-value columns stay in the original-price export,
not the current-price feed. Other schema conventions
are **not guessed**: in reviewed-only mode Hubei emits products and job warnings, but no prices, unless an operator has
reviewed the currency/pack-price basis and adds `cny_pack_evidence_url` pointing to an official Hubei
document establishing that basis **for the particular table**. A plausible price, matching price in
another province, code, or mere URL does not establish this convention; this option records a human
review, it does not automatically verify the evidence. Never set it just to suppress a warning.
Adjustments without an unambiguous `effective_from` also remain product-only. No old-price start date
is invented; the prior disclosed amount is preserved in notes, not inserted as a current price.

Eligible pack-listing prices use `public_procurement/guawang_pack_listing`, `price_status=reference`, and
`coverage_scope=province:CN-GD` or `province:CN-HB`. Proven award adjustments use the separate
`awarded_pack_price` variant. These are published listings/awards, **not** observed purchases,
retail prices, or reimbursement amounts. Proposed or future-effective publications do not create
current prices. A proposed suspension or absence from a new incremental batch never closes a series.
`is_current` remains the latest collected observation, not verified market validity.

Products retain full national codes; no cross-province product merge, minimum-price selection or
join by authorisation alone is performed. Exact-code descriptions within a source may enrich an
adjustment only if identity/pack fields agree. Unknown Chinese pack expressions produce no derived
unit price; `支` is a generic unit, not an inferred syringe. Traditional specialties are separately
classified and not assigned a fabricated single ingredient. Raw herbs and devices are not promoted to
internationally comparable drug prices. Supplier-only documents cannot supply prices. The Guangdong
public query's amounts are now fully extracted, but its unverified units/dates still prevent
comparable price facts; multiplying or dividing them by pack size would invent evidence.

`es_cima.options.medicine_names` defines a **targeted** product-only catalogue, initially Dupixent,
Keytruda, Opdivo, Humira and Lynparza. Paginated searches and medication detail responses are preserved
as raw JSON; presentations retain their official Spanish national codes and marketing status.
Reconcile these with `es_nomenclator` by national code, not medicine-name similarity. Missing from the
Nomenclator export does not establish that a medicine is unfinanced, and CIMA does not supply prices.
The coverage JSON includes `national_code_reconciliation` for each CIMA match: whether the exact
national code exists in the stored Nomenclator and has a positive latest observation there. This lookup
does not depend on name matching, and never copies a Nomenclator price onto a CIMA record.
Marketing status is included so unmarketed presentations are not mistaken for currently sold packs.

`es_cipm` discovers every distinct PDF linked from the Ministry's
[CIPM agreements and information notes](https://www.sanidad.gob.es/areas/farmacia/precios/comisionInteministerial/acuerdosNotasInformativas/home.htm).
It imports explicit positive table prices, preserves national codes in `group_code`
(`group_name=CODIGO_NACIONAL`), and keeps each dated disclosure under a source-specific
`CIPM_DISCLOSURE` identity. Merged cells are inherited only when their PDF geometry proves the span.
When an old/new comparison is explicit, the proposed new amount is used, not the previous price.
Ambiguous multi-date schedules, unsupported units/layouts, missing prices and denied proposals are
not converted into prices. Document and row/table exclusions are recorded in job issues and in
the parse coverage summary; downloading the complete archive does not imply complete extraction.

These are **proposed maximum industrial/ex-factory prices per presentation**, with `reference`
status and `cipm_<session>:proposed_industrial` variants. The meeting date is stored as
`published_at` with `date_basis=meeting_date`; `valid_from`/`valid_to` remain unknown.
The Ministry states that agreements are not definitive or effective until the final resolution
and Nomenclator inclusion. A favourable proposal is not proof of current reimbursement entitlement,
retail VAT, confidential net prices or current market validity. Repeated disclosures remain
historical evidence; `is_current` only identifies the latest observation of each disclosure series.
Unpriced informational notes are explicitly excluded, not treated as missing zero-price products.

## Dashboard

`/dashboard` (sources, last run figures, schema status, next expected update),
`/dashboard/jobs/{id}` (stage-by-stage ✓/⚠/✗ with durations and metrics), `/dashboard/errors` (filters:
source, severity, type, job, dates), `/dashboard/data` (data viewer: every product with its prices, filters on
source, country, price type and text, optional price history, CSV download of all fields) and
`/dashboard/data/{product_id}` (all product fields + full price history).

## REST API

`GET /health`, `/sources`, `/sources/{id}`, `/runs`, `/jobs`, `/jobs/{id}`, `/errors`,
`/prices` (filters `source_id`, `country`, `price_type`, `q`, `current_only`; one row = product + price fields;
products without price appear with empty price fields), `/prices.csv` (same filters, all fields, streamed),
`/products/{id}` (product + price history, optional `target_currency` FX conversion).
List endpoints are paginated (`page`, `page_size` ≤ 500). Amounts are returned as exact decimal strings.

## Tests

```bash
pytest            # fully offline: fixtures in tests/fixtures/<source_id>/
```

Per source: parsing, normalisation, generic fields filled, validation, schema-change detection, snapshot
integrity, known values. Pipeline: limit, product filter, dry-run, resume, history, schema blocking, secrets.
API & dashboard: filters, CSV, data viewer, product detail. Migrations.

## Adding a source

1. `app/adapters/<source>.py`: subclass `SourceAdapter` (`discover`, `parse`, `expected_schemas`,
   `normalize_record`; override `download`/`validate` only if needed). Map source columns to the generic
   `SourceProductData` / `PriceFact` fields.
2. Register it in `app/adapters/__init__.py` and add an entry in `app/registry/sources.yaml`.
3. Add fixtures in `tests/fixtures/<source>/`, routes in `tests/conftest.py`, and the source to the test lists
   (including `FILLED_FIELDS` in `tests/test_adapters.py`).

No change to the pipeline core is required.
