"""Data contracts between GIM and any AI provider.

INPUT  (what a model receives): AnalysisRequest -> EventBundle -> EvidenceDocument / EvidenceExcerpt
OUTPUT (what a model must return): AnalysisResult -> Claim

Design rules
  * Every input object is frozen (immutable) and carries permanent references:
      DOC-<documents.id>, EVT-<events.id>, DOC-<id>#title / #summary for excerpts.
  * Source text is UNTRUSTED DATA. It travels in clearly separated fields and must
    never be interpreted as instructions (prompt-injection defence lives in the
    provider's prompt AND in validate_result below).
  * Unknown stays unknown: event geography is None unless a dataset/analyst set it.
  * The output is checked deterministically by ``validate_result`` -- a claim that
    cites evidence which was not supplied, or "quotes" text that is not in it,
    is rejected regardless of which model produced it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

CONTRACT_VERSION = "gim.analysis/1"
CLAIM_TYPES = {
    "REPORTED_FACT",     # stated by >= 1 supplied source; cite them
    "ATTRIBUTED_CLAIM",  # "X says ..."; attributed_to required
    "ASSESSMENT",        # analytic judgement; confidence required
    "CONTEXT",           # background from model knowledge; UNSOURCED, must be verified by an analyst
}
CONFIDENCE_LEVELS = {"low", "moderate", "high"}


def doc_ref(document_id: int) -> str:
    return f"DOC-{int(document_id)}"


def event_ref(event_id: int) -> str:
    return f"EVT-{int(event_id)}"


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvidenceDocument:
    ref: str                          # DOC-123 (permanent)
    source_id: str                    # collection source (registry id)
    url: str                          # original URL
    canonical_url: str
    title: str                        # untrusted text
    summary: str                      # untrusted text
    publisher: str
    original_reporter: str            # '' = not identifiable
    doc_type: str                     # original_reporting | syndicated | official_statement | ...
    source_category: str
    language: str
    published_at: str | None          # UTC ISO
    updated_at: str | None
    collected_at: str
    event_date: str | None
    publisher_country: str
    countries_mentioned: tuple[str, ...]
    content_hash: str                 # detects later edits of the stored copy
    data_origin: str                  # live | fixture
    delivered_by: tuple[str, ...]
    provenance: str                   # JSON string (kept as text so the object stays hashable/immutable)


@dataclass(frozen=True)
class EvidenceExcerpt:
    excerpt_id: str                   # DOC-123#title / DOC-123#summary
    doc_ref: str
    field: str
    text: str                         # verbatim stored text (untrusted)


@dataclass(frozen=True)
class EventGeography:
    event_country: str | None         # None = UNKNOWN (never guessed from publisher location)
    countries_involved: tuple[str, ...] | None
    location_basis: str               # unknown | dataset | analyst | ai_extracted
    countries_mentioned: tuple[str, ...]


@dataclass(frozen=True)
class EventBundle:
    ref: str                          # EVT-45 (permanent)
    headline: str                     # earliest member title (untrusted text)
    first_published_at: str
    last_published_at: str
    clustering_method: str
    geography: EventGeography
    corroboration: str                # JSON of CorroborationSummary.as_dict()
    documents: tuple[EvidenceDocument, ...]
    excerpts: tuple[EvidenceExcerpt, ...]


@dataclass(frozen=True)
class AnalysisRequest:
    request_id: str
    task: str                         # e.g. event_summary | daily_brief | country_report
    created_at: str
    window_start: str
    window_end: str
    events: tuple[EventBundle, ...]
    contract_version: str = CONTRACT_VERSION

    def evidence_index(self) -> dict[str, str]:
        """All citable ids -> text (doc refs map to title + summary)."""
        index: dict[str, str] = {}
        for ev in self.events:
            for d in ev.documents:
                index[d.ref] = f"{d.title}\n{d.summary}"
            for x in ev.excerpts:
                index[x.excerpt_id] = x.text
        return index

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
@dataclass
class Claim:
    text: str
    claim_type: str
    source_refs: list[str] = field(default_factory=list)  # DOC-.. or DOC-..#field
    event_ref: str | None = None
    attributed_to: str | None = None
    confidence: str | None = None
    quote: str | None = None                                # must be verbatim from a cited source


@dataclass
class AnalysisResult:
    request_id: str
    provider: str
    model: str
    claims: list[Claim]
    open_questions: list[str] = field(default_factory=list)
    status: str = "draft"             # draft -> reviewed -> final (human review, Phase 3)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        claims = [Claim(**c) for c in data.get("claims", [])]
        return cls(data["request_id"], data["provider"], data["model"], claims, data.get("open_questions", []))


def validate_result(request: AnalysisRequest, result: AnalysisResult) -> list[str]:
    """Deterministic checks of model output. Empty list = acceptable as a DRAFT."""
    problems: list[str] = []
    if result.request_id != request.request_id:
        problems.append("request_id does not match the request")
    index = request.evidence_index()
    events = {e.ref for e in request.events}
    for i, c in enumerate(result.claims):
        where = f"claim {i + 1}"
        if c.claim_type not in CLAIM_TYPES:
            problems.append(f"{where}: unknown claim_type {c.claim_type!r}")
            continue
        unknown = [r for r in c.source_refs if r not in index]
        if unknown:
            problems.append(f"{where}: cites evidence that was not supplied: {unknown}")
        if c.event_ref and c.event_ref not in events:
            problems.append(f"{where}: unknown event {c.event_ref}")
        if c.claim_type in ("REPORTED_FACT", "ATTRIBUTED_CLAIM") and not c.source_refs:
            problems.append(f"{where}: {c.claim_type} without source_refs")
        if c.claim_type == "ATTRIBUTED_CLAIM" and not c.attributed_to:
            problems.append(f"{where}: ATTRIBUTED_CLAIM without attributed_to")
        if c.claim_type == "ASSESSMENT" and c.confidence not in CONFIDENCE_LEVELS:
            problems.append(f"{where}: ASSESSMENT needs confidence in {sorted(CONFIDENCE_LEVELS)}")
        if c.claim_type == "CONTEXT" and c.source_refs:
            problems.append(f"{where}: CONTEXT is model background knowledge and must not cite sources")
        if c.quote:
            cited = [index[r] for r in c.source_refs if r in index]
            if not any(c.quote in text for text in cited):
                problems.append(f"{where}: quote is not verbatim in the cited sources")
    return problems
