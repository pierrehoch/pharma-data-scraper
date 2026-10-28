import json
from datetime import date

import pytest

from app.core.errors import DiscoveryError
from tests.conftest import fx
from tests.test_adapters import make_adapter, run_adapter


def test_explicit_pdf_decision_date_overrides_malformed_future_filename(caplog):
    from app.adapters.de_gba import _decision_date
    from app.core.errors import SchemaChangedError

    assert _decision_date("Tragende Gruende\nVom 19. Dezember 2019\nInhalt", date(2919, 12, 19)) == date(2019, 12, 19)
    assert "corrected to explicit PDF decision date" in caplog.text
    with pytest.raises(SchemaChangedError, match="future decision date"):
        _decision_date("No explicit decision date", date(2919, 12, 19))


def test_full_catalogue_is_default_and_preserves_discovery_manifest(run_adapter, http):
    _, artifact, snapshot, dataset, normalized, _ = run_adapter("de_gba")
    assert "2 procedures" in artifact.edition
    assert len(artifact.files) == 2
    assert any("seite=2" in url for url in http.requested)
    report = json.loads(snapshot.file("manifest").path.read_text())
    assert report["scope"] == "full_catalogue"
    assert len(report["procedures"]) == 2
    assert report["failures"] == []
    assert dataset.context["documents_parsed"] == 2
    assert dataset.context["documents_excluded"] == []
    assert len(normalized.records) == 20


@pytest.mark.parametrize("content,message", [
    ('<html>No catalogue</html>', "missing result count"),
    ('<div class="gba-entity-list__results-counter">1 Ergebnis</div>', "no procedure rows"),
])
def test_catalogue_layout_changes_fail_explicitly(settings, http, registry, content, message):
    http.override(r"nutzenbewertung/$", content.encode())
    adapter = make_adapter("de_gba", settings, http, registry)
    with pytest.raises(DiscoveryError, match=message):
        adapter.discover()


def test_repeated_catalogue_page_does_not_silently_truncate(settings, http, registry):
    http.override(r"seite=2$", fx("de_gba", "catalogue_1.html"))
    with pytest.raises(DiscoveryError, match="repeats"):
        make_adapter("de_gba", settings, http, registry).discover()


def test_catalogue_expected_count_must_match(settings, http, registry):
    content = fx("de_gba", "catalogue_1.html").read_text().replace("2 Ergebnisse", "3 Ergebnisse")
    http.override(r"nutzenbewertung/$", content.encode())
    with pytest.raises(DiscoveryError, match="expected 3 procedures, discovered 2"):
        make_adapter("de_gba", settings, http, registry).discover()


def test_targeted_mode_does_not_request_catalogue(settings, http, registry):
    adapter = make_adapter("de_gba", settings, http, registry)
    adapter.config.options["procedure_urls"] = ["https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/1104/"]
    artifact = adapter.discover()
    assert len(artifact.files) == 1
    assert artifact.partial
    assert http.requested == ["https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/1104/"]


def test_targeted_import_preserves_full_catalogue_row_baseline(pipeline, session_factory):
    from sqlalchemy import select
    from app.core.enums import JobStatus
    from app.core.pipeline import RunOptions, Selection
    from app.db.models import SchemaBaseline

    pipeline.run(Selection(source_ids=["de_gba"]), RunOptions())
    pipeline.registry["de_gba"].options["procedure_urls"] = ["https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/1104/"]
    result = pipeline.run(Selection(source_ids=["de_gba"]), RunOptions())
    assert result.jobs[0].status == JobStatus.PARTIAL_SUCCESS
    with session_factory() as session:
        baseline = session.scalar(select(SchemaBaseline).where(SchemaBaseline.source_id == "de_gba"))
        assert baseline.row_count == 20


