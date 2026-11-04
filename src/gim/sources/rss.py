"""Generic RSS / Atom connector (UN News, Council of the EU, Commission, media...).

We download the feed ourselves (polite client, size cap, conditional GET) and
hand the BYTES to feedparser -- feedparser never touches the network, so it
cannot follow redirects to unexpected places or ignore our rate limits.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser

from gim.models import Document
from gim.pipeline.attribution import classify_reporting
from gim.pipeline.classify import classify_title
from gim.pipeline.geo import tag_places
from gim.pipeline.normalize import canonicalize_url, clean_text, is_safe_url, title_fingerprint
from gim.sources.base import Connector, FetchResult, FetchState, ParseError
from gim.sources.registry import SourceSpec

log = logging.getLogger(__name__)
MAX_SUMMARY = 600


def _dt(struct) -> datetime | None:
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_feed(body: bytes, spec: SourceSpec, feed_url: str | None = None) -> list[Document]:
    parsed = feedparser.parse(body)
    if not parsed.entries and not parsed.get("version"):
        # e.g. an HTML error / consent page instead of a feed
        exc = parsed.get("bozo_exception")
        raise ParseError(f"not a valid RSS/Atom feed ({type(exc).__name__ if exc else 'no feed markup'})")
    feed_lang = clean_text(parsed.feed.get("language", ""), 20).lower()[:2]
    default_lang = feed_lang or (spec.languages[0] if spec.languages else "")
    is_news = spec.category in ("international_media", "news_discovery")

    docs: list[Document] = []
    skipped = 0
    for entry in parsed.entries:
        url = (entry.get("link") or "").strip()
        title = clean_text(entry.get("title"))
        if not (is_safe_url(url) and title):
            skipped += 1
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"), MAX_SUMMARY)
        publisher_domain = (urlsplit(url).hostname or "").removeprefix("www.")
        text = f"{title}. {summary}"
        doc_type, reporter = (classify_reporting(publisher_domain, text, spec.doc_type)
                              if is_news else (spec.doc_type, ""))
        countries, regions = tag_places(text)
        if not regions:
            regions = list(spec.default_regions) or ["global"]
        # dict.get bypasses feedparser's deprecated published/updated aliasing
        published = _dt(dict.get(entry, "published_parsed")) or _dt(dict.get(entry, "updated_parsed"))
        updated = _dt(dict.get(entry, "updated_parsed"))
        docs.append(Document(
            source_id=spec.id,
            url=url,
            canonical_url=canonicalize_url(url),
            title=title,
            publisher=spec.organization,
            source_category=spec.category,
            doc_type=doc_type,
            language=clean_text(entry.get("language", ""), 10).lower()[:2] or default_lang,
            published_at=published,
            updated_at=updated if updated and updated != published else None,
            summary=summary,
            original_reporter=reporter or (spec.organization if not is_news else ""),
            external_id=clean_text(entry.get("id", ""), 300),
            countries=countries,
            regions=regions,
            categories=classify_title(text),
            title_fingerprint=title_fingerprint(title),
            provenance={"connector": "rss", "feed_url": feed_url or spec.url,
                        "published_missing": published is None},
        ))
    if skipped:
        log.warning("%s: skipped %d feed entries without a safe link or title", spec.id, skipped)
    return docs


class RssConnector(Connector):
    def fetch(self, state: FetchState) -> FetchResult:
        resp = self.http.get(self.spec.url, etag=state.etag, last_modified=state.last_modified)
        result = FetchResult(etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"))
        if resp.not_modified:
            result.not_modified = True
            result.etag = result.etag or state.etag
            result.last_modified = result.last_modified or state.last_modified
            return result
        result.raw.append(("feed.xml", resp.body))
        result.documents = parse_feed(resp.body, self.spec)
        return result
