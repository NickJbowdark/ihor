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
# Sections of an EVENT analysis and of a REPORT synthesis.
EVENT_SECTIONS = ("significance", "key_developments", "actors", "background", "source_comparison",
                  "implications", "uncertainty", "indicators")
REPORT_SECTIONS = ("executive_summary", "regional_overview", "trends", "implications", "indicators", "uncertainty")
ALL_SECTIONS = set(EVENT_SECTIONS) | set(REPORT_SECTIONS)


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
    archive: tuple[EventBundle, ...] = ()   # earlier events retrieved from the GIM archive (RAG)
    scope: str = "{}"                       # JSON: report type, regions, countries, topic

    def evidence_index(self) -> dict[str, str]:
        """All citable ids -> text (doc refs map to title + summary). Includes archive evidence."""
        index: dict[str, str] = {}
        for ev in self.events + self.archive:
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
    section: str = "key_developments"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        """Tolerant constructor for model output: unknown keys are ignored, types coerced."""
        refs = data.get("source_refs") or []
        if isinstance(refs, str):
            refs = [refs]
        return cls(
            text=str(data.get("text") or "").strip(),
            claim_type=str(data.get("claim_type") or "").strip().upper(),
            source_refs=[str(r).strip() for r in refs if str(r).strip()],
            event_ref=(str(data["event_ref"]).strip() if data.get("event_ref") else None),
            attributed_to=(str(data["attributed_to"]).strip() if data.get("attributed_to") else None),
            confidence=(str(data["confidence"]).strip().lower() if data.get("confidence") else None),
            quote=(str(data["quote"]) if data.get("quote") else None),
            section=str(data.get("section") or "key_developments").strip().lower(),
        )


@dataclass
class AnalysisResult:
    request_id: str
    provider: str
    model: str
    claims: list[Claim]
    open_questions: list[str] = field(default_factory=list)
    status: str = "draft"             # draft -> reviewed -> final (human review)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # statements the validator refused
    title: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        claims = [Claim.from_dict(c) for c in data.get("claims", []) if isinstance(c, dict)]
        return cls(data["request_id"], data["provider"], data["model"], claims, data.get("open_questions", []))


def claim_problems(claim: Claim, index: dict[str, str], events: set[str]) -> list[str]:
    """Deterministic checks for ONE claim. Empty list = acceptable."""
    c = claim
    problems: list[str] = []
    if not c.text or len(c.text) > 2000:
        problems.append("empty or overlong text")
    if c.claim_type not in CLAIM_TYPES:
        return problems + [f"unknown claim_type {c.claim_type!r}"]
    if c.section not in ALL_SECTIONS:
        problems.append(f"unknown section {c.section!r}")
    unknown = [r for r in c.source_refs if r not in index]
    if unknown:
        problems.append(f"cites evidence that was not supplied: {unknown}")
    if c.event_ref and c.event_ref not in events:
        problems.append(f"unknown event {c.event_ref}")
    if c.claim_type in ("REPORTED_FACT", "ATTRIBUTED_CLAIM") and not c.source_refs:
        problems.append(f"{c.claim_type} without source_refs")
    if c.claim_type == "ATTRIBUTED_CLAIM" and not c.attributed_to:
        problems.append("ATTRIBUTED_CLAIM without attributed_to")
    if c.claim_type == "ASSESSMENT" and c.confidence not in CONFIDENCE_LEVELS:
        problems.append(f"ASSESSMENT needs confidence in {sorted(CONFIDENCE_LEVELS)}")
    if c.claim_type == "CONTEXT" and c.source_refs:
        problems.append("CONTEXT is model background knowledge and must not cite sources")
    if c.quote:
        cited = [index[r] for r in c.source_refs if r in index]
        if not any(c.quote in text for text in cited):
            problems.append("quote is not verbatim in the cited sources")
    return problems


def validate_result(request: AnalysisRequest, result: AnalysisResult) -> list[str]:
    """Deterministic checks of model output. Empty list = acceptable as a DRAFT."""
    problems: list[str] = []
    if result.request_id != request.request_id:
        problems.append("request_id does not match the request")
    index = request.evidence_index()
    events = {e.ref for e in request.events + request.archive}
    for i, c in enumerate(result.claims):
        problems += [f"claim {i + 1}: {p}" for p in claim_problems(c, index, events)]
    return problems


def split_valid(request: AnalysisRequest, claims: list[Claim]) -> tuple[list[Claim], list[dict[str, Any]]]:
    """(accepted claims, rejected [{claim, problems}]) -- rejected claims are kept for transparency."""
    index = request.evidence_index()
    events = {e.ref for e in request.events + request.archive}
    ok, rejected = [], []
    for c in claims:
        p = claim_problems(c, index, events)
        if p:
            rejected.append({"claim": asdict(c), "problems": p})
        else:
            ok.append(c)
    return ok, rejected
