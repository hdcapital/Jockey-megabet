"""Shared HTTP layer: retries, backoff, throttling and raw-response archival.

Every response fetched through :class:`ArchivingClient` can be persisted to
``data/raw/`` (and to the ``raw_responses`` DB table by callers) so that any
number displayed by the scanner can later be reproduced from the exact bytes
the source returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings

log = logging.getLogger(__name__)


class SourceUnavailableError(Exception):
    """A data source could not be reached or refused the request.

    Carries the real underlying error so it can be reported honestly instead
    of being papered over with fabricated data.
    """

    def __init__(
        self,
        source: str,
        url: str,
        detail: str,
        status: int | None = None,
        body_snippet: str | None = None,
    ):
        self.source = source
        self.url = url
        self.detail = detail
        self.status = status
        self.body_snippet = body_snippet
        msg = f"{source} unavailable ({url}): {detail}"
        if body_snippet:
            msg += f" | response body: {body_snippet}"
        super().__init__(msg)


@dataclass
class FetchResult:
    url: str
    status_code: int
    fetched_at: datetime
    body: bytes
    sha256: str
    archive_path: Path | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


class _HostThrottle:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delay = self.min_interval - (now - last)
            self._last[host] = max(now, last + self.min_interval)
        if delay > 0:
            time.sleep(delay)


class ArchivingClient:
    """httpx wrapper with retries, throttling and raw archival."""

    def __init__(self, source: str, archive: bool | None = None):
        settings = get_settings()
        self.source = source
        self.archive = settings.archive_raw_responses if archive is None else archive
        self.archive_dir = settings.raw_archive_dir
        self._throttle = _HostThrottle(settings.http_min_request_interval_seconds)
        self._client = httpx.Client(
            timeout=settings.http_timeout_seconds,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-AU,en;q=0.9",
            },
            follow_redirects=True,
        )
        self._retries = settings.http_max_retries
        self._backoff = settings.http_backoff_base_seconds

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ArchivingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> FetchResult:
        return self._fetch("GET", url, params=params)

    def post_json(
        self,
        url: str,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> FetchResult:
        return self._fetch("POST", url, json_body=json_body, headers=headers, data=data)

    # ------------------------------------------------------------------
    def _fetch(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> FetchResult:
        host = urlsplit(url).netloc

        @retry(
            reraise=True,
            stop=stop_after_attempt(self._retries),
            wait=wait_exponential(multiplier=self._backoff, min=self._backoff, max=30),
            retry=retry_if_exception(_is_retryable),
        )
        def _do() -> httpx.Response:
            self._throttle.wait(host)
            resp = self._client.request(
                method, url, params=params, json=json_body, headers=headers, data=data
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                resp.raise_for_status()
            return resp

        try:
            resp = _do()
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailableError(
                self.source,
                url,
                f"HTTP {exc.response.status_code} after retries",
                status=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                self.source, url, f"{type(exc).__name__}: {exc}"
            ) from exc

        fetched_at = datetime.now(timezone.utc)
        body = resp.content
        digest = hashlib.sha256(body).hexdigest()
        result = FetchResult(
            url=str(resp.request.url),
            status_code=resp.status_code,
            fetched_at=fetched_at,
            body=body,
            sha256=digest,
            headers=dict(resp.headers),
        )
        if self.archive:
            result.archive_path = self._write_archive(result)
        if resp.status_code >= 400:
            # Keep a compressed sample of the real error body (e.g. a CDN
            # geo-block page) so failures are reported with evidence.
            snippet = " ".join(
                body[:600].decode("utf-8", errors="replace").split()
            )
            raise SourceUnavailableError(
                self.source,
                url,
                f"HTTP {resp.status_code}",
                status=resp.status_code,
                body_snippet=snippet or None,
            )
        return result

    def _write_archive(self, result: FetchResult) -> Path | None:
        try:
            day_dir = self.archive_dir / self.source / result.fetched_at.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            stamp = result.fetched_at.strftime("%H%M%S")
            path = day_dir / f"{stamp}_{result.sha256[:12]}.json"
            meta = {
                "source": self.source,
                "url": result.url,
                "fetched_at": result.fetched_at.isoformat(),
                "http_status": result.status_code,
                "sha256": result.sha256,
            }
            with open(path, "wb") as fh:
                fh.write(json.dumps(meta).encode())
                fh.write(b"\n")
                fh.write(result.body)
            return path
        except OSError as exc:  # archival must never take down a scan
            log.warning("raw archive write failed: %s", exc)
            return None
