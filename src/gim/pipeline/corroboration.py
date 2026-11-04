"""Source independence and corroboration -- WITHOUT per-organisation scores.

For one event we ask: how many *independent origins* report it, and of what
kind? An origin is the organisation that actually produced the information:

  * syndicated copy        -> the wire agency (Reuters, AP, AFP...), not the reprinting site
  * official statement     -> the issuing institution
  * identical headline to a document whose agency IS known -> that agency
    (inferred; flagged in the caveats)
  * identical headline on several sites with unknown origin -> ONE shared origin
    (verbatim reuse indicates a common source even without an agency marker)
  * otherwise              -> the publisher

The result is a *description* (counts + a label + explicit caveats), never a
truth score. "Reported by 3 independent origins" is evidence of independent
reporting, not proof of the facts; an official statement establishes what the
institution SAYS, not that its claims are true.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

OFFICIAL = "official_statement"


@dataclass
class Origin:
    key: str                     # stable label, e.g. 'agency:Reuters', 'publisher:bbc.co.uk'
    label: str                   # human readable
    kind: str                    # wire_agency | official | shared_copy | publisher | dataset
    document_ids: list[int] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)


@dataclass
class CorroborationSummary:
    documents: int
    publishers: int
    origins: list[Origin]
    independent_origins: int          # distinct non-official origins
    official_origins: int
    level: str                        # official_only | single_origin | multiple_origins | official_plus_reporting
    label: str
    caveats: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "origins": [o.__dict__ for o in self.origins]}


def origin_for(doc: dict[str, Any], shared_fingerprints: set[str],
               agency_by_fingerprint: dict[str, str] | None = None) -> tuple[str, str, str]:
    doc_type = doc.get("doc_type") or "unknown"
    reporter = (doc.get("original_reporter") or "").strip()
    publisher = (doc.get("publisher") or "").strip() or "unknown publisher"
    if doc_type == OFFICIAL:
        org = reporter or publisher
        return f"official:{org}", org, "official"
    if doc_type == "dataset_record":
        return f"dataset:{doc.get('source_id')}", publisher, "dataset"
    if reporter:  # syndicated copy, or the agency's own site
        return f"agency:{reporter}", reporter, "wire_agency"
    fp = doc.get("title_fingerprint") or ""
    if agency_by_fingerprint and fp in agency_by_fingerprint:
        agency = agency_by_fingerprint[fp]
        return f"agency:{agency}", agency, "wire_agency_inferred"
    if fp in shared_fingerprints:
        return f"copy:{fp}", f"shared copy ({doc.get('title', '')[:50]}…)", "shared_copy"
    return f"publisher:{publisher}", publisher, "publisher"


def summarise(docs: list[dict[str, Any]]) -> CorroborationSummary:
    """docs: rows with id, doc_type, original_reporter, publisher, title_fingerprint, source_id, title."""
    publishers_by_fp: dict[str, set[str]] = defaultdict(set)
    agency_by_fp: dict[str, str] = {}
    for d in sorted(docs, key=lambda x: x["id"]):
        if d.get("original_reporter") and d.get("doc_type") in ("original_reporting", "syndicated"):
            agency_by_fp.setdefault(d.get("title_fingerprint") or "", d["original_reporter"])
    for d in docs:
        if d.get("doc_type") in ("unknown", "original_reporting", "syndicated") and not d.get("original_reporter"):
            publishers_by_fp[d.get("title_fingerprint") or ""].add(d.get("publisher") or "")
    shared = {fp for fp, pubs in publishers_by_fp.items() if fp and len(pubs) > 1}

    origins: dict[str, Origin] = {}
    inferred_docs: list[int] = []
    for d in sorted(docs, key=lambda x: x["id"]):
        key, label, kind = origin_for(d, shared, agency_by_fp)
        inferred = kind == "wire_agency_inferred"
        kind = "wire_agency" if inferred else kind
        if inferred:
            inferred_docs.append(d["id"])
        o = origins.setdefault(key, Origin(key, label, kind))
        o.document_ids.append(d["id"])
        pub = d.get("publisher") or ""
        if pub and pub not in o.publishers:
            o.publishers.append(pub)

    official = [o for o in origins.values() if o.kind == "official"]
    independent = [o for o in origins.values() if o.kind != "official"]
    caveats = []
    copies = sum(len(o.document_ids) - 1 for o in origins.values())
    if copies:
        caveats.append(f"{copies} document(s) are copies of another origin and add no independent confirmation.")
    if inferred_docs:
        caveats.append(f"{len(inferred_docs)} document(s) carry an agency's exact headline without naming it; "
                       "attributed to that agency (inferred).")
    if any(o.kind == "shared_copy" for o in origins.values()):
        caveats.append("Identical headlines on different sites were treated as ONE origin (likely common source).")
    if len(independent) >= 2 and any(o.kind == "publisher" for o in independent):
        caveats.append("Independence of different publishers is assumed, not verified: they may rely on the same "
                       "unnamed source or on each other.")

    if official and not independent:
        level, label = "official_only", "Official statement(s) only — the position is documented, the facts are not independently established."
    elif len(independent) == 1 and not official:
        level, label = "single_origin", "Single origin — not corroborated."
    elif len(independent) == 1:
        level, label = "official_plus_reporting", "Official statement plus one reporting origin."
    else:
        level = "multiple_origins"
        label = f"Reported by {len(independent)} apparently independent origins (independence not verified)."
        if official:
            label += f" Plus {len(official)} official source(s)."
    return CorroborationSummary(
        documents=len(docs), publishers=len({d.get('publisher') for d in docs}),
        origins=list(origins.values()), independent_origins=len(independent), official_origins=len(official),
        level=level, label=label, caveats=caveats,
    )