def test_clinic_integer_thousands_and_nonzero_vat(settings, http, registry):
    from decimal import Decimal

    filename = "2025-04-17_AM-RL-XII_Artesunat_D-1135_TrG.pdf"
    http.override(r"nutzenbewertung/1104/$",
                  f'<a href="/downloads/40-268-11409/{filename}">Tragende Gruende</a>'.encode())
    http.override(rf"{filename}$", fx("de_gba", filename))
    adapter = make_adapter("de_gba", settings, http, registry)
    adapter.config.options["procedure_urls"] = ["https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/1104/"]
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert dataset.records[0].data["prices"] == [Decimal("3200"), Decimal("608"), Decimal("3808")]
    normalized = adapter.normalize(dataset)
    assert normalized.failed_rows == 0
    price = normalized.records[0].prices[0]
    assert price.amount == Decimal("3808") and price.vat_included is True
    assert "before VAT 3200 EUR" in price.notes


def test_catalogue_has_no_fifty_procedure_cap(settings, http, registry):
    rows = "".join(
        f'<tr><td><a href="/bewertungsverfahren/nutzenbewertung/{index}/">Drug {index}</a></td></tr>'
        for index in range(1, 52)
    )
    html = f'<div class="gba-entity-list__results-counter">51 Ergebnisse</div><table id="liste"><tbody>{rows}</tbody></table>'
    http.override(r"nutzenbewertung/\d+/$", fx("de_gba", "dupilumab.html"))
    http.override(r"nutzenbewertung/$", html.encode())
    artifact = make_adapter("de_gba", settings, http, registry).discover()
    assert "51 procedures" in artifact.edition
    assert len(artifact.files) == 1  # identical document linked by multiple procedures
    assert len(http.requested) == 52


def test_catalogue_procedures_without_decisions_are_audited(settings, http, registry):
    http.override(r"nutzenbewertung/1200/$", b"<html>Verfahren begonnen</html>")
    adapter = make_adapter("de_gba", settings, http, registry)
    artifact = adapter.discover()
    assert len(artifact.files) == 1
    assert "1 procedures without a justification" in artifact.notes[0]
    snapshot = adapter.download(artifact)
    report = json.loads(snapshot.file("manifest").path.read_text())
    assert report["procedures"][1]["documents"] == []
    assert adapter.parse(snapshot).records


def test_malformed_official_decision_date_is_reported_not_guessed(settings, http, registry):
    html = fx("de_gba", "dupilumab.html").read_text() + (
        '<a href="/downloads/40-268-1908/2012-29-03_AM-RL-XII_Fingolimod_TrG.pdf">Tragende Gruende</a>'
    )
    http.override(r"nutzenbewertung/1104/$", html.encode())
    adapter = make_adapter("de_gba", settings, http, registry)
    artifact = adapter.discover()
    assert len(artifact.files) == 2
    dataset = adapter.parse(adapter.download(artifact))
    assert dataset.records
    assert any("Unusable decision date" in issue.message for issue in dataset.issues)


def test_download_failure_does_not_discard_other_documents(settings, http, registry):
    http.routes = [r for r in http.routes if "Lisocabtagen-maraleucel_D-1196" not in r.pattern]
    adapter = make_adapter("de_gba", settings, http, registry)
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert dataset.records
    assert any("download failed" in issue.message for issue in dataset.issues)
    report = json.loads(dataset.snapshot.file("manifest").path.read_text())
    assert len(report["failures"]) == 1 and report["failures"][0]["stage"] == "download"


def test_discovery_failure_is_persisted(settings, http, registry):
    http.routes = [r for r in http.routes if "/1200/" not in r.pattern]
    adapter = make_adapter("de_gba", settings, http, registry)
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert dataset.context["documents_parsed"] == 1
    assert any("discovery failed" in issue.message for issue in dataset.issues)


def test_corrupt_pdf_is_reported_without_discarding_other_prices(settings, http, registry):
    http.override(r"Lisocabtagen-maraleucel_D-1196_TrG\.pdf$", b"not a PDF")
    adapter = make_adapter("de_gba", settings, http, registry)
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert dataset.records
    assert dataset.context["documents_parsed"] == 1
    assert len(dataset.context["documents_excluded"]) == 1
    assert "Lisocabtagen" in dataset.context["documents_excluded"][0]["filename"]
    assert any("excluded" in issue.message for issue in dataset.issues)


