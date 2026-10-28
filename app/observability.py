"""Structured logging with job/source/stage context."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterator

_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)
_source_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("source_id", default=None)
_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar("stage", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_secrets: list[str] = []


def register_secrets(values: list[str]) -> None:
    _secrets.extend(v for v in values if v and v not in _secrets)


def redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "job_id": _job_id.get(),
            "source_id": _source_id.get(),
            "stage": _stage.get(),
            "message": redact(record.getMessage()),
            "logger": record.name,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable format for terminals: '16:17:02 INFO  [us_nadac/publish] message'."""

    def format(self, record: logging.LogRecord) -> str:
        source, stage = _source_id.get(), _stage.get()
        ctx = f"[{source}{'/' + stage if stage else ''}] " if source else ""
        base = f"{datetime.now().strftime('%H:%M:%S')} {record.levelname:<7} {ctx}{redact(record.getMessage())}"
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def configure_logging(level: str = "INFO", fmt: str = "auto") -> None:
    if fmt == "auto":
        fmt = "text" if sys.stderr.isatty() else "json"
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "pdfminer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(name)


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


PROGRESS_INTERVAL_SECONDS = 5.0


class Progress:
    """Throttled progress log for long loops: done/total, rate, ETA and optional counters.

    `update()` is cheap (the clock is only read every `check_every` calls), so it can be called per row.
    """

    def __init__(
        self,
        label: str,
        *,
        total: int | None = None,
        unit: str = "rows",
        logger: logging.Logger | None = None,
        interval: float | None = None,
        check_every: int = 200,
        formatter=None,  # noqa: ANN001 - callable(done) -> str for non-count units (e.g. bytes)
    ):
        self.label = label
        self.total = total
        self.unit = unit
        self.log = logger or get_logger("app.progress")
        self.interval = PROGRESS_INTERVAL_SECONDS if interval is None else interval
        self.check_every = max(1, check_every)
        self.formatter = formatter
        self.done = 0
        self.extra: dict[str, Any] = {}
        self._start = time.monotonic()
        self._last_log = self._start
        self._calls = 0

    def update(self, n: int = 1, **extra: Any) -> None:
        self.done += n
        if extra:
            self.extra.update(extra)
        self._calls += 1
        if self._calls % self.check_every == 0 or n > 1:
            now = time.monotonic()
            if now - self._last_log >= self.interval:
                self._last_log = now
                self.log.info(self.message())

    def set(self, done: int, **extra: Any) -> None:
        self.update(done - self.done, **extra)

    def message(self, *, final: bool = False) -> str:
        elapsed = max(time.monotonic() - self._start, 1e-9)
        rate = self.done / elapsed
        fmt = self.formatter or (lambda v: f"{v:,}")
        parts = [f"{self.label}: {fmt(self.done)}"]
        if self.total:
            parts[0] += f"/{fmt(self.total)} {self.unit} ({100 * self.done / self.total:.1f}%)"
        else:
            parts[0] += f" {self.unit}"
        rate_txt = fmt(int(rate)) if self.formatter else f"{rate:,.0f}"
        parts.append(f"{rate_txt} {self.unit}/s")
        if final:
            parts.append(f"in {human_duration(elapsed)}")
        elif self.total and rate > 0 and self.done < self.total:
            parts.append(f"ETA {human_duration((self.total - self.done) / rate)}")
        else:
            parts.append(f"elapsed {human_duration(elapsed)}")
        if self.extra:
            parts.append(", ".join(f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}" for k, v in self.extra.items()))
        return " · ".join(parts)

    def finish(self, **extra: Any) -> None:
        if extra:
            self.extra.update(extra)
        self.log.info(self.message(final=True))


@contextlib.contextmanager
def log_context(*, job_id: str | None = None, source_id: str | None = None, stage: str | None = None) -> Iterator[None]:
    tokens = []
    if job_id is not None:
        tokens.append((_job_id, _job_id.set(job_id)))
    if source_id is not None:
        tokens.append((_source_id, _source_id.set(source_id)))
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
