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
