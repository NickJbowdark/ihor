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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from gim.config import Settings, get_settings

log = logging.getLogger(__name__)

MAX_BYTES = 15 * 1024 * 1024
# Retry-After waits up to this long are slept through; longer ones abort the run and the
# collector schedules the source again after the requested time (no blocked scheduler).
MAX_INLINE_WAIT_S = 120.0


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header ("120" or an HTTP date). None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


class FetchError(RuntimeError):
    """Network / HTTP failure after retries. ``status`` is the HTTP code if any."""

    def __init__(self, message: str, status: int | None = None, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s

    @property
    def kind(self) -> str:
        if self.status == 429:
            return "rate_limited"
        if self.status is None:
            return "network"
        return "http_5xx" if self.status >= 500 else "http_4xx"


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
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                resp.close()
                if retry_after is not None and retry_after > MAX_INLINE_WAIT_S:
                    raise FetchError(f"HTTP {resp.status_code} from {host}; server asks to retry after "
                                     f"{retry_after:.0f}s", resp.status_code, retry_after)
                if attempt == attempts:
                    raise FetchError(f"HTTP {resp.status_code} from {host} after {attempts} attempts",
                                     resp.status_code, retry_after)
                wait = retry_after if retry_after is not None else delay
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
