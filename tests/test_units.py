"""Unit tests: parsing helpers, normalization, quality rules, pricing, FX, snapshots, registry."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import ErrorType, PriceBasis, PriceStatus
from app.core.errors import EncodingError, ParserError
from app.core.parsing import decode_bytes, dedupe_headers, parse_date, parse_decimal, slug, sniff_delimiter
from app.core.pricing import normalize_price
from app.core.quality import check_record, variation_issue
from app.core.schema import compare_with_baseline
from app.core.types import NormalizedRecord, PriceFact, SourceArtifact, SourceProductData, TableProfile
from app.normalization.forms import normalize_form
from app.normalization.packs import parse_pack
from app.normalization.strength import parse_strength
from app.normalization.text import extract_brand
from app.registry import RegistryError, load_registry


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    "raw,sep,expected",
    [
        ("24,34", ",", "24.34"),
        ("2.264,69", ",", "2264.69"),
        ("1,234.56", None, "1234.56"),
        ("1 234,5", ",", "1234.5"),
        ("1,234,56", ",", "1234.56"),  # BDPM thousands bug
        ("0.26341", ".", "0.26341"),
        ("-0,14", ",", "-0.14"),
        ("€ 3,10", ",", "3.10"),
        ("1.234", ",", "1234"),
        (7.5, None, "7.5"),
    ],
)
def test_parse_decimal(raw, sep, expected):  # noqa: ANN001
    assert parse_decimal(raw, decimal_sep=sep) == Decimal(expected)


def test_parse_decimal_empty_and_formula():
    assert parse_decimal("    -     ") is None
    assert parse_decimal("") is None
    with pytest.raises(ParserError, match="formula"):
        parse_decimal("=ROUND(N3*(1-0.1979),2)")


def test_decode_bytes_variants():
    assert decode_bytes("prix 3,10 €".encode("utf-8")) == ("prix 3,10 €", "utf-8")
    assert decode_bytes(b"\xef\xbb\xbfabc")[1] == "utf-8-sig"
    text, enc = decode_bytes("Prezzo al pubblico €".encode("cp1252"))
    assert enc == "cp1252" and text.endswith("€")
    with pytest.raises(EncodingError):
        decode_bytes(b"\x00\x01\x02binary")


def test_sniff_delimiter_validates_expectation():
    assert sniff_delimiter("a;b;c\n1;2;3\n4;5;6") == ";"
    assert sniff_delimiter("a\tb\n1\t2") == "\t"
    with pytest.raises(ParserError):
        sniff_delimiter("a;b;c\n1;2;3", expected="\t")


def test_dates_are_explicit():
    assert parse_date("01/07/2026", ["%m/%d/%Y"]) == date(2026, 1, 7)
    assert parse_date("01/07/2026", ["%d/%m/%Y"]) == date(2026, 7, 1)
    assert parse_date("22-MAR-2025", ["%d-%b-%Y"]) == date(2025, 3, 22)
    with pytest.raises(ParserError):
        parse_date("2026/13/45", ["%Y/%m/%d"])


def test_headers():
    assert slug("Prezzo al pubblico €") == "prezzo_al_pubblico"
    assert slug("Código Nacional") == "codigo_nacional"
    assert slug("薬価基準収載医薬品コード") == "薬価基準収載医薬品コード"  # dakuten preserved
    assert dedupe_headers(["A", None, "A", ""]) == ["a", "col_1", "a_1", "col_3"]


# ------------------------------------------------------------------ normalization
@pytest.mark.parametrize(
    "raw,label",
    [("500 mg", "500 mg"), ("0.5 g", "500 mg"), ("500,0 mg", "500 mg"), ("5 mg/5 ml", "1 mg/ml"), ("100 µg", "0.1 mg"),
     ("1 000 UI", "1000 IU"), ("１ｍｇ１錠", "1 mg"), ("10 MG/G CREM DERM", "10 mg/g"), ("240 microgrammes", "0.24 mg")],
)
def test_strength(raw, label):  # noqa: ANN001
    assert parse_strength(raw).label == label


@pytest.mark.parametrize(
    "raw,qty,unit",
    [("28 tablets", 28, "tablet"), ("28 comprimés", 28, "tablet"), ("28 tbl", 28, "tablet"),
     ("2 plaquette(s) PVC aluminium de 14 comprimé(s)", 28, "tablet"), ("CAMPRAL*84 cpr", 84, "tablet"),
     ("DEPAKINE 500 mg comprimidos gastrorresistentes, 20 comprimidos", 20, "tablet"), ("CT BG AL X 40 G", 40, "g"),
     ("250 MG PO LIOF SOL INJ CT 1 FA + SER", 1, "vial"), ("soluzione flacone 240 ml 20 mg/ml", 240, "ml")],
)
def test_pack(raw, qty, unit):  # noqa: ANN001
    p = parse_pack(raw)
    assert (p.quantity, p.unit) == (Decimal(qty), unit)


@pytest.mark.parametrize(
    "raw,qty",
    [
        ("2 bolsas de 5.000 ml", "10000"),
        ("30 envases unidosis de 0,3 ml", "9"),
    ],
)
def test_spanish_pack_quantities(raw, qty):  # noqa: ANN001
    pack = parse_pack(raw, decimal_sep=",")
    assert pack is not None
    assert (pack.quantity, pack.unit) == (Decimal(qty), "ml")


def test_forms_for_pack_units():
    assert normalize_form("comprimé pelliculé") == "tablet"
    assert normalize_form("comprimidos gastrorresistentes") == "gastro-resistant tablet"
    assert normalize_form("ユーロジン１ｍｇ錠") == "tablet"
    assert normalize_form("solution injectable") == "injection"


def test_extract_brand():
    assert extract_brand("DUPIXENT 300 mg, solution injectable – 2 stylos") == "DUPIXENT"
    assert extract_brand("DUPIXENT*2 penne preriempite SC 300 mg 2 ml") == "DUPIXENT"
    assert extract_brand("PARACETAMOL BIOGARAN 1 g, comprimé") == "PARACETAMOL BIOGARAN"
    assert extract_brand(None) is None and extract_brand("500 mg") is None


def test_japanese_era_dates():
    from app.adapters.jp_nhi import _era_date

    assert _era_date("R9.3.31まで") == date(2027, 3, 31)
    assert _era_date("令和8年3月31日") == date(2026, 3, 31)
    assert _era_date("備考") is None


# ------------------------------------------------------------------ quality, pricing, schema
def _record(amount: Decimal, **fact_kw) -> NormalizedRecord:  # noqa: ANN003
    fact = PriceFact(amount=amount, currency=fact_kw.pop("currency", "EUR"), price_type=PriceBasis.RETAIL,
                     price_status=PriceStatus.REGULATED, price_unit="pack", **fact_kw)
    return NormalizedRecord("r1", SourceProductData("123", "X"), [fact])


def test_quality_rules_produce_warnings():
    issues = check_record(_record(Decimal(0), currency="eur", valid_from=date(2026, 2, 1), valid_to=date(2026, 1, 1)))
    rules = {i.details["rule"] for i in issues}
    assert rules == {"price_positive", "currency_exists", "effective_period_valid"}
    assert all(i.error_type == ErrorType.VALIDATION_ERROR and i.severity == "warning" for i in issues)
    rec = _record(Decimal(100))
    assert variation_issue(rec, rec.prices[0], Decimal(5)) is not None
    assert variation_issue(rec, rec.prices[0], Decimal(50)) is None


def test_price_normalization_keeps_original():
    fact = PriceFact(Decimal("8.42"), "EUR", PriceBasis.RETAIL, PriceStatus.REGULATED, "pack", pack_quantity=Decimal(28), pack_unit="tablet")
    assert normalize_price(fact) == (Decimal("0.30071429"), "tablet")
    assert fact.amount == Decimal("8.42")
    per_unit = PriceFact(Decimal("0.26341"), "USD", PriceBasis.WHOLESALE, PriceStatus.OBSERVED, "EA")
    assert normalize_price(per_unit) == (Decimal("0.26341000"), "unit")
    no_pack = PriceFact(Decimal("5"), "EUR", PriceBasis.RETAIL, PriceStatus.REGULATED, "pack")
    assert normalize_price(no_pack) == (None, None)


def test_baseline_comparison():
    profile = TableProfile("main", ["a", "b", "c"], {"a": "integer", "b": "decimal", "c": "text"}, 100)
    assert compare_with_baseline(profile, {"columns": ["a", "b", "c"], "column_types": {"b": "decimal"}, "row_count": 90}, truncated=False) == []
    issues = compare_with_baseline(profile, {"columns": ["a", "b"], "column_types": {"b": "text"}, "row_count": 10}, truncated=False)
    messages = " ".join(i.message for i in issues)
    assert "added ['c']" in messages and "Type of column 'b'" in messages and "Row count changed" in messages
    assert not any("Row count" in i.message for i in compare_with_baseline(profile, {"columns": ["a", "b", "c"], "row_count": 10}, truncated=True))


# ------------------------------------------------------------------ registry & snapshots
def test_progress_messages(caplog):  # noqa: ANN001
    from app.observability import Progress

    p = Progress("publish", total=200, unit="records", interval=0, check_every=1)
    with caplog.at_level("INFO", logger="app.progress"):
        for _ in range(100):
            p.update(matched=1)
        p.finish()
    assert "publish: 100/200 records (50.0%)" in caplog.text and "ETA" in caplog.text and "matched=1" in caplog.text


def test_registry_is_complete(settings):  # noqa: ANN001
    from app.adapters import adapter_ids

    reg = load_registry(settings.sources_file)
    assert set(reg) == set(adapter_ids())
    assert reg["uk_dmd"].country == "GB" and reg["ca_on_odb"].coverage_scope == "province:ON"
    assert reg["ca_bc_pharmacare"].coverage_scope == "province:BC"
    assert reg["br_cmed"].options["icms_rates"] == ["0%", "18 %"]


def test_registry_rejects_incomplete_entry(tmp_path: Path):
    p = tmp_path / "s.yaml"
    p.write_text("sources:\n  x:\n    country: FR\n    name: x\n")
    with pytest.raises(RegistryError, match="missing fields"):
        load_registry(p)


def test_snapshots_never_overwrite(tmp_path: Path):
    from app.core.snapshots import DownloadedFile, SnapshotStore
    from app.core.types import ArtifactFile

    store = SnapshotStore(tmp_path)
    dirs = []
    for content in (b"v1", b"v1"):
        tmp = tmp_path / "dl.bin"
        tmp.write_bytes(content)
        art = SourceArtifact("s", [ArtifactFile("https://x/f.csv", "f.csv")])
        prev = dirs[-1] if dirs else None
        snap = store.save("s", art, [DownloadedFile(art.files[0], tmp, "https://x/f.csv", "text/csv")], parser_version="1",
                          previous_id=prev.id if prev else None, previous_hashes=[f.sha256 for f in prev.files] if prev else None)
        dirs.append(snap)
    assert dirs[0].directory != dirs[1].directory
    assert dirs[1].duplicate_of == dirs[0].id
    assert store.load(dirs[0].directory).files[0].sha256 == dirs[0].files[0].sha256
    dirs[0].files[0].path.chmod(0o644)
    dirs[0].files[0].path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="sha256"):
        store.load(dirs[0].directory)


# ------------------------------------------------------------------ FX
def test_fx_conversion_uses_cross_rates(session_factory):  # noqa: ANN001
    from app.fx.service import convert, parse_ecb_xml, store_rates

    xml = b"""<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <Cube><Cube time="2026-09-30"><Cube currency="USD" rate="1.1000"/><Cube currency="JPY" rate="165.00"/></Cube></Cube></gesmes:Envelope>"""
    with session_factory() as s:
        assert store_rates(s, parse_ecb_xml(xml)) == 2
        c = convert(s, Decimal("110"), "USD", "EUR", date(2026, 10, 1))
        assert c.converted_price == Decimal("100.000000") and c.fx_date == date(2026, 9, 30)
        c = convert(s, Decimal("1.1"), "USD", "JPY", date(2026, 10, 1))
        assert c.converted_price == Decimal("165.000000")
        assert convert(s, Decimal(1), "USD", "EUR", date(2027, 1, 1)) is None  # stale rate refused
        assert convert(s, Decimal(1), "BRL", "EUR", date(2026, 10, 1)) is None


def test_migrations_upgrade(tmp_path: Path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    root = Path(__file__).parent.parent
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"source", "job", "job_error", "raw_snapshot", "product", "price", "fx_rate", "schema_baseline"} <= tables
    assert not {"canonical_product", "product_mapping", "presentation", "source_license"} & tables


def test_migration_unifies_legacy_price_types(tmp_path: Path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from app.core.publisher import observation_key, series_key

    root = Path(__file__).parent.parent
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0001")
    legacy = [  # source, type, variant, status
        ("us_nadac", "acquisition_cost", "default", "observed"),
        ("es_nomenclator", "reimbursement_ceiling", "precio_de_referencia", "reference"),
        ("de_gba", "other", "net_after_statutory_rebates", "reference"),
        ("br_cmed", "maximum_government_price", "pmvg_icms_18", "calculated"),
    ]
    engine = create_engine(url)
    with engine.begin() as conn:
        for i, (src, ptype, variant, status) in enumerate(legacy, 1):
            conn.execute(text(
                "INSERT INTO price (id, product_id, source_id, series_key, observation_key, price_type, price_variant, "
                "price_status, amount, currency, price_unit, valid_from, vat_included, retrieved_at, coverage_scope, "
                "snapshot_id, quality_flags, is_current) VALUES (:i, :i, :src, 'x', :ok, :t, :v, :s, 12.50, 'EUR', 'pack', "
                "'2026-01-01', 1, '2026-01-01 00:00:00', 'national', 'snap', '[]', 1)"
            ), {"i": i, "src": src, "ok": f"k{i}", "t": ptype, "v": variant, "s": status})
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT price_type, price_variant, price_status, series_key, observation_key FROM price ORDER BY id")).all()
    assert [tuple(r[:3]) for r in rows] == [
        ("wholesale", "nadac", "observed"),
        ("reimbursement", "precio_de_referencia", "ceiling"),
        ("retail", "net_after_statutory_rebates", "reference"),
        ("public_procurement", "pmvg_icms_18", "calculated"),
    ]
    for i, (ptype, variant, status, skey, okey) in enumerate(rows, 1):
        fact = PriceFact(Decimal("12.5"), "EUR", PriceBasis(ptype), PriceStatus(status), "pack", price_variant=variant,
                         valid_from=date(2026, 1, 1), vat_included=True)
        assert skey == series_key(fact) and okey == observation_key(i, fact)
    command.downgrade(cfg, "0001")
    with engine.connect() as conn:
        back = conn.execute(text("SELECT source_id, price_type, price_variant, price_status FROM price ORDER BY id")).all()
    assert [tuple(r) for r in back] == legacy
