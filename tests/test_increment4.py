"""Regression tests for the Phase 3.1 increment: engine resilience, evidence selection,
IDF-weighted event matching, report sections, SQL activity aggregate."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from gim import demo
from gim.analysis import engine, prompts
from gim.analysis.bundle import build_event_bundles
from gim.analysis.contracts import AnalysisResult, Claim
from gim.analysis.llm import BudgetExceeded, LLMError
from gim.pipeline.events import ClusterDoc, Idf, event_tokens, similarity
from gim.reports.builder import generate_report
from gim.storage import db, intel

from test_ai_engine import FakeLLM, biggest_event, configure, good_event


@pytest.fixture
def small(tmp_path):
    demo.build_demo(tmp_path / "d.db", rich=False)
    return db.connect(tmp_path / "d.db")


@pytest.fixture(scope="module")
def rich(tmp_path_factory):
    p = tmp_path_factory.mktemp("r") / "d.db"
    demo.build_demo(p)
    return p


def test_failed_repair_keeps_valid_first_answer(small, monkeypatch):
    def script(body, i):
        if i == 0:
            g = good_event(body)
            g["claims"].append({"section": "key_developments", "claim_type": "REPORTED_FACT",
                                "text": "Invented.", "source_refs": ["DOC-999999"]})
            return 200, g
        return 500, None                                     # repair round fails
    fake = FakeLLM(script)
    try:
        configure(monkeypatch, fake.url)
        monkeypatch.setattr("gim.analysis.llm.time.sleep", lambda s: None)
        a = engine.analyse_event(small, biggest_event(small), engine.get_analyst(small)[0])
        assert len(a["claims"]) == 3 and len(a["rejected"]) == 1   # valid claims survive
    finally:
        fake.close()


def test_empty_ai_result_is_not_cached(small, monkeypatch):
    fake = FakeLLM(lambda body, i: (200, {"claims": [], "open_questions": []}))
    try:
        configure(monkeypatch, fake.url)
        analyst = engine.get_analyst(small)[0]
        eid = biggest_event(small)
        engine.analyse_event(small, eid, analyst)
        assert intel.latest_event_analysis(small, eid) is None
    finally:
        fake.close()


class Flaky:
    name, model = "flaky", "test"

    def __init__(self):
        self.calls = 0

    def analyse_event(self, request):
        self.calls += 1
        if self.calls == 1:
            raise LLMError("provider timeout")
        if self.calls == 3:
            raise BudgetExceeded("cap reached")
        return engine.BaselineAnalyst().analyse_event(request)

    def synthesise(self, *a, **kw):
        raise LLMError("synthesis failed")


def test_report_survives_ai_failures(small):
    rid = generate_report(small, "weekly_assessment", Flaky(), live_only=False)
    content = json.loads(intel.get_report(small, rid)["content"])
    lim = " ".join(content["limitations"])
    assert "AI analysis failed" in lim and "budget cap reached" in lim
    assert "AI synthesis failed" not in lim                    # after the cap, synthesis uses the baseline directly
    assert content["events"] and content["synthesis"]          # baseline filled the gaps


class SynthFails(Flaky):
    def analyse_event(self, request):
        return engine.BaselineAnalyst().analyse_event(request)


def test_report_survives_synthesis_failure(small):
    rid = generate_report(small, "weekly_assessment", SynthFails(), live_only=False)
    content = json.loads(intel.get_report(small, rid)["content"])
    assert "AI synthesis failed" in " ".join(content["limitations"]) and content["synthesis"]


def test_evidence_selection_covers_every_origin(rich):
    conn = db.connect(rich)
    eid = conn.execute("SELECT id FROM events WHERE independent_origins + official_origins >= 2 "
                       "ORDER BY documents_count DESC LIMIT 1").fetchone()[0]
    bundle = build_event_bundles(conn, [dict(conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())])[0]
    origins = json.loads(bundle.corroboration)["origins"]
    chosen = {d.ref for d in prompts.select_documents(bundle, len(origins))}
    for o in origins:
        assert {f"DOC-{i}" for i in o["document_ids"]} & chosen, o["label"]
    header = prompts._event_header(bundle)
    assert "ORIGINS" in header and all(o["label"][:20] in header for o in origins)


def test_idf_weighting_prefers_rare_names():
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    common = [ClusterDoc(i, t, f"Minister says talks continue on budget plan {i}", f"c{i}", frozenset())
              for i in range(30)]
    a = ClusterDoc(100, t, "Minister says talks continue in Tbilisi on Abkhazia", "a", frozenset())
    b = ClusterDoc(101, t, "Tbilisi Abkhazia talks: minister optimistic", "b", frozenset())
    c = ClusterDoc(102, t, "Minister says talks continue, pension plan", "c", frozenset())
    idf = Idf([d.tokens for d in common + [a, b, c]])
    assert similarity(a, b, idf)[0] > similarity(a, b)[0]      # rare shared names count more
    # c and a generic headline share only common words: plain Jaccard would merge them, IDF does not
    assert similarity(c, common[0])[0] >= 0.5 and similarity(c, common[0], idf)[0] == 0.0


def test_report_sections_and_changes(rich):
    conn = db.connect(rich)
    rid = generate_report(conn, "country_profile", engine.BaselineAnalyst(), countries=["Sudan"], live_only=False)
    rep = intel.get_report(conn, rid)
    md, content = rep["markdown"], json.loads(rep["content"])
    for heading in ("## Events covered", "## Changes compared with the previous period", "## Source base"):
        assert heading in md
    assert content["changes"][0][0] == "Events in scope"
    assert content["source_base"]["documents"] > 0


def test_key_judgments_block():
    from gim.reports.render import build_blocks
    content = {"title": "t", "report_name": "r", "status": "draft", "scope_text": "s",
               "window_start": "2026-01-01T00:00:00+00:00", "window_end": "2026-01-02T00:00:00+00:00",
               "generated_at": "2026-01-02T00:00:00+00:00", "provider": "x", "model": "m", "prompt_version": "p",
               "data_origin": "live", "open_questions": [], "events": [], "limitations": [], "rejected": [],
               "documents": {}, "statistics": {"events_analysed": 0, "documents": 0, "independent_origins": 0,
               "official_origins": 0, "categories": [], "countries_mentioned": [], "organizations": [],
               "corroboration_levels": []},
               "synthesis": [{"section": "implications", "claim_type": "ASSESSMENT", "confidence": "low", "text": "L"},
                             {"section": "executive_summary", "claim_type": "ASSESSMENT", "confidence": "high", "text": "H"},
                             {"section": "executive_summary", "claim_type": "REPORTED_FACT", "text": "F", "source_refs": []}]}
    blocks = build_blocks(content)
    kinds = [(b.kind, b.text) for b in blocks]
    i = kinds.index(("h1", "Key judgments"))
    assert kinds[i + 1][1].startswith("H") and kinds[i + 2][1].startswith("L")   # high confidence first
    assert sum(1 for k in kinds if k[1].startswith("H")) == 1                   # not repeated below


def test_activity_aggregate_and_region_filter(rich):
    conn = db.connect(rich)
    since = datetime.now(timezone.utc) - timedelta(days=60)
    rows = intel.activity(conn, since)
    total = conn.execute("SELECT COUNT(*) FROM documents WHERE timeline_at >= ?", (db.to_iso(since),)).fetchone()[0]
    assert sum(r["documents"] for r in rows) == total
    africa = db.query_events(conn, since, regions=["africa"])
    assert africa and all("africa" in e["regions"].split(",") for e in africa)
