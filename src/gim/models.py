"""Core data structures shared by all pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Article:
    """One news article as reported by a collection source.

    IMPORTANT distinction (a core OSINT habit):
      * ``seen_at`` is when the *source system* (GDELT) first saw the article.
        It approximates the publication time.
      * It is NOT the date the reported event happened. Event dates are
        extracted later (Phase 3) and stored separately.
    """

    url: str
    canonical_url: str
    title: str
    title_fingerprint: str
    domain: str
    language: str
    source_country: str
    seen_at: datetime  # timezone-aware, UTC
    source: str = "gdelt"
    categories: list[str] = field(default_factory=list)


@dataclass
class Document:
    """A collected document from ANY source (news item, official statement, report...).

    Provenance fields answer: who published it, who originally reported it,
    when was it published, when did WE collect it, and through which source.
    Time fields are timezone-aware UTC datetimes.
    """

    source_id: str                 # registry id of the collection source, e.g. 'un_news'
    url: str                       # original URL as delivered by the source
    canonical_url: str             # normalised URL (dedup key)
    title: str                     # original title, cleaned of markup only
    publisher: str                 # organisation/site that published THIS copy
    source_category: str           # registry category, e.g. 'official_eu'
    doc_type: str                  # original_reporting | syndicated | official_statement | ...
    language: str = ""
    published_at: datetime | None = None   # publication time stated by the source
    updated_at: datetime | None = None     # last update stated by the source (corrections)
    event_date: str | None = None          # ISO date of the event itself, when known
    summary: str = ""                       # short summary as provided by the source
    original_reporter: str = ""             # e.g. 'Reuters' for wire copy, author org on ReliefWeb
    external_id: str = ""                   # source's own id / guid
    # GEOGRAPHY -- four different questions, never substitute one for another:
    #   publisher_country    where the PUBLISHER is based (GDELT 'sourcecountry')
    #   countries_mentioned  countries NAMED in title/summary (gazetteer match)
    #   event location / countries involved  -> stored on the EVENT, unknown until
    #   established by a dataset, an analyst or (Phase 3) checked AI extraction.
    publisher_country: str = ""
    countries_mentioned: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    title_fingerprint: str = ""
    data_origin: str = "live"               # live | fixture
    provenance: dict = field(default_factory=dict)       # connector details: feed url, raw ids...
