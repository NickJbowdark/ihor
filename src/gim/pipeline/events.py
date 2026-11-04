"""Deterministic event grouping (first implementation, no AI).

Vocabulary used throughout GIM:
  SOURCE    a collection channel in the registry (e.g. 'un_news', 'gdelt')
  DOCUMENT  one published item at one URL (article, press release, report)
  CLAIM     a statement inside a document, attributed to someone (Phase 3: extracted
            and checked; see gim.analysis.contracts)
  EVENT     a real-world happening that one or many documents report on

Goal: 30 outlets reporting the same meeting -> ONE event with 30 linked
documents. Every document keeps its own URL, publisher and attribution.

Algorithm (incremental, deterministic, explainable):
  Documents are processed oldest first. For each document not yet in an event,
  compare it with the members of recent events (within +/- 48 h):
    1. identical headline fingerprint                     -> same event (score 1.0)
    2. headline token similarity (Jaccard) >= 0.5 with at least 3 shared key
       tokens, AND compatible countries (if both name countries, at least one
       in common)                                          -> same event
  Best score wins; ties go to the older event (lower id). Otherwise a new event.
  Events are never merged or renumbered automatically, so EVT ids stay stable.

Known limits (by design, honest): different wording for the same event
("Iran, Saudi Arabia hold talks" vs "Tehran and Riyadh diplomats meet") may
stay separate; two different events with near-identical headlines on the same
day ("Explosion in Kabul") may be merged. Phase 3 adds semantic similarity and
AI checks ON TOP of this, and keeps this method as the auditable baseline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gim.pipeline.geo import tag_places
from gim.pipeline.normalize import key_tokens

CLUSTERING_METHOD = "deterministic-title-v1"
WINDOW = timedelta(hours=48)
MIN_JACCARD = 0.5
MIN_SHARED_TOKENS = 3


def _stem(token: str) -> str:
    # tiny plural normaliser: talks->talk, ministers->minister (not 'press'/'us')
    return token[:-1] if len(token) > 4 and token.endswith("s") and not token.endswith("ss") else token


def event_tokens(title: str) -> frozenset[str]:
    """Headline tokens + normalised country names (so 'Iranian' and 'Iran' match)."""
    tokens = {_stem(t) for t in key_tokens(title)}
    countries, _ = tag_places(title)
    tokens |= {f"@{c.lower()}" for c in countries}
    return frozenset(tokens)


@dataclass
class ClusterDoc:
    id: int
    timeline_at: datetime
    title: str
    fingerprint: str
    countries: frozenset[str]
    tokens: frozenset[str] = field(default=frozenset())

    def __post_init__(self):
        if not self.tokens:
            self.tokens = event_tokens(self.title)


@dataclass
class ClusterEvent:
    id: int
    first_at: datetime
    last_at: datetime
    members: list[ClusterDoc]


def similarity(a: ClusterDoc, b: ClusterDoc) -> tuple[float, str]:
    """(score, method) -- score 0 means 'not the same event'."""
    if a.fingerprint and a.fingerprint == b.fingerprint:
        return 1.0, "same_fingerprint"
    if a.countries and b.countries and not (a.countries & b.countries):
        return 0.0, "countries_incompatible"
    shared = a.tokens & b.tokens
    union = a.tokens | b.tokens
    if not union or len(shared) < MIN_SHARED_TOKENS:
        return 0.0, "too_few_shared_tokens"
    jaccard = len(shared) / len(union)
    return (round(jaccard, 3), "title_similarity") if jaccard >= MIN_JACCARD else (0.0, "below_threshold")


def choose_event(doc: ClusterDoc, events: list[ClusterEvent]) -> tuple[int | None, str, float]:
    """Pick the best matching event for ``doc`` or None. Deterministic."""
    best: tuple[float, int, str] | None = None
    for ev in sorted(events, key=lambda e: e.id):
        if doc.timeline_at < ev.first_at - WINDOW or doc.timeline_at > ev.last_at + WINDOW:
            continue
        for member in ev.members:
            score, method = similarity(doc, member)
            if score > 0 and (best is None or score > best[0]):
                best = (score, ev.id, method)
    if best is None:
        return None, "seed", 1.0
    return best[1], best[2], best[0]


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _countries(csv: str | None) -> frozenset[str]:
    return frozenset(c for c in (csv or "").split(",") if c)


def assign_events(conn) -> dict[str, int]:
    """Attach every document that has no event yet. Returns counts for logging.

    Runs in one transaction: either all assignments of this pass are stored or none.
    """
    from gim.storage import db  # local import keeps pipeline modules free of storage at import time

    pending = db.unassigned_documents(conn)
    if not pending:
        return {"documents": 0, "new_events": 0, "linked": 0}
    since = (_dt(pending[0]["timeline_at"]) - WINDOW).isoformat(timespec="seconds")
    events: dict[int, ClusterEvent] = {}
    for r in db.event_members_since(conn, since):
        ev = events.setdefault(r["event_id"], ClusterEvent(r["event_id"], _dt(r["first_published_at"]),
                                                           _dt(r["last_published_at"]), []))
        ev.members.append(ClusterDoc(r["id"], _dt(r["timeline_at"]), r["title"], r["title_fingerprint"],
                                     _countries(r["countries_mentioned"])))
    new_events = linked = 0
    with db.transaction(conn):
        for r in pending:
            doc = ClusterDoc(r["id"], _dt(r["timeline_at"]), r["title"], r["title_fingerprint"],
                             _countries(r["countries_mentioned"]))
            event_id, method, score = choose_event(doc, list(events.values()))
            if event_id is None:
                event_id = db.create_event(conn, doc.title, r["timeline_at"], CLUSTERING_METHOD)
                events[event_id] = ClusterEvent(event_id, doc.timeline_at, doc.timeline_at, [])
                new_events += 1
            else:
                linked += 1
            db.link_event_document(conn, event_id, doc.id, method, score)
            ev = events[event_id]
            ev.members.append(doc)
            ev.first_at, ev.last_at = min(ev.first_at, doc.timeline_at), max(ev.last_at, doc.timeline_at)
            db.refresh_event(conn, event_id)
    return {"documents": len(pending), "new_events": new_events, "linked": linked}
