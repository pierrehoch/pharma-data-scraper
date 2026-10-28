from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: existing environment variables always win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    database_url: str
    raw_storage_dir: Path
    manual_import_dir: Path
    sources_file: Path
    http_timeout: float = 60.0
    http_user_agent: str = "pharma-data-scraper/0.1 (+public price research; contact: set HTTP_CONTACT)"
    http_delay_seconds: float = 0.5
    log_level: str = "INFO"
    log_format: str = "auto"  # auto (text on a terminal, json otherwise) | text | json
    progress_interval_seconds: float = 5.0
    trud_api_key: str | None = None
    ca_on_odb_xml_url: str | None = None
    publish_batch_size: int = 500
    secrets: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ
        trud = env.get("TRUD_API_KEY") or None
        contact = env.get("HTTP_CONTACT")
        ua = env.get("HTTP_USER_AGENT") or (
            f"pharma-data-scraper/0.1 (+public price research; contact: {contact})"
            if contact
            else cls.http_user_agent
        )
        return cls(
            database_url=env.get("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'storage' / 'local.db'}"),
            raw_storage_dir=Path(env.get("RAW_STORAGE_DIR", PROJECT_ROOT / "storage" / "raw")),
            manual_import_dir=Path(env.get("MANUAL_IMPORT_DIR", PROJECT_ROOT / "data" / "manual")),
            sources_file=Path(env.get("SOURCES_FILE", PROJECT_ROOT / "app" / "registry" / "sources.yaml")),
            http_timeout=float(env.get("HTTP_TIMEOUT", "60")),
            http_user_agent=ua,
            http_delay_seconds=float(env.get("HTTP_DELAY_SECONDS", "0.5")),
            log_level=env.get("LOG_LEVEL", "INFO"),
            log_format=env.get("LOG_FORMAT", "auto"),
            progress_interval_seconds=float(env.get("PROGRESS_INTERVAL_SECONDS", "5")),
            trud_api_key=trud,
            ca_on_odb_xml_url=env.get("CA_ON_ODB_XML_URL") or None,
            publish_batch_size=int(env.get("PUBLISH_BATCH_SIZE", "500")),
            secrets=[s for s in [trud] if s],
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(settings: Settings | None) -> None:
    global _settings
    _settings = settings
