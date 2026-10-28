"""Command line: python -m app <command>."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings
from app.observability import configure_logging, register_secrets


def _session_factory():  # noqa: ANN202
    from app.db.session import make_session_factory

    return make_session_factory(get_settings().database_url)


def cmd_ingest(args: argparse.Namespace) -> int:
    from app.core.pipeline import Pipeline, RunOptions, Selection

    settings = get_settings()
    if args.limit is not None and args.limit <= 0:
        print("--limit must be a positive integer", file=sys.stderr)
        return 2
    pipeline = Pipeline(settings, _session_factory())
    selection = Selection(source_ids=args.source or [], countries=args.country or [], all=args.all)
    options = RunOptions(
        limit=args.limit,
        product_ids=args.product_id,
        dry_run=args.dry_run,
        resume=args.resume,
        force_download=args.force_download,
        accept_schema_change=args.accept_schema_change,
        command="python -m app " + " ".join(shlex.quote(a) for a in sys.argv[1:]),
    )
    try:
        summary = pipeline.run(selection, options)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(summary, dry_run=args.dry_run)
    return 0 if all(j.status.value in ("success", "partial_success", "unsupported") for j in summary.jobs) else 1


def _print_summary(summary, *, dry_run: bool) -> None:  # noqa: ANN001
    t = summary.totals()
    print()
    print(f"Run {summary.run_id}{'  [DRY-RUN: database not modified]' if dry_run else ''}")
    print(f"{'Source':<20}{'Status':<17}{'Raw':>7}{'Parsed':>8}{'Norm.':>7}{'Products':>9}{'Prices':>8}{'Warn':>6}{'Err':>5}")
    for j in summary.jobs:
        m = j.metrics
        print(
            f"{j.source_id:<20}{j.status.value:<17}{m['records_downloaded'] or 0:>7}{m['records_parsed'] or 0:>8}"
            f"{m['records_normalized'] or 0:>7}{m['records_published'] or 0:>9}"
            f"{m['prices_published'] or 0:>8}{m['validation_warnings'] or 0:>6}{j.error_count:>5}"
        )
    print()
    print(f"{t['sources_discovered']} sources discovered")
    print()
    print(f"{t['sources_downloaded']} sources downloaded")
    print(f"{t['sources_partially_supported']} source(s) partially supported")
    print(f"{t['sources_unsupported']} source(s) unsupported")
    print(f"{t['sources_failed']} source(s) failed / schema changed")
    print()
    print(f"{t['records_downloaded']:,} raw records")
    print(f"{t['records_parsed']:,} parsed")
    print(f"{t['records_normalized']:,} normalized")
    print(f"{t['records_published']:,} products published")
    print(f"{t['prices_published']:,} new price observations")
    print(f"{t['validation_warnings']:,} validation warnings")
    print(f"{t['errors']:,} errors")


def cmd_sources(_: argparse.Namespace) -> int:
    from app.adapters import get_adapter_class
    from app.registry import load_registry

    for cfg in load_registry(get_settings().sources_file).values():
        cls = get_adapter_class(cfg.adapter)
        print(f"{cfg.source_id:<16} {cfg.country:<3} {cls.status.value:<22} {cfg.expected_frequency:<10} {cfg.name}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from app.api.queries import product_coverage
    from app.registry import load_registry

    terms = args.query if args.query is not None else ["dupixent", "dupilumab", "デュピクセント", "デュピルマブ"]
    if not any(term.strip() for term in terms):
        print("--query must contain a non-empty search term", file=sys.stderr)
        return 2
    registry = load_registry(get_settings().sources_file)
    with _session_factory()() as session:
        rows = product_coverage(session, terms, list(registry))
    for row in rows:
        row["country"] = registry[row["source_id"]].country
    if args.json:
        print(json.dumps({"queries": terms, "semantics": "Latest positive observations per source series plus non-current values published with a not_current_value warning; not necessarily currently effective, final, or equivalent prices. quality_flags list published-with-warning reasons. Counts are source items, not unique medicines.", "sources": rows}, ensure_ascii=False, indent=2))
    else:
        print("Search terms (OR): " + ", ".join(terms))
        print(f"{'Source':<20}{'Country':<9}{'Products':>9}{'Priced':>9}{'Prices':>9}{'Flagged':>9}  Price types")
        for row in rows:
            print(f"{row['source_id']:<20}{row['country']:<9}{row['products']:>9}{row['priced_products']:>9}{row['price_observations']:>9}{row['price_observations_with_warnings']:>9}  {', '.join(row['price_types'])}")
        print("Prices = latest positive observations per source series, not proof of current market availability.")
        print("Flagged = published with warnings (inferred currency, unknown basis/concept, non-current value...).")
        print("Source items are not deduplicated across sources. Use --json for amounts, dates, units and limitations.")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    if args.action == "upgrade":
        if get_settings().database_url.startswith("sqlite:///"):
            Path(get_settings().database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(cfg, "head")
    elif args.action == "current":
        command.current(cfg)
    return 0


def cmd_fx(args: argparse.Namespace) -> int:
    from app.core.http import HttpClient
    from app.fx.service import update_from_ecb

    s = get_settings()
    http = HttpClient(user_agent=s.http_user_agent, timeout=s.http_timeout)
    with _session_factory()() as session:
        added = update_from_ecb(session, http, history=args.history)
        session.commit()
    print(f"{added} ECB rates stored")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="Drug price collection platform")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging (batch commits, per-file details)")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING… (default: LOG_LEVEL or INFO)")
    parser.add_argument("--log-format", choices=["auto", "text", "json"], help="default: LOG_FORMAT or auto")
    parser.add_argument("--progress-interval", type=float, help="Seconds between progress lines (default 5)")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Run the pipeline for one or more sources")
    target = ing.add_mutually_exclusive_group(required=True)
    target.add_argument("--source", action="append", help="Source id (repeatable)")
    target.add_argument("--country", action="append", help="ISO country code (repeatable, e.g. FR, US, UK)")
    target.add_argument("--all", action="store_true", help="All enabled sources")
    ing.add_argument("--limit", type=int, help="Process only the first N records per source (applied after parsing)")
    ing.add_argument("--product-id", action="append", help="Only this national identifier (repeatable)")
    ing.add_argument("--dry-run", action="store_true", help="Download/parse/validate without modifying the database")
    ing.add_argument("--resume", action="store_true", help="Resume the last interrupted job of each selected source")
    ing.add_argument("--force-download", action="store_true", help="Download even if the same edition is already stored")
    ing.add_argument("--accept-schema-change", action="store_true", help="Publish despite a detected schema change (after review)")
    ing.set_defaults(func=cmd_ingest)

    sub.add_parser("sources", help="List registered sources").set_defaults(func=cmd_sources)
    coverage = sub.add_parser("coverage", help="Measure product and price coverage (default: Dupixent/dupilumab)")
    coverage.add_argument("--query", action="append", help="Name, ingredient or identifier substring; repeat for OR matching")
    coverage.add_argument("--json", action="store_true", help="Include exact observations, units, dates and limitations as JSON")
    coverage.set_defaults(func=cmd_coverage)

    ingredients = sub.add_parser("ingredients", help="Local multilingual ingredient dictionary and mapping")
    actions = ingredients.add_subparsers(dest="action", required=True)
    sync = actions.add_parser("sync", help="Explicitly fetch public reference data (never used by search)")
    sync.add_argument("--provider", choices=["rxnorm", "wikidata"], required=True)
    sync.add_argument("--entity", action="append", help="Wikidata QID (repeatable)")
    sync.add_argument("--discover", type=int, metavar="LIMIT", help="Discover up to LIMIT Wikidata entities (1-10000)")
    sync.set_defaults(func=cmd_ingredients)
    imp = actions.add_parser("import", help="Validate and atomically import a dictionary/curation JSON snapshot")
    imp.add_argument("path", type=Path)
    imp.set_defaults(func=cmd_ingredients)
    export = actions.add_parser("export", help="Export dictionary and reviewed source overrides")
    export.add_argument("path", type=Path)
    export.set_defaults(func=cmd_ingredients)
    backfill = actions.add_parser("backfill", help="Resolve existing products using only the installed dictionary")
    backfill.add_argument("--source", help="Restrict to one source")
    backfill.set_defaults(func=cmd_ingredients)
    actions.add_parser("report", help="Report mapping coverage and unresolved gaps").set_defaults(func=cmd_ingredients)

    db = sub.add_parser("db", help="Database migrations")
    db.add_argument("action", choices=["upgrade", "current"])
    db.set_defaults(func=cmd_db)

    fx = sub.add_parser("fx", help="Exchange rates")
    fx.add_argument("action", choices=["update"])
    fx.add_argument("--history", action="store_true", help="Load the full ECB history")
    fx.set_defaults(func=cmd_fx)

    srv = sub.add_parser("serve", help="Run API + dashboard")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--reload", action="store_true")
    srv.set_defaults(func=cmd_serve)
    return parser


def cmd_ingredients(args: argparse.Namespace) -> int:
    from pydantic import ValidationError
    from sqlalchemy import select

    from app.api.queries import ingredient_coverage
    from app.core.errors import DownloadError
    from app.core.http import HttpClient
    from app.db.models import Ingredient, IngredientAlias, IngredientDictionary, IngredientIdentifier, IngredientOverride, Product
    from app.normalization.ingredient_dictionary import (
        DictionaryDocument, cache_json, discover_wikidata, fetch_rxnorm, fetch_wikidata, install_dictionary,
    )
    from app.normalization.ingredients import backfill
    from app.normalization.text import fold

    settings = get_settings()
    cache_dir = settings.raw_storage_dir.parent / "ingredient_dictionary"
    try:
        with _session_factory()() as session:
            if args.action == "report":
                result = ingredient_coverage(session)
            elif args.action == "backfill":
                if args.source:
                    from app.db.models import Source
                    if session.get(Source, args.source) is None:
                        raise ValueError(f"Unknown source {args.source}")
                result = backfill(session, source_id=args.source)
            elif args.action == "export":
                identifiers = {}
                evidence = {}
                for row in session.scalars(select(IngredientIdentifier)):
                    identifiers.setdefault(row.ingredient_id, {})[row.namespace] = row.value
                    evidence.setdefault(row.ingredient_id, set()).add(row.evidence)
                aliases = {}
                for row in session.scalars(select(IngredientAlias)):
                    aliases.setdefault(row.ingredient_id, []).append({"label": row.label, "language": row.language})
                    evidence.setdefault(row.ingredient_id, set()).add(row.evidence)
                records = []
                for row in session.scalars(select(Ingredient)):
                    record = {
                        "name": row.name, "identifiers": identifiers[row.id], "aliases": aliases.get(row.id, []),
                        "evidence": "; ".join(sorted(evidence.get(row.id, set()))),
                    }
                    if row.family_id:
                        namespace, value = sorted(identifiers[row.family_id].items())[0]
                        record.update(family={"namespace": namespace, "value": value}, family_evidence=row.family_evidence)
                    records.append(record)
                overrides = []
                for row in session.scalars(select(IngredientOverride)):
                    refs = []
                    for ingredient_id in row.ingredient_ids:
                        namespace, value = sorted(identifiers[ingredient_id].items())[0]
                        refs.append({"namespace": namespace, "value": value})
                    overrides.append({
                        "source_id": row.source_id, "source_text": row.source_text,
                        "ingredients": refs, "evidence": row.evidence,
                    })
                document = DictionaryDocument.model_validate({
                    "provider": "reviewed", "revision": "local-export", "ingredients": records, "overrides": overrides,
                })
                if args.path.exists():
                    raise ValueError(f"Refusing to overwrite existing export {args.path}")
                args.path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
                result = {"exported": str(args.path), "ingredients": len(records)}
            else:
                if args.action == "import":
                    document = DictionaryDocument.model_validate_json(args.path.read_text(encoding="utf-8"))
                    path = cache_json(cache_dir, document.provider, document.model_dump())
                else:
                    http = HttpClient(
                        user_agent=settings.http_user_agent, timeout=settings.http_timeout,
                        delay_seconds=max(settings.http_delay_seconds, 0.5),
                    )
                    try:
                        if args.provider == "rxnorm":
                            labels = {fold(name) for name in session.scalars(
                                select(Product.ingredient).where(Product.ingredient.is_not(None)).distinct()
                            )}
                            document, path = fetch_rxnorm(http, cache_dir, substance_names=labels)
                        else:
                            entity_ids = args.entity or []
                            if args.discover:
                                entity_ids += discover_wikidata(http, limit=args.discover)
                            known_rxnorm = set(session.scalars(select(IngredientIdentifier.value).where(
                                IngredientIdentifier.namespace == "rxnorm",
                            )))
                            document, path = fetch_wikidata(http, cache_dir, entity_ids, known_rxnorm)
                            previous = session.get(IngredientDictionary, "wikidata")
                            if previous:
                                old = DictionaryDocument.model_validate_json(Path(previous.cache_path).read_text(encoding="utf-8"))
                                requested = set(entity_ids)
                                combined = {
                                    r.identifiers["wikidata"]: r for r in old.ingredients
                                    if r.identifiers["wikidata"] not in requested
                                }
                                combined.update({r.identifiers["wikidata"]: r for r in document.ingredients})
                                document.ingredients = list(combined.values())
                                path = cache_json(cache_dir, document.provider, document.model_dump())
                    finally:
                        http.close()
                result = install_dictionary(session, document, cache_path=path)
                session.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ValidationError, ValueError, OSError, DownloadError) as exc:
        print(f"Ingredient operation failed: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    from app import observability

    settings = get_settings()
    args = build_parser().parse_args(argv)
    level = "DEBUG" if args.verbose else (args.log_level or settings.log_level)
    configure_logging(level, args.log_format or settings.log_format)
    observability.PROGRESS_INTERVAL_SECONDS = args.progress_interval or settings.progress_interval_seconds
    register_secrets(settings.secrets)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted – use --resume to continue the interrupted job(s).", file=sys.stderr)
        return 130
