"""HTTP access with retries, politeness delay, file:// support and secret redaction."""

from __future__ import annotations

import email.utils
import json
import shutil
import time
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import httpx

from app.core.errors import DownloadError
from app.observability import Progress, get_logger, human_bytes, redact

log = get_logger(__name__)


@dataclass
class TextResponse:
    url: str
    text: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadResult:
    path: Path
    final_url: str
    content_type: str | None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def filename_from_headers(self) -> str | None:
        disp = self.headers.get("content-disposition", "")
        for part in disp.split(";"):
            part = part.strip()
            if part.lower().startswith("filename="):
                return part.split("=", 1)[1].strip().strip('"')
        return None

    @property
    def last_modified(self) -> date | None:
        value = self.headers.get("last-modified")
        if not value:
            return None
        try:
            return email.utils.parsedate_to_datetime(value).date()
        except (TypeError, ValueError):
            return None


class HttpClientProtocol(Protocol):
    def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> TextResponse: ...

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any: ...

    def download(self, url: str, dest: Path, *, headers: dict[str, str] | None = None) -> DownloadResult: ...


class HttpClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, *, user_agent: str, timeout: float = 60.0, delay_seconds: float = 0.5, retries: int = 3):
        self.retries = retries
        self.delay_seconds = delay_seconds
        self._last_request: dict[str, float] = {}
        self._throttle_lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en,fr;q=0.8,*;q=0.5"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 20.0)),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _throttle(self, url: str) -> None:
        with self._throttle_lock:
            self._throttle_locked(url)

    def _throttle_locked(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            wait = self.delay_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None, stream: bool = False):
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle(url)
            try:
                if stream:
                    request = self._client.build_request(method, url, headers=headers)
                    response = self._client.send(request, stream=True)
                else:
                    response = self._client.request(method, url, headers=headers)
            except httpx.ConnectError as exc:
                last_exc = exc
                if "nodename nor servname" in str(exc) or "Name or service not known" in str(exc):
                    raise DownloadError(
                        f"Host '{urlparse(url).netloc}' cannot be resolved (DNS) for {redact(url)}",
                        details={"url": redact(url)},
                    ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
            else:
                if response.status_code in self.RETRY_STATUSES and attempt < self.retries:
                    if stream:
                        response.close()
                    last_exc = DownloadError(f"HTTP {response.status_code} for {redact(url)}")
                    time.sleep(min(2**attempt, 10))
                    continue
                if response.status_code >= 400:
                    if stream:
                        response.read()
                        response.close()
                    raise DownloadError(
                        f"HTTP {response.status_code} when requesting {redact(url)}",
                        details={"url": redact(url), "status_code": response.status_code, "body": redact(response.text[:500])},
                    )
                return response
            log.warning("HTTP attempt %s/%s failed for %s: %s", attempt, self.retries, redact(url), last_exc)
            time.sleep(min(2**attempt, 10))
        raise DownloadError(
            f"Request to {redact(url)} failed after {self.retries} attempts: {redact(str(last_exc))}",
            details={"url": redact(url)},
        )

    def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> TextResponse:
        if url.startswith("file://"):
            path = Path(unquote(urlparse(url).path))
            return TextResponse(url=url, text=path.read_text(encoding="utf-8"), status_code=200)
        log.info("Fetching %s", redact(url))
        response = self._request("GET", url, headers=headers)
        return TextResponse(
            url=str(response.url), text=response.text, status_code=response.status_code, headers=dict(response.headers)
        )

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        resp = self.get_text(url, headers=headers)
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise DownloadError(f"Expected JSON from {redact(url)} but got: {resp.text[:200]!r}") from exc

    def download(self, url: str, dest: Path, *, headers: dict[str, str] | None = None) -> DownloadResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.startswith("file://"):
            src = Path(unquote(urlparse(url).path))
            if not src.is_file():
                raise DownloadError(f"Local file not found: {src}")
            shutil.copyfile(src, dest)
            return DownloadResult(path=dest, final_url=url, content_type=None)
        response = self._request("GET", url, headers=headers, stream=True)
        length = response.headers.get("content-length")
        total = int(length) if length and length.isdigit() else None
        log.info("Downloading %s%s", redact(url), f" ({human_bytes(total)})" if total else "")
        progress = Progress(f"download {dest.name}", total=total, unit="bytes", formatter=human_bytes, check_every=20)
        try:
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
                    progress.update(len(chunk))
        except httpx.HTTPError as exc:
            raise DownloadError(f"Transfer of {redact(url)} interrupted after {human_bytes(progress.done)}: {exc}") from exc
        finally:
            response.close()
        progress.finish()
        return DownloadResult(
            path=dest,
            final_url=str(response.url),
            content_type=response.headers.get("content-type"),
            headers={k.lower(): v for k, v in response.headers.items()},
        )
