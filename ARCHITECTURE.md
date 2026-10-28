# Architecture

## Stack (repository was empty — choices made here)

| Concern | Choice | Why |
|---|---|---|
| Language | Python ≥ 3.11 | Best ecosystem for CSV/XLSX/PDF/XML parsing |
| API | FastAPI (REST, OpenAPI at `/docs`) | Typed, simple, async-capable |
| Dashboard | Server-rendered Jinja2 pages served by the same FastAPI app (`/dashboard`) | No JS build chain to maintain; the dashboard only consumes the same services as the REST API, so a React/Next.js front can be added later without touching the backend |
| DB | PostgreSQL (prod/dev via Docker), SQLite (offline tests) | SQLAlchemy 2.0 models kept portable (JSON, string enums) |
| Migrations | Alembic (`migrations/`) | `python -m app db upgrade` |
| Tests | pytest, fully offline (fixtures + fake HTTP client) | `tests/fixtures/<source_id>/` |
| Config | Environment variables (`.env`, see `.env.example`) + `app/registry/sources.yaml` | Secrets never in the repo |
| Packaging | `docker-compose.yml`: `postgres`, `backend` (API + dashboard) | |

## Layers

```
Source Registry (app/registry/sources.yaml → table source)
      ↓
Source Discovery          adapter.discover()      → SourceArtifact (exact URLs, edition date)
      ↓
Download                  adapter.download()      → files on disk
      ↓
Raw Snapshot Storage      SnapshotStore           → storage/raw/<source>/<timestamp>/ + metadata.json (sha256)
      ↓
Parsing                   adapter.parse()         → ParsedDataset (+ TableProfile per file)
      ↓
Schema Validation         core/schema.py          → expected schema + baseline comparison → SCHEMA_CHANGED blocks publish
      ↓   (--limit / --product-id applied here, before expensive steps)
Normalization             adapter.normalize()     → NormalizedRecord (generic product fields + PriceFacts)
      ↓
Validation / Quality      adapter.validate()      → warnings (never deletes data)
      ↓
Price Normalization       core/pricing.py         → unit_price / unit_price_unit next to the published amount
      ↓
Database                  core/publisher.py       → product (upsert) + append-only price history
      ↓
Dashboard / API           app/api, app/dashboard  → monitoring + data viewer
```

The pipeline (`app/core/pipeline.py`) only knows the `SourceAdapter` contract
(`app/core/adapter.py`). Each source lives in `app/adapters/<source>.py`; adding a
source = one adapter class + one registry entry + fixtures. No source-specific logic
exists in the core.

## Key decisions

* **Raw first.** Every download is stored untouched with its SHA-256 before any
  parsing. Snapshot directories are timestamped and never overwritten. A snapshot is
  reused only if the discovered URL *and* edition date are identical (or with `--resume`).
* **Schema change detection** is two-fold: the adapter declares an expected schema
  (required columns / column count), and the last accepted profile (columns, inferred
  types, row count) is stored in `schema_baseline`. Any drift → job status
  `schema_changed`, nothing published, error visible in the dashboard. Accept with
  `--accept-schema-change` after review.
* **One product table, generic fields.** Each source maps its columns to the same generic fields
  (`SourceProductData`, `PriceFact`); a field stays empty when the source does not publish it, and
  information without a dedicated field goes to `notes`. Each row belongs to exactly one source;
  source products are never merged across countries.
* **Ingredient discovery is an annotation layer.** Canonical ingredient identities, multilingual
  aliases, documented substance families and many-to-many product membership allow cross-country
  ingredient searches without asserting product equivalence. Dictionary synchronization is explicit
  and cached; ingestion/search use local mappings only. Ambiguous/missing evidence remains visible,
  reviewed source-label overrides are preserved, and backfill does not rewrite price history.
* **Price semantics are explicit**: `price_type` (supply-chain level: ex_factory, wholesale, retail,
  reimbursement, public_procurement – the comparability axis), `price_status` (incl. ceiling),
  `price_variant` (source concept), VAT flag, tax rate, dispensing fee, statutory reductions flag, unit,
  coverage scope. The published amount/currency/unit are
  never altered; the per-unit price and FX conversions are separate columns / computed views.
* **History is append-only**: a new price value creates a new observation; the previous
  one is flagged `is_current = false` with `superseded_at` (its price is never updated).
* **Limited runs**: `--limit` / `--product-id` are applied after parsing; adapters may
  receive the limit as a *hint* to stop streaming parsers early (PDF, huge XLSX) – the
  dataset is then flagged `truncated` and row-count drift checks are skipped.
* **Dry-run** executes everything inside a DB transaction that is rolled back; only the
  job, its errors and the raw snapshot are persisted.
* **Resume**: publication commits per batch together with a job checkpoint
  (`published_upto`). `--resume` reuses the snapshot of the last interrupted job and
  skips already published records.
