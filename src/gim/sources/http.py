"""Polite, defensive HTTP client shared by all connectors.

* one honest User-Agent,
* a minimum interval between requests to the SAME host,
* retries with exponential back-off on 429 / 5xx (honours Retry-After),
* conditional GET (ETag / Last-Modified) so unchanged feeds cost almost nothing,
* a hard cap on response size (a hostile or broken server cannot exhaust memory).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from gim.config import Settings, get_settings

log = logging.getLogger(__name__)

MAX_BYTES = 15 * 1024 * 1024


class FetchError(RuntimeError):
    """Network / HTTP failure after retries. ``status`` is the HTTP code if any."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class PoliteHttp:
    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None,
                 min_interval_s: float = 2.0, sleep=time.sleep):
        self.settings = settings or get_settings()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.settings.user_agent})
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0.0) + self.min_interval_s - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last[host] = time.monotonic()

    def request(self, method: str, url: str, *, params=None, json=None, headers=None,
                etag: str | None = None, last_modified: str | None = None, attempts: int = 3) -> HttpResponse:
        host = urlsplit(url).hostname or ""
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        delay = 5.0
        for attempt in range(1, attempts + 1):
            self._throttle(host)
            try:
                resp = self.session.request(method, url, params=params, json=json, headers=hdrs,
                                            timeout=self.settings.http_timeout_s, stream=True)
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise FetchError(f"network error: {exc}") from exc
                log.warning("Network error for %s (attempt %d/%d): %s", host, attempt, attempts, exc)
                self._sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After", "")
                resp.close()
                if attempt == attempts:
                    raise FetchError(f"HTTP {resp.status_code} from {host}", resp.status_code)
                wait = min(float(retry_after), 120.0) if retry_after.isdigit() else delay
                log.warning("HTTP %d from %s, retrying in %.0fs", resp.status_code, host, wait)
                self._sleep(wait)
                delay *= 2
                continue

            body = self._read_capped(resp)
            out = HttpResponse(resp.status_code, body, {k.lower(): v for k, v in resp.headers.items()})
            if resp.status_code in (200, 304):
                return out
            snippet = body[:200].decode("utf-8", "replace")
            raise FetchError(f"HTTP {resp.status_code} from {host}: {snippet!r}", resp.status_code)
        raise FetchError("unreachable")  # pragma: no cover

    def get(self, url: str, **kw) -> HttpResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> HttpResponse:
        return self.request("POST", url, **kw)

    @staticmethod
    def _read_capped(resp) -> bytes:
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                resp.close()
                raise FetchError(f"response larger than {MAX_BYTES // 1_000_000} MB -- refusing")
            chunks.append(chunk)
        return b"".join(chunks)
