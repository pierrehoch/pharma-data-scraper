"""Offline test harness: fake HTTP client serving fixtures, temp DB/storage, pipeline factory."""

from __future__ import annotations

import json
import io
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from app.config import Settings, set_settings
from app.core.errors import DownloadError
from app.core.http import DownloadResult, TextResponse
from app.db.models import Base
from app.db.session import dispose_engines, get_engine, make_session_factory
from app.registry import load_registry

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = Path(__file__).parent.parent


@dataclass
class Route:
    pattern: str
    path: Path | Callable[[], bytes]
    headers: dict[str, str] = field(default_factory=dict)


class FakeHttp:
    """Maps URL regexes to fixture files. Unknown URLs raise DownloadError (tests never hit the network)."""

    def __init__(self, routes: list[Route]):
        self.routes = list(routes)
        self.requested: list[str] = []

    def override(self, pattern: str, content: bytes | Path, headers: dict[str, str] | None = None) -> None:
        self.routes.insert(0, Route(pattern, (lambda c=content: c) if isinstance(content, bytes) else content, headers or {}))

    def _find(self, url: str) -> Route:
        self.requested.append(url)
        for r in self.routes:
            if re.search(r.pattern, url):
                return r
        raise DownloadError(f"[test] no fixture for URL {url}")

    def _bytes(self, r: Route) -> bytes:
        return r.path() if callable(r.path) else r.path.read_bytes()

    def get_text(self, url: str, *, headers=None) -> TextResponse:  # noqa: ANN001
        r = self._find(url)
        return TextResponse(url=url, text=self._bytes(r).decode("utf-8"), status_code=200, headers=r.headers)

    def get_json(self, url: str, *, headers=None):  # noqa: ANN001, ANN201
        return json.loads(self.get_text(url).text)

    def download(self, url: str, dest: Path, *, headers=None) -> DownloadResult:  # noqa: ANN001
        if url.startswith("file://"):
            from urllib.parse import unquote, urlparse

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(unquote(urlparse(url).path)), dest)
            return DownloadResult(path=dest, final_url=url, content_type=None)
        r = self._find(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._bytes(r))
        return DownloadResult(path=dest, final_url=url, content_type=r.headers.get("content-type"), headers={k.lower(): v for k, v in r.headers.items()})


def fx(source: str, name: str) -> Path:
    return FIXTURES / source / name


def bc_fixture_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("pddf_2026_10_06.csv", fx("ca_bc_pharmacare", "pddf_2026_10_06.csv").read_bytes())
    return output.getvalue()


