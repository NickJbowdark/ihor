"""Build immutable AnalysisRequests from the database (read-only)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from gim.analysis.contracts import (AnalysisRequest, EventBundle, EventGeography, EvidenceDocument,
                                    EvidenceExcerpt, doc_ref, event_ref)
from gim.pipeline.corroboration import summarise
from gim.storage import db


def _split(csv: str | None) -> tuple[str, ...]:
    return tuple(x for x in (csv or "").split(",") if x)


def _evidence(row: dict) -> EvidenceDocument:
    return EvidenceDocument(
        ref=doc_ref(row["id"]), source_id=row["source_id"], url=row["url"], canonical_url=row["canonical_url"],
        title=row["title"], summary=row["summary"], publisher=row["publisher"],
        original_reporter=row["original_reporter"], doc_type=row["doc_type"], source_category=row["source_category"],
        language=row["language"], published_at=row["published_at"], updated_at=row["updated_at"],
        collected_at=row["collected_at"], event_date=row["event_date"], publisher_country=row["publisher_country"],
        countries_mentioned=_split(row["countries_mentioned"]), content_hash=row["content_hash"],
        data_origin=row["data_origin"], delivered_by=_split(row.get("delivered_by")), provenance=row["provenance"],
    )


def build_event_bundles(conn, event_rows: list[dict]) -> tuple[EventBundle, ...]:
    docs_by_event = db.event_documents(conn, [e["id"] for e in event_rows])
    bundles = []
    for e in event_rows:
        rows = docs_by_event.get(e["id"], [])
        documents = tuple(_evidence(r) for r in rows)
        excerpts = tuple(
            EvidenceExcerpt(f"{d.ref}#{f}", d.ref, f, getattr(d, f))
            for d in documents for f in ("title", "summary") if getattr(d, f)
        )
        involved = _split(e["countries_involved"]) if e["countries_involved"] else None
        bundles.append(EventBundle(
            ref=event_ref(e["id"]), headline=e["headline"], first_published_at=e["first_published_at"],
            last_published_at=e["last_published_at"], clustering_method=e["clustering_method"],
            geography=EventGeography(e["event_country"], involved, e["location_basis"],
                                     _split(e["countries_mentioned"])),
            corroboration=json.dumps(summarise(rows).as_dict(), ensure_ascii=False),
            documents=documents, excerpts=excerpts,
        ))
    return tuple(bundles)


def build_request(conn, since: datetime, until: datetime | None = None, *, task: str = "event_summary",
                  regions: list[str] | None = None, live_only: bool = True, max_events: int = 50) -> AnalysisRequest:
    until = until or datetime.now(timezone.utc)
    events = db.query_events(conn, since, until, regions=regions,
                             data_origins=["live"] if live_only else None, limit=max_events)
    return AnalysisRequest(
        request_id=f"req-{uuid.uuid4().hex[:12]}", task=task,
        created_at=db.utcnow_iso(), window_start=db.to_iso(since), window_end=db.to_iso(until),
        events=build_event_bundles(conn, events),
    )
