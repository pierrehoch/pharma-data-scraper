"""Central source registry: YAML configuration synchronised into the DB."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.db.models import Source

COUNTRY_ALIASES = {"UK": "GB", "GE": "DE", "BRA": "BR", "CAN": "CA", "USA": "US", "FRA": "FR", "DEU": "DE"}

FREQUENCY_DAYS: dict[str, int | None] = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 31,
    "quarterly": 92,
    "annual": 366,
    "irregular": None,
}

REQUIRED_FIELDS = (
    "country",
    "name",
    "adapter",
    "enabled",
    "expected_frequency",
    "source_url",
    "identifier_type",
)


@dataclass
class SourceConfig:
    source_id: str
    country: str
    name: str
    adapter: str
    enabled: bool
    expected_frequency: str
    source_url: str
    identifier_type: str
    currency: str | None = None
    coverage_scope: str = "national"
    documentary_status: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def next_expected_update(self, last_success: datetime | None) -> datetime | None:
        days = FREQUENCY_DAYS.get(self.expected_frequency)
        if days is None or last_success is None:
            return None
        return last_success + timedelta(days=days)


class RegistryError(ValueError):
    pass


def normalize_country(code: str) -> str:
    code = code.strip().upper()
    return COUNTRY_ALIASES.get(code, code)


def load_registry(path: Path) -> dict[str, SourceConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = raw.get("sources") or {}
    registry: dict[str, SourceConfig] = {}
    for source_id, entry in sources.items():
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            raise RegistryError(f"Source '{source_id}' in {path} is missing fields: {missing}")
        if entry["expected_frequency"] not in FREQUENCY_DAYS:
            raise RegistryError(
                f"Source '{source_id}': expected_frequency '{entry['expected_frequency']}' "
                f"not in {sorted(FREQUENCY_DAYS)}"
            )
        registry[source_id] = SourceConfig(
            source_id=source_id,
            country=normalize_country(entry["country"]),
            name=entry["name"],
            adapter=entry["adapter"],
            enabled=bool(entry["enabled"]),
            expected_frequency=entry["expected_frequency"],
            source_url=entry["source_url"],
            identifier_type=entry["identifier_type"],
            currency=entry.get("currency"),
            coverage_scope=entry.get("coverage_scope", "national"),
            documentary_status=entry.get("documentary_status"),
            options=entry.get("options") or {},
        )
    return registry


def sync_registry(session: Session, registry: dict[str, SourceConfig]) -> None:
    """Upsert registry entries (and adapter metadata) into the DB so the dashboard can show them."""
    from app.adapters import get_adapter_class

    for cfg in registry.values():
        adapter_cls = get_adapter_class(cfg.adapter)
        src = session.get(Source, cfg.source_id) or Source(id=cfg.source_id)
        src.country = cfg.country
        src.name = cfg.name
        src.adapter = cfg.adapter
        src.enabled = cfg.enabled
        src.expected_frequency = cfg.expected_frequency
        src.source_url = cfg.source_url
        src.identifier_type = cfg.identifier_type
        src.adapter_status = str(adapter_cls.status)
        src.parser_type = adapter_cls.parser_type
        src.coverage_scope = cfg.coverage_scope
        src.currency = cfg.currency
        src.documentary_status = cfg.documentary_status
        src.notes = adapter_cls.limitations or None
        src.options = cfg.options
        session.add(src)
    session.flush()
