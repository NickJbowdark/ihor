"""Client for the GDELT DOC 2.0 API (article search).

What GDELT DOC gives you: article *metadata* (URL, title, outlet domain,
language, source country, time first seen) for worldwide news coverage
over a rolling window of roughly the last 3 months. It does NOT give article
full text -- and we do not scrape it (copyright, terms of use, load on sites).

Access etiquette, enforced in code:
  * at most one request every 5+ seconds (configurable, never lower),
  * honest User-Agent,
  * exponential back-off on 429 / 5xx,
  * a hard cap on records per request (GDELT max = 250).

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from gim.config import Settings, get_settings
from gim.models import Article
from gim.pipeline.classify import classify_title
from gim.pipeline.normalize import canonicalize_url, clean_text, is_safe_url, title_fingerprint

log = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250
_TIMESPAN_RE = re.compile(r"^\d{1,3}(min|h|d|w|m)$")


class GdeltError(RuntimeError):
    """Raised when GDELT returns an error or an unusable response."""


class GdeltClient:
    _lock = threading.Lock()
    _last_request = 0.0  # shared across instances: one polite rate per process

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None):
        self.settings = settings or get_settings()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.settings.user_agent})

    # -- low level ---------------------------------------------------------
    def _throttle(self) -> None:
        with GdeltClient._lock:
            wait = GdeltClient._last_request + self.settings.gdelt_min_interval_s - time.monotonic()
            if wait > 0:
                log.debug("Throttling GDELT request for %.1fs", wait)
                time.sleep(wait)
            GdeltClient._last_request = time.monotonic()

    def _get_json(self, params: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
        delay = 10.0
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = self.session.get(GDELT_DOC_URL, params=params, timeout=self.settings.http_timeout_s)
            except requests.RequestException as exc:
                log.warning("GDELT network error (attempt %d/%d): %s", attempt, attempts, exc)
                if attempt == attempts:
                    raise GdeltError(f"Network error contacting GDELT: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("GDELT HTTP %d (attempt %d/%d)", resp.status_code, attempt, attempts)
                if attempt == attempts:
                    raise GdeltError(f"GDELT unavailable (HTTP {resp.status_code})")
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise GdeltError(f"GDELT HTTP {resp.status_code}: {resp.text[:200]!r}")

            text = resp.text.strip()
            if not text:
                return {}
            try:
                return resp.json()
            except ValueError:
                # GDELT reports query problems as plain text, e.g.
                # "Queries containing OR'd terms must be surrounded by ()."
                raise GdeltError(f"GDELT rejected the query: {clean_text(text, 300)}") from None
        raise GdeltError("unreachable")  # pragma: no cover

    # -- public API --------------------------------------------------------
    def search_articles(self, query: str, timespan: str = "24h", max_records: int = MAX_RECORDS) -> list[Article]:
        """Return articles matching ``query`` seen within ``timespan`` (e.g. 24h, 7d, 2w)."""
        if not _TIMESPAN_RE.match(timespan):
            raise ValueError(f"Invalid timespan {timespan!r}; use e.g. 30min, 24h, 7d, 2w, 1m")
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": max(1, min(int(max_records), MAX_RECORDS)),
            "timespan": timespan,
            "sort": "DateDesc",
        }
        log.info("Querying GDELT", extra={"query": query, "timespan": timespan})
        data = self._get_json(params)
        return parse_artlist(data)


def parse_seendate(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_artlist(data: dict[str, Any]) -> list[Article]:
    """Convert a raw GDELT ArtList JSON response into validated Article objects.

    Records that fail validation are skipped and logged -- never trusted blindly.
    """
    raw_items = data.get("articles") or []
    if not isinstance(raw_items, list):
        raise GdeltError("Unexpected GDELT response shape: 'articles' is not a list")

    articles: list[Article] = []
    skipped = 0
    for item in raw_items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        url = str(item.get("url") or "").strip()
        title = clean_text(item.get("title"))
        seen_at = parse_seendate(item.get("seendate", ""))
        if not (is_safe_url(url) and title and seen_at):
            skipped += 1
            continue
        articles.append(
            Article(
                url=url,
                canonical_url=canonicalize_url(url),
                title=title,
                title_fingerprint=title_fingerprint(title),
                domain=clean_text(item.get("domain"), 200).lower(),
                language=clean_text(item.get("language"), 50),
                source_country=clean_text(item.get("sourcecountry"), 100),
                seen_at=seen_at,
                source="gdelt",
                categories=classify_title(title),
            )
        )
    if skipped:
        log.warning("Skipped %d malformed GDELT records", skipped)
    return articles
