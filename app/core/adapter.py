"""The SourceAdapter contract exposed to the pipeline. No source-specific logic lives in the core."""

from __future__ import annotations

import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from app.config import Settings
from app.core.enums import AdapterStatus, ErrorType, Severity
from app.core.http import HttpClientProtocol
from app.core.quality import validate_dataset
from app.core.schema import ExpectedSchema
from app.core.snapshots import DownloadedFile, SnapshotStore
from app.core.types import (
    Issue,
    NormalizedDataset,
    NormalizedRecord,
    ParsedDataset,
    ParsedRecord,
    RawSnapshot,
    RunHints,
    SourceArtifact,
    ValidationReport,
)
from app.observability import Progress
from app.registry import SourceConfig


@dataclass
class AdapterContext:
    settings: Settings
    http: HttpClientProtocol
    store: SnapshotStore
    hints: RunHints = field(default_factory=RunHints)
    previous_snapshot_id: str | None = None
    previous_snapshot_hashes: list[str] | None = None


class SourceAdapter(ABC):
    """Common contract: discover → download → parse → normalize → validate.

    Adapters are free to implement each step as they need (multi-file joins, pagination,
    PDF text extraction...) as long as they return the shared data contracts.
    """

    adapter_id: ClassVar[str]
    parser_type: ClassVar[str]
    parser_version: ClassVar[str] = "1.0"
    status: ClassVar[AdapterStatus] = AdapterStatus.SUPPORTED
    limitations: ClassVar[str] = ""

    def __init__(self, config: SourceConfig, ctx: AdapterContext):
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------ helpers

    @property
    def source_id(self) -> str:
        return self.config.source_id

    @property
    def hints(self) -> RunHints:
        return self.ctx.hints

    def option(self, name: str, default=None):  # noqa: ANN001, ANN201
        return self.config.options.get(name, default)

    def issue(
        self,
        error_type: ErrorType,
        message: str,
        *,
        severity: Severity = Severity.WARNING,
        stage: str | None = None,
        row_ref: str | None = None,
        **details: object,
    ) -> Issue:
        return Issue(error_type, severity, message, stage=stage, row_ref=row_ref, details=dict(details))

    # ------------------------------------------------------------------ contract

    def check_configuration(self) -> list[Issue]:
        """Return MISSING_CONFIGURATION issues (e.g. absent credentials). Default: nothing required."""
        return []

    @abstractmethod
    def discover(self) -> SourceArtifact:
        """Find the current edition (exact URLs + publication date) without hardcoding dated file names."""

    def download(self, artifact: SourceArtifact) -> RawSnapshot:
        staging = self.ctx.store.staging_dir(self.source_id)
        downloaded: list[DownloadedFile] = []
        for i, f in enumerate(artifact.files):
            result = self.ctx.http.download(f.url, staging / f"{i:03d}_{f.filename}")
            header_name = getattr(result, "filename_from_headers", None)
            downloaded.append(
                DownloadedFile(
                    artifact_file=f,
                    temp_path=result.path,
                    final_url=result.final_url,
                    content_type=result.content_type,
                    filename=header_name or f.filename,
                )
            )
        return self.ctx.store.save(
            self.source_id,
            artifact,
            downloaded,
            parser_version=self.parser_version,
            previous_id=self.ctx.previous_snapshot_id,
            previous_hashes=self.ctx.previous_snapshot_hashes,
        )

    @abstractmethod
    def parse(self, snapshot: RawSnapshot) -> ParsedDataset:
        """Read raw files into ParsedRecords + TableProfiles. Raise ParserError/EncodingError on structural problems."""

    @abstractmethod
    def expected_schemas(self) -> list[ExpectedSchema]:
        """Declared structure for schema change detection (one entry per parsed file role)."""

    def record_matches_product_ids(self, record: ParsedRecord, product_ids: set[str]) -> bool:
        return record.key is not None and record.key in product_ids

    @abstractmethod
    def normalize_record(self, record: ParsedRecord, dataset: ParsedDataset) -> NormalizedRecord | None:
        """Map one parsed record to the generic product/price contracts. Return None to skip."""

    def normalize(self, dataset: ParsedDataset) -> NormalizedDataset:
        out: list[NormalizedRecord] = []
        failed = 0
        issues: list[Issue] = []
        progress = Progress("normalize", total=len(dataset.records), unit="records")
        for record in dataset.records:
            progress.update(failed=failed)
            try:
                normalized = self.normalize_record(record, dataset)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the source
                failed += 1
                issues.append(
                    Issue(
                        ErrorType.PARSER_ERROR,
                        Severity.ERROR,
                        f"Row {record.row_ref} could not be normalized: {type(exc).__name__}: {exc}",
                        stage="normalization",
                        row_ref=record.row_ref,
                        details={"traceback": traceback.format_exc(limit=3)},
                    )
                )
                continue
            if normalized is not None:
                out.append(normalized)
        return NormalizedDataset(self.source_id, dataset.snapshot, out, failed_rows=failed, issues=issues)

    def validate(self, dataset: NormalizedDataset) -> ValidationReport:
        return ValidationReport(issues=validate_dataset(dataset))