* **Unsupported interfaces are explicit** (`adapter_status = manual_or_unsupported`).
  Protected Chinese query interfaces are not bypassed; the manual CSV path remains available separately
  from importers of publicly downloadable procurement documents.
* **Coverage is not price equivalence.** `python -m app coverage` counts matching source products
  separately from positive price observations. Latest stored observations can be historical,
  provisional, or province-specific; dates, units, variants and notes remain attached.
* **Chinese public extraction is digital-only and audited.** Derived CSV/JSONL exports retain
  original price cells separately from normalized business observations. Price scores and company/unit
  fields are excluded; aggregate monetary totals are identified but never treated as unit prices.
  Image-only PDF pages are inventoried with official URLs, nested archive-member paths and page
  numbers; no OCR or completeness claim for unsupported layouts is made.
* **Incomplete semantics are flagged, not guessed.** `PriceFact.quality_flags` and `never_current`
  let adapters publish figures whose currency, basis, concept or date is implicit. Unknown bases use
  `price_unit=unknown` (no unit price), unknown concepts `PriceBasis.OTHER`; `never_current` facts get their
  own series and never supersede. Queries include them by default; `DataFilter.verified_only` excludes
  every flag in `quality.UNVERIFIED_FLAGS`.

## Data model

`source` (registry) · `ingest_run` → `job` → `job_error` (operations) · `raw_snapshot` + `schema_baseline`
(provenance) · `product` (one row per source item, generic fields – see README) → `price` (amount, currency,
unit, type, variant, status, VAT, tax rate, dispensing fee, statutory reductions, unit price, validity period,
snapshot + row reference, quality flags, current/superseded) · `fx_rate` (spot / monthly / annual / PPP never
mixed) · `identifier_crosswalk` (prepared for NDC↔HCPCS).

Product levels: `presentation` (pack), `product` (no pack), `billing_code` (HCPCS), `regulatory_entry`
(AMNOG/G-BA) and `device` (ES accessories). Sources without prices (AMNOG, Health Canada DPD, CIMA)
only create products. G-BA, CIPM and Chinese procurement disclosure hashes are not national product identifiers.
CIPM retains the Spanish national code separately in `group_code`; each document/page/table/CN
disclosure has a stable source identity, proposed industrial/reference prices and unknown effective
validity. The meeting date is provenance, not an entitlement or market-validity date.

Prices without an explicit date in the source (BDPM, AIFA, Nomenclátor, JP, Drug Tariff…) get
`valid_from = edition date` with `date_basis = "edition_date"`; an unchanged value in a later edition does not
create a new observation (only `last_seen_*` is updated).

## Per-source status (October 2026)

| Source | Status | Main limitation |
|---|---|---|
| us_nadac, us_asp, fr_bdpm, it_aifa, es_nomenclator, uk_drug_tariff, jp_nhi, br_cmed, ca_hc_dpd | supported, verified live | – |
| uk_dmd | supported, needs `TRUD_API_KEY` | XML layout implemented from the dm+d data model, validated on synthetic fixtures only |
| de_festbetrag | partially supported | PDF; Festbetrag groups only; legal review pending (internal only) |
| de_amnog | partially supported | no negotiated amounts published → no prices |
| de_gba | partially supported | full historical catalogue discovery; explicit per-document exclusions for unsupported/non-price PDFs; no PZN or unique brand inference; Lauer-Taxe reference dates are not current validity |
| es_cima | partially supported | targeted official product reconciliation by national code, no prices/financing entitlement inferred |
| es_cipm | partially supported | all linked Ministry CIPM PDFs; explicit proposed industrial table prices; denied, ambiguous, unsupported and unpriced rows/documents excluded with audit issues; not final/current retail prices |
| ca_on_odb | supported, verified live | Ontario only; published unit price is distinct from ministry payment and benefit eligibility |
| ca_bc_pharmacare | supported, verified live | BC benefits only; effective as of the CSV edition; DIN/PIN and plan-specific pricing |
| cn_nhsa | manual_or_unsupported | no automatable public source; manual VBP CSV import path |
| cn_vbp | partially supported | bounded public procurement PDFs; default 2024-2 prices are proposed/historical, unknown effective dates, official billing units |
| cn_gdmede | partially supported, opt-in | full public drug-notice and procurement-query pagination; audited original-price export plus explicitly established CNY pack-listing observations |
| cn_hubei | partially supported, opt-in | full public archive enumeration; audited access/format exclusions, ordinary-browser capture import; original price cells retained; implicit currency/unit values published with warnings, never invented |

## Performance notes (v1)

Publication is row-by-row with a savepoint per record (isolation of bad rows). A full BDPM load
(20,925 presentations, 27k prices) takes ≈2.5 min on SQLite. Bulk upserts are the next optimisation
for NADAC-size files.