def test_conflicting_document_is_rejected_atomically(settings, http, registry, monkeypatch):
    from app.adapters import de_gba

    original = de_gba._pack_rows

    def conflicting_rows(pdf, *, english=False):
        rows = original(pdf, english=english)
        if rows[0]["ingredient"] == "Dupilumab":
            conflict = {**rows[0], "prices": [value + 1 for value in rows[0]["prices"]]}
            rows.append(conflict)
        return rows

    monkeypatch.setattr(de_gba, "_pack_rows", conflicting_rows)
    adapter = make_adapter("de_gba", settings, http, registry)
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert dataset.context["documents_parsed"] == 1
    assert "conflicting disclosures" in dataset.context["documents_excluded"][0]["reason"]
    assert not any("Dupilumab" in record.data["document_url"] for record in dataset.records)


def test_all_documents_invalid_is_not_success(settings, http, registry):
    from app.core.errors import ParserError

    http.override(r"_TrG\.pdf$", b"not a PDF")
    adapter = make_adapter("de_gba", settings, http, registry)
    with pytest.raises(ParserError, match="no justification pack-price records"):
        adapter.parse(adapter.download(adapter.discover()))


@pytest.mark.parametrize("filename,expected_rows", [
    ("2020-04-02_AM-RL-XII_Niraparib_D-496_TrG.pdf", 3),
    ("2022-08-04_AM-RL-XII_Ivacaftor_D-793_TrG.pdf", 4),
    ("2026-06-18_AM-RL-XII_Nipocalimab_D-1282_TrG.pdf", 7),
])
def test_historical_layouts_have_exact_pack_values(settings, http, registry, filename, expected_rows):
    from decimal import Decimal

    http.override(r"nutzenbewertung/1104/$",
                  f'<a href="/downloads/40-268-11185/{filename}">Tragende Gruende</a>'.encode())
    http.override(rf"{filename}$", fx("de_gba", filename))
    adapter = make_adapter("de_gba", settings, http, registry)
    adapter.config.options["procedure_urls"] = ["https://www.g-ba.de/bewertungsverfahren/nutzenbewertung/1104/"]
    dataset = adapter.parse(adapter.download(adapter.discover()))
    normalized = adapter.normalize(dataset)
    assert dataset.context["documents_excluded"] == []
    assert len(normalized.records) == expected_rows and normalized.failed_rows == 0
    if "Niraparib" in filename:
        product = normalized.records[0]
        assert product.product.ingredient == "Niraparib"
        assert product.product.pack_text == "84 HKP"
        assert [p.amount for p in product.prices] == [Decimal("8214.71"), Decimal("7744.38")]
    if "Ivacaftor" in filename:
        products = [r.product for r in normalized.records]
        assert products[0].ingredient == products[1].ingredient == "Ivacaftor"
        assert products[0].strength_text == "75 mg" and products[1].strength_text == "150 mg"
        assert products[2].ingredient == products[3].ingredient == "Ivacaftor / Tezacaftor / Elexacaftor"
        assert products[2].strength_text == "37,5 mg / 25 mg / 50 mg"


@pytest.mark.parametrize("filename", [
    "2012-03-29_AM-RL-XII_Linagliptin_TrG.pdf",
    "2015-04-16_AM-RL-XII_Lurasidon_2014-11-01-D-142_TrG.pdf",
])
def test_historical_narrative_only_justifications_are_excluded(settings, http, registry, filename):
    http.override(r"nutzenbewertung/1200/$",
                  f'<a href="/downloads/40-268-11937/{filename}">Tragende Gruende</a>'.encode())
    http.override(rf"{filename}$", fx("de_gba", filename))
    adapter = make_adapter("de_gba", settings, http, registry)
    dataset = adapter.parse(adapter.download(adapter.discover()))
    assert dataset.context["documents_parsed"] == 1
    assert len(dataset.context["documents_excluded"]) == 1
    assert dataset.context["documents_excluded"][0]["filename"] == filename
    assert all("Dupilumab" in record.data["document_url"] for record in dataset.records)
