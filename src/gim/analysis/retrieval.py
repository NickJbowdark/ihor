"""Retrieval over the historical archive (the "R" in RAG).

Given an event, find EARLIER events in the GIM database that are about the same
places, organisations and subject (SQLite FTS5 / BM25 over titles and summaries).
They are passed to the model as ARCHIVE evidence with their own DOC ids, so a
statement such as "talks last met in March" must cite an archived document.

Rules: only events that ended before the current one started (no leakage from the
future), never the event itself, never mixing synthetic fixtures into live analysis.
Phase 6 can add embeddings for semantic similarity on top of this lexical retrieval.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from gim.analysis.bundle import build_event_bundles
from gim.analysis.contracts import EventBundle
from gim.storage import intel


def history_query(event: dict) -> str:
    parts = [event.get("headline") or ""]
    parts += (event.get("countries_mentioned") or "").split(",")
    parts += (event.get("organizations") or "").split(",")
    return " ".join(p for p in parts if p)


def related_history(conn, event: dict, k: int = 4, query: str | None = None) -> tuple[EventBundle, ...]:
    before = datetime.fromisoformat(event["first_published_at"]) - timedelta(hours=12)
    origin = event.get("data_origin") or "live"
    origins = ["fixture"] if origin == "fixture" else ["live"]
    hits = intel.search_events(conn, query or history_query(event), before=before, limit=k * 3,
                               exclude_event=event.get("id"), data_origins=origins)
    hits = [h for h in hits if h["last_published_at"] < event["first_published_at"]]
    # Relevance guard: if the event names countries/organisations, a related past event must share one.
    anchors = _set(event.get("countries_mentioned")) | _set(event.get("organizations"))
    if anchors:
        hits = [h for h in hits if anchors & (_set(h.get("countries_mentioned")) | _set(h.get("organizations")))]
    return build_event_bundles(conn, hits[:k])


def _set(csv: str | None) -> set[str]:
    return {x for x in (csv or "").split(",") if x}
