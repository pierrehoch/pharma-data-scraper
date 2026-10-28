"""Immutable raw snapshot storage: raw/<source_id>/<retrieval timestamp>/<files> + metadata.json."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from app.core.types import ArtifactFile, RawSnapshot, SnapshotFile, SourceArtifact
from app.observability import redact

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    name = _SAFE.sub("_", name).strip("._") or "file"
    return name[:180]


@dataclass
class DownloadedFile:
    artifact_file: ArtifactFile
    temp_path: Path
    final_url: str
    content_type: str | None
    filename: str | None = None


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root

    def staging_dir(self, source_id: str) -> Path:
        path = self.root / ".staging" / source_id / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _new_snapshot_dir(self, source_id: str, retrieved_at: datetime) -> Path:
        base = self.root / source_id / retrieved_at.strftime("%Y-%m-%dT%H%M%S_%fZ")
        candidate, n = base, 1
        while candidate.exists():  # never overwrite an existing snapshot
            candidate = base.with_name(f"{base.name}-{n}")
            n += 1
        candidate.mkdir(parents=True)
        return candidate

    def save(
        self,
        source_id: str,
        artifact: SourceArtifact,
        downloaded: list[DownloadedFile],
        *,
        parser_version: str,
        previous_id: str | None = None,
        previous_hashes: list[str] | None = None,
    ) -> RawSnapshot:
        retrieved_at = datetime.now(timezone.utc)
        directory = self._new_snapshot_dir(source_id, retrieved_at)
        files: list[SnapshotFile] = []
        used_names: set[str] = set()
        for item in downloaded:
            name = safe_filename(item.filename or item.artifact_file.filename)
            if name in used_names:
                name = f"{item.artifact_file.role}__{name}"
            used_names.add(name)
            target = directory / name
            shutil.move(str(item.temp_path), target)
            files.append(
                SnapshotFile(
                    role=item.artifact_file.role,
                    filename=name,
                    path=target,
                    source_url=redact(item.final_url or item.artifact_file.url),
                    sha256=sha256_file(target),
                    size=target.stat().st_size,
                    content_type=item.content_type or item.artifact_file.content_type,
                    requested_url=redact(item.artifact_file.url),
                )
            )
        snapshot_id = f"{source_id}:{directory.name}"
        duplicate_of = None
        if previous_id and previous_hashes and sorted(previous_hashes) == sorted(f.sha256 for f in files):
            duplicate_of = previous_id
        snapshot = RawSnapshot(
            id=snapshot_id,
            source_id=source_id,
            retrieved_at=retrieved_at,
            directory=directory,
            files=files,
            published_at=artifact.published_at,
            edition=artifact.edition,
            parser_version=parser_version,
            partial=artifact.partial,
            duplicate_of=duplicate_of,
        )
        self._write_metadata(snapshot, artifact)
        return snapshot

    def _write_metadata(self, snapshot: RawSnapshot, artifact: SourceArtifact) -> None:
        primary = snapshot.primary
        metadata = {
            "snapshot_id": snapshot.id,
            "source_id": snapshot.source_id,
            "source_url": primary.source_url,
            "retrieved_at": snapshot.retrieved_at.isoformat(),
            "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
            "edition": snapshot.edition,
            "filename": primary.filename,
            "content_type": primary.content_type,
            "sha256": primary.sha256,
            "parser_version": snapshot.parser_version,
            "partial": snapshot.partial,
            "duplicate_of": snapshot.duplicate_of,
            "discovered_from": redact(artifact.discovered_from or ""),
            "discovery_notes": artifact.notes,
            "files": [
                {
                    "role": f.role,
                    "filename": f.filename,
                    "source_url": f.source_url,
                    "requested_url": f.requested_url,
                    "sha256": f.sha256,
                    "size": f.size,
                    "content_type": f.content_type,
                }
                for f in snapshot.files
            ],
        }
        path = snapshot.directory / "metadata.json"
        path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o444)
        for f in snapshot.files:
            f.path.chmod(0o444)

    def load(self, directory: Path) -> RawSnapshot:
        meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        files = [
            SnapshotFile(
                role=f["role"],
                filename=f["filename"],
                path=directory / f["filename"],
                source_url=f["source_url"],
                sha256=f["sha256"],
                size=f["size"],
                content_type=f.get("content_type"),
                requested_url=f.get("requested_url"),
            )
            for f in meta["files"]
        ]
        for f in files:
            if sha256_file(f.path) != f.sha256:
                raise ValueError(f"Snapshot file {f.path} does not match its recorded sha256 – raw data was modified")
        return RawSnapshot(
            id=meta["snapshot_id"],
            source_id=meta["source_id"],
            retrieved_at=datetime.fromisoformat(meta["retrieved_at"]),
            directory=directory,
            files=files,
            published_at=date.fromisoformat(meta["published_at"]) if meta.get("published_at") else None,
            edition=meta.get("edition"),
            parser_version=meta.get("parser_version"),
            partial=meta.get("partial", False),
            reused=True,
            duplicate_of=meta.get("duplicate_of"),
        )