def default_routes() -> list[Route]:
    return [
        # France
        Route(r"base-donnees-publique\.medicaments\.gouv\.fr/telechargement$", fx("fr_bdpm", "telechargement.html")),
        Route(r"/download/file/CIS_bdpm\.txt$", fx("fr_bdpm", "CIS_bdpm.txt")),
        Route(r"/download/file/CIS_CIP_bdpm\.txt$", fx("fr_bdpm", "CIS_CIP_bdpm.txt")),
        Route(r"/download/file/CIS_COMPO_bdpm\.txt$", fx("fr_bdpm", "CIS_COMPO_bdpm.txt")),
        # US
        Route(r"data\.medicaid\.gov/api/1/search", fx("us_nadac", "search.json")),
        Route(r"data\.medicaid\.gov/api/1/datastore/query", fx("us_nadac", "sample.json")),
        Route(r"download\.medicaid\.gov/data/nadac-national-average-drug-acquisition-cost-.*\.csv$", fx("us_nadac", "nadac.csv")),
        Route(r"cms\.gov/medicare/payment/part-b-drugs/asp-pricing-files$", fx("us_asp", "page.html")),
        Route(r"payment-limit-files.*\.zip$", fx("us_asp", "payment_limit.zip")),
        Route(r"crosswalk.*\.zip$", fx("us_asp", "crosswalk.zip")),
        # Italy
        Route(r"aifa\.gov\.it/liste-farmaci-a-h$", fx("it_aifa", "page.html")),
        Route(r"Classe_A_per_principio_attivo_31-05-2026\.csv$", fx("it_aifa", "class_a.csv")),
        Route(r"Classe_H_per_principio_attivo_31-05-2026\.csv$", fx("it_aifa", "class_h.csv")),
        # Spain
        Route(r"acuerdosNotasInformativas/home\.htm$", fx("es_cipm", "page.html")),
        Route(r"/Acuerdos_CIPM_273\.pdf$", fx("es_cipm", "Acuerdos_CIPM_273.pdf")),
        Route(r"/ACUERDOS_CIPM_250\.pdf$", fx("es_cipm", "ACUERDOS_CIPM_250.pdf")),
        Route(r"/ACUERDOS_DE_LA_CIPM_177_web\.pdf$", fx("es_cipm", "ACUERDOS_DE_LA_CIPM_177_web.pdf")),
        Route(r"/Nota_informativa_CIPM_Octubre_2026\.pdf$", fx("es_cipm", "Nota_informativa_CIPM_Octubre_2026.pdf")),
        Route(r"cima\.aemps\.es/cima/rest/medicamentos\?", fx("es_cima", "search.json")),
        Route(r"cima\.aemps\.es/cima/rest/medicamento\?", fx("es_cima", "medicine.json")),
        Route(r"nomenclator\.do\?metodo=buscarProductos", fx("es_nomenclator", "nomenclator.csv"),
              {"content-type": "text/csv; charset=UTF-8", "content-disposition": 'attachment; filename="20261001_Nomenclator_de_Facturacion.csv"'}),
        Route(r"sanidad\.gob\.es/profesionales/nomenclator\.do$", fx("es_nomenclator", "page.html")),
        # UK
        Route(r"isd\.digital\.nhs\.uk/trud/api/v1/keys/.*/items/24/releases", fx("uk_dmd", "trud_releases.json")),
        Route(r"isd\.digital\.nhs\.uk/download/.*\.zip$", fx("uk_dmd", "dmd_release.zip")),
        Route(r"drug-tariff-part-viii$", fx("uk_drug_tariff", "page.html")),
        Route(r"Part%20VIIIA%20Oct%202026\.csv$", fx("uk_drug_tariff", "part_viiia.csv")),
        # Japan
        Route(r"mhlw\.go\.jp/stf/seisakunitsuite/bunya/0000078916\.html$", fx("jp_nhi", "index.html")),
        Route(r"mhlw\.go\.jp/topics/2026/04/tp20260401-01\.html$", fx("jp_nhi", "list.html")),
        Route(r"tp20260813-01_01\.xlsx$", fx("jp_nhi", "tp20260813-01_01.xlsx")),
        Route(r"tp20260813-01_02\.xlsx$", fx("jp_nhi", "tp20260813-01_02.xlsx")),
        Route(r"tp20260813-01_03\.xlsx$", fx("jp_nhi", "tp20260813-01_03.xlsx")),
        Route(r"tp20260401-01_04\.xlsx$", fx("jp_nhi", "tp20260401-01_04.xlsx")),
        # Brazil
        Route(r"anvisa/pt-br/assuntos/medicamentos/cmed/precos$", fx("br_cmed", "page.html")),
        Route(r"lista_pmc_20260923", fx("br_cmed", "lista_pmc.xlsx")),
        Route(r"lista_pmvg_20260923", fx("br_cmed", "lista_pmvg.xlsx")),
        # Germany
        Route(r"g-ba\.de/bewertungsverfahren/nutzenbewertung/$", fx("de_gba", "catalogue_1.html")),
        Route(r"g-ba\.de/bewertungsverfahren/nutzenbewertung/\?sort=beginn&direction=desc&seite=2$", fx("de_gba", "catalogue_2.html")),
        Route(r"g-ba\.de/bewertungsverfahren/nutzenbewertung/1104/$", fx("de_gba", "dupilumab.html")),
        Route(r"g-ba\.de/bewertungsverfahren/nutzenbewertung/1200/$", fx("de_gba", "lisocabtagen.html")),
        Route(r"g-ba\.de/downloads/.*/2025-02-06_AM-RL-XII_Dupilumab_D-1086_TrG\.pdf$", fx("de_gba", "2025-02-06_AM-RL-XII_Dupilumab_D-1086_TrG.pdf")),
        Route(r"g-ba\.de/downloads/.*/2025-10-02_AM-RL-XII_Lisocabtagen-maraleucel_D-1196_TrG\.pdf$", fx("de_gba", "2025-10-02_AM-RL-XII_Lisocabtagen-maraleucel_D-1196_TrG.pdf")),
        Route(r"bfarm\.de/DE/Arzneimittel/.*/Festbetraege/_node\.html$", fx("de_festbetrag", "page.html")),
        Route(r"festbetraege-20260915\.pdf", fx("de_festbetrag", "festbetraege-20260915.pdf")),
        Route(r"ebv_nach_130b\.jsp\?.*pageNo=2", fx("de_amnog", "page_2.html")),
        Route(r"ebv_nach_130b\.jsp", fx("de_amnog", "page_1.html")),
        # Canada
        Route(r"downloadable-drug-data-files$", fx("ca_bc_pharmacare", "page.html")),
        Route(r"pharmacare/outgoing/pddf\.zip$", bc_fixture_zip),
        Route(r"ontario\.ca/document/ontario-drug-benefit-odb-formulary-comparative-drug-index-cdi-and-monthly-formulary-0$", fx("ca_on_odb", "page.html")),
        Route(r"odb-data-extract\.xml$", fx("ca_on_odb", "data_extract.xml")),
        Route(r"what-data-extract-drug-product-database\.html$", fx("ca_hc_dpd", "readme.html")),
        Route(r"drug-product-database/allfiles\.zip$", fx("ca_hc_dpd", "allfiles.zip")),
        Route(r"data\.ontario\.ca/api/3/action/package_show", fx("ca_on_odb", "ckan_package.json")),
        Route(r"data_extract\.xml$", fx("ca_on_odb", "data_extract.xml")),
        Route(r"nhsa\.gov\.cn/module/download/downfile\.jsp", fx("cn_vbp", "gy-yd2024-2-proposed.pdf")),
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        raw_storage_dir=tmp_path / "raw",
        manual_import_dir=tmp_path / "manual",
        sources_file=PROJECT / "app" / "registry" / "sources.yaml",
        http_delay_seconds=0,
        log_format="text",
        trud_api_key="test-trud-key",
        publish_batch_size=3,
    )
    set_settings(s)
    yield s
    set_settings(None)
    dispose_engines()


@pytest.fixture
def session_factory(settings: Settings):
    Base.metadata.create_all(get_engine(settings.database_url))
    return make_session_factory(settings.database_url)


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp(default_routes())


@pytest.fixture
def registry(settings: Settings):
    reg = load_registry(settings.sources_file)
    reg["de_amnog"].options["request_delay_seconds"] = 0
    return reg


@pytest.fixture
def pipeline(settings, session_factory, http, registry):  # noqa: ANN001, ANN201
    from app.core.pipeline import Pipeline
    from app.core.snapshots import SnapshotStore

    return Pipeline(settings, session_factory, http=http, store=SnapshotStore(settings.raw_storage_dir), registry=registry)
