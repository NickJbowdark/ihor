"""Build an intelligence report: select events -> analyse each (cached) -> synthesise ->
deterministic statistics and bibliography -> store (status 'draft') -> Markdown.

A report is ALWAYS a draft until a human marks it reviewed/final.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from gim.analysis import prompts
from gim.analysis.bundle import build_event_bundles
from gim.analysis.contracts import AnalysisRequest
from gim.analysis.engine import Analyst, BaselineAnalyst, analyse_event
from gim.analysis.llm import BudgetExceeded, LLMError
from gim.analysis.retrieval import related_history
from gim.regions import REGIONS
from gim.reports.render import render_markdown
from gim.reports.specs import SPECS
from gim.storage import db, intel

log = logging.getLogger(__name__)

STANDARD_LIMITATIONS = [
    "Draft generated with machine assistance; it must be reviewed by an analyst before use.",
    "Source base: open sources collected by GIM in the period. Absence of reporting is not evidence of absence.",
    "Corroboration counts describe how many apparently independent ORIGINS reported something; "
    "they do not verify the facts. Wire-agency copies count as one origin.",
    "Official statements document the position of the issuing institution, not the truth of its claims.",
    "Countries 'mentioned' are not event locations. Event locations are shown only when established "
    "by a dataset or an analyst.",
    "Statements marked 'background' come from the model's general knowledge and are UNVERIFIED.",
]


class ReportError(ValueError):
    pass


def select_events(conn, spec, *, since, until, regions, countries, topic, live_only, limit: int | None = None) -> list[dict]:
    origins = ["live"] if live_only else None
    limit = limit or spec.max_events
    if spec.needs == "topic" or topic:
        if not topic:
            raise ReportError("this report type needs a topic (keywords)")
        hits = intel.search_events(conn, topic, after=since, limit=limit * 3, data_origins=origins)
        hits = [h for h in hits if h["first_published_at"] <= db.to_iso(until)]
        if regions:
            hits = [h for h in hits if set((h["regions"] or "").split(",")) & set(regions)]
        if countries:
            hits = [h for h in hits if set((h["countries_mentioned"] or "").split(",")) & set(countries)]
        return sorted(hits, key=lambda h: -h["prominence"])[:limit]
    if spec.needs == "country" and not countries:
        raise ReportError("this report type needs a country")
    if spec.needs == "region" and not regions:
        raise ReportError("this report type needs a region")
    return db.query_events(conn, since, until, regions=regions or None, countries=countries or None,
                           categories=list(spec.categories) or None, data_origins=origins,
                           order="prominence", limit=limit)


def _changes(conn, spec, *, since, until, regions, countries, topic, live_only) -> dict:
    """Deterministic change data: this period vs the previous period of equal length, same scope."""
    span = until - since
    kw = dict(regions=regions, countries=countries, topic=topic, live_only=live_only, limit=2000)
    try:
        now_ev = select_events(conn, spec, since=since, until=until, **kw)
        prev_ev = select_events(conn, spec, since=since - span, until=since, **kw)
    except ReportError:
        return {"rows": [], "lines": []}
    prev_ev = [e for e in prev_ev if e["first_published_at"] < db.to_iso(since)]

    def count(evts, key):
        return Counter(x for e in evts for x in (e[key] or "").split(",") if x)

    rows, lines = [], []
    n, p = len(now_ev), len(prev_ev)
    rows.append(["Events in scope", n, p, _pct(n, p)])
    lines.append(f"Events in scope: {n} this period vs {p} in the previous period ({_pct(n, p)}).")
    for label, key, top in (("Topic", "categories", 6), ("Country mentioned", "countries_mentioned", 8)):
        c_now, c_prev = count(now_ev, key), count(prev_ev, key)
        movers = sorted(set(c_now) | set(c_prev), key=lambda k: -(abs(c_now[k] - c_prev[k]) + c_now[k] / 100))
        for k in movers[:top]:
            if k in countries:
                continue
            rows.append([f"{label}: {k}", c_now[k], c_prev[k], _pct(c_now[k], c_prev[k])])
            if c_now[k] != c_prev[k]:
                lines.append(f"{label} '{k}': {c_now[k]} events vs {c_prev[k]} ({_pct(c_now[k], c_prev[k])}).")
    return {"rows": rows, "lines": lines[:14]}


def _pct(now: int, prev: int) -> str:
    if prev == 0:
        return "new" if now else "—"
    return f"{(now - prev) / prev:+.0%}"


def _source_base(bundles) -> dict:
    docs = [d for b in bundles for d in b.documents]
    types = Counter(d.doc_type for d in docs)
    officials = Counter(d.original_reporter or d.publisher for d in docs if d.doc_type == "official_statement")
    return {"documents": len(docs), "publishers": len({d.publisher for d in docs}),
            "languages": sorted({d.language for d in docs if d.language}),
            "doc_types": types.most_common(), "official_institutions": officials.most_common(8),
            "collection_sources": Counter(s for d in docs for s in d.delivered_by).most_common()}


def _statistics(conn, events: list[dict], since, until, countries, regions) -> dict:
    cat = Counter(c for e in events for c in (e["categories"] or "").split(",") if c)
    ctry = Counter(c for e in events for c in (e["countries_mentioned"] or "").split(",") if c)
    orgs = Counter(o for e in events for o in (e["organizations"] or "").split(",") if o)
    days = max(1, int((until - since).total_seconds() // 86400))
    series = intel.daily_counts(conn, since, country=countries[0] if countries else None,
                                region=regions[0] if regions and not countries else None)
    return {
        "events_analysed": len(events),
        "documents": sum(e["documents_count"] for e in events),
        "independent_origins": sum(e["independent_origins"] for e in events),
        "official_origins": sum(e["official_origins"] for e in events),
        "categories": cat.most_common(), "countries_mentioned": ctry.most_common(15),
        "organizations": orgs.most_common(10), "period_days": days, "daily_documents": series,
        "corroboration_levels": Counter(e["corroboration_level"] for e in events).most_common(),
    }


def generate_report(conn, report_type: str, analyst: Analyst, *, regions: list[str] | None = None,
                    countries: list[str] | None = None, topic: str | None = None, days: int | None = None,
                    live_only: bool = True, now: datetime | None = None, notes: list[str] | None = None,
                    progress=None) -> int:
    """Generate and store a report. Returns its id. ``progress(i, n, text)`` is an optional callback."""
    spec = SPECS.get(report_type)
    if not spec:
        raise ReportError(f"unknown report type {report_type!r}; choose from {sorted(SPECS)}")
    regions, countries = list(regions or []), list(countries or [])
    unknown = [r for r in regions if r not in REGIONS and r != "global"]
    if unknown:
        raise ReportError(f"unknown region(s): {unknown}")
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=days or spec.default_days)
    events = select_events(conn, spec, since=since, until=until, regions=regions, countries=countries,
                           topic=topic, live_only=live_only)
    if not events:
        raise ReportError("no events match this scope and period -- collect data first or widen the period")

    # 1) per-event analyses (cached). An AI failure never aborts the report: the affected event
    #    falls back to the deterministic baseline and the report says so.
    analyses, rejected_total, run_notes = {}, [], []
    baseline = BaselineAnalyst()
    active = analyst
    for i, e in enumerate(events, 1):
        if progress:
            progress(i, len(events) + 1, f"Analysing EVT-{e['id']}: {e['headline'][:70]}")
        try:
            a = analyse_event(conn, e["id"], active)
        except BudgetExceeded as exc:
            run_notes.append(f"AI budget cap reached during generation ({exc}); remaining events use the "
                             "non-AI baseline.")
            active = baseline
            a = analyse_event(conn, e["id"], baseline)
        except LLMError as exc:
            run_notes.append(f"EVT-{e['id']}: AI analysis failed ({str(exc)[:120]}); baseline used for this event.")
            a = analyse_event(conn, e["id"], baseline)
        analyses[f"EVT-{e['id']}"] = a
        rejected_total += [{**r, "event_ref": f"EVT-{e['id']}"} for r in a.get("rejected", [])]
    changes = _changes(conn, spec, since=since, until=until, regions=regions, countries=countries,
                       topic=topic, live_only=live_only)

    # 2) synthesis over all events (+ archive for the scope)
    bundles = build_event_bundles(conn, events)
    scope_query = " ".join(filter(None, [topic or "", " ".join(countries),
                                         " ".join(REGIONS[r].name for r in regions if r in REGIONS)]))
    archive = related_history(conn, {**events[0], "first_published_at": db.to_iso(since), "id": None},
                              k=5, query=scope_query or events[0]["headline"])
    scope = {"report_type": report_type, "regions": regions, "countries": countries, "topic": topic or ""}
    request = AnalysisRequest(request_id=f"rep-{uuid.uuid4().hex[:10]}", task=report_type,
                              created_at=db.utcnow_iso(), window_start=db.to_iso(since), window_end=db.to_iso(until),
                              events=bundles, archive=archive, scope=json.dumps(scope))
    if progress:
        progress(len(events) + 1, len(events) + 1, "Writing synthesis")
    scope_text = ", ".join(filter(None, [", ".join(REGIONS[r].name for r in regions if r in REGIONS),
                                         ", ".join(countries), topic and f"topic: {topic}"])) or "global"
    try:
        synth = active.synthesise(request, f"{spec.purpose} Scope: {scope_text}.",
                                  {ref: a["claims"] for ref, a in analyses.items()}, changes["lines"])
    except LLMError as exc:   # includes BudgetExceeded
        run_notes.append(f"AI synthesis failed ({str(exc)[:120]}); summary built by the non-AI baseline.")
        synth = baseline.synthesise(request, spec.purpose, {}, changes["lines"])
    rejected_total += synth.rejected
    providers = {a["provider"] for a in analyses.values()} | {synth.provider}
    report_provider, report_model = ("baseline", "deterministic") if providers == {"baseline"} \
        else (analyst.name, analyst.model)

    origins = {e["data_origin"] for e in events}
    data_origin = origins.pop() if len(origins) == 1 else "mixed"
    title = synth.title or f"{spec.name} — {scope_text}"
    limitations = list(STANDARD_LIMITATIONS)
    if report_provider == "baseline":
        limitations.insert(0, "NO AI ANALYSIS: no AI provider was configured or available; this report contains "
                              "attributed source statements and statistics only.")
    if data_origin != "live":
        limitations.insert(0, "SYNTHETIC DATA: this report was generated from test fixtures, NOT real intelligence.")
    limitations += (notes or []) + run_notes
    if rejected_total:
        limitations.append(f"{len(rejected_total)} model statement(s) were rejected by the evidence validator "
                           "and are excluded (listed in the appendix).")

    content = {
        "report_type": report_type, "report_name": spec.name, "title": title, "scope": scope,
        "scope_text": scope_text, "window_start": db.to_iso(since), "window_end": db.to_iso(until),
        "generated_at": db.utcnow_iso(), "provider": report_provider, "model": report_model,
        "prompt_version": prompts.PROMPT_VERSION, "data_origin": data_origin, "status": "draft",
        "synthesis": [asdict(c) for c in synth.claims], "open_questions": synth.open_questions,
        "events": [_event_entry(e, b, analyses[b.ref]) for e, b in zip(events, bundles)],
        "archive": [{"ref": a.ref, "headline": a.headline, "first_published_at": a.first_published_at}
                    for a in archive],
        "statistics": _statistics(conn, events, since, until, countries, regions),
        "changes": changes["rows"], "source_base": _source_base(bundles),
        "limitations": limitations, "rejected": rejected_total,
        "documents": _document_index(bundles + archive),
    }
    markdown = render_markdown(content)
    return intel.save_report(conn, report_type=report_type, title=title, scope=scope,
                             window_start=content["window_start"], window_end=content["window_end"],
                             provider=report_provider, model=report_model, content=content, markdown=markdown,
                             data_origin=data_origin)


def _event_entry(e: dict, bundle, analysis: dict) -> dict:
    corr = json.loads(bundle.corroboration)
    return {
        "ref": bundle.ref, "headline": e["headline"], "first_published_at": e["first_published_at"],
        "last_published_at": e["last_published_at"], "countries_mentioned": e["countries_mentioned"],
        "organizations": e["organizations"], "categories": e["categories"],
        "event_location": e["event_country"] or "unknown", "location_basis": e["location_basis"],
        "corroboration": corr["label"], "corroboration_level": corr["level"], "caveats": corr["caveats"], "prominence": e["prominence"],
        "documents_count": e["documents_count"], "claims": analysis["claims"],
        "open_questions": analysis.get("open_questions", []), "archive_refs": analysis.get("archive_refs", []),
        "source_refs": [d.ref for d in bundle.documents],
    }


def _document_index(bundles) -> dict[str, dict]:
    out = {}
    for b in bundles:
        for d in b.documents:
            out[d.ref] = {"title": d.title, "publisher": d.publisher, "original_reporter": d.original_reporter,
                          "doc_type": d.doc_type, "published_at": d.published_at, "url": d.url,
                          "data_origin": d.data_origin, "event_ref": b.ref}
    return out
