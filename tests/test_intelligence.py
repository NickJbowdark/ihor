"""Archive search, statistics, watchlists, alerts, reports storage, rendering safety."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from gim import alerts, demo
from gim.analysis.engine import BaselineAnalyst, analyse_event
from gim.analysis.retrieval import related_history
from gim.models import Document
from gim.pipeline.actors import tag_organizations
from gim.pipeline.events import assign_events
from gim.pipeline.normalize import canonicalize_url, title_fingerprint
from gim.reports.builder import generate_report
from gim.reports.render import md_escape, render_markdown
from gim.storage import db, intel

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=400)


@pytest.fixture(scope="module")
def rich(tmp_path_factory):
    path = tmp_path_factory.mktemp("rich") / "demo.db"
    demo.build_demo(path)
    return path


def test_fts_search_and_injection_safety(rich):
    conn = db.connect(rich)
    hits = intel.search_documents(conn, "Geneva peace talks")
    assert hits and all(h["data_origin"] == "fixture" for h in hits)
    assert "Geneva" in hits[0]["title"]
    # FTS operators / quotes in user input must not break or alter the query
    assert intel.fts_query('NEAR(a b) "x" OR * -') == '"near"'          # operators become plain quoted words
    intel.search_documents(conn, 'Sudan" OR title:* NEAR(')          # must not raise
    assert intel.search_documents(conn, "!!! ??") == []


def test_search_events_and_history_no_future_leakage(rich):
    conn = db.connect(rich)
    ev = dict(conn.execute("SELECT * FROM events WHERE headline LIKE '%Armenian and Azerbaijani%' "
                           "ORDER BY first_published_at DESC LIMIT 1").fetchone())
    hist = related_history(conn, ev)
    assert hist, "the earlier Geneva report should be retrieved"
    for h in hist:
        assert h.last_published_at < ev["first_published_at"]
        assert {"Armenia", "Azerbaijan"} & set(h.geography.countries_mentioned)


def test_event_statistics_columns(rich):
    conn = db.connect(rich)
    row = conn.execute("SELECT * FROM events WHERE headline LIKE '%Armenian and Azerbaijani%' "
                       "ORDER BY documents_count DESC LIMIT 1").fetchone()
    assert row["documents_count"] == 5 and row["independent_origins"] == 3
    assert row["corroboration_level"] == "multiple_origins" and row["prominence"] > 0
    assert "europe" in row["regions"] and row["data_origin"] == "fixture"


def test_country_mention_stats_are_mentions(rich):
    conn = db.connect(rich)
    stats = {s["country"]: s for s in intel.country_mention_stats(conn, SINCE)}
    assert stats["Sudan"]["events"] > 0
    assert intel.located_events(conn, SINCE) == []                 # nothing located automatically


def test_organizations():
    assert tag_organizations("ECOWAS and the African Union met; the AU said") == ["ECOWAS", "African Union"]
    assert tag_organizations("they used us") == []


def test_alert_rules(rich):
    conn = db.connect(rich)
    rules = {r["rule"] for r in intel.list_alerts(conn)}
    assert {"volume_spike", "watchlist_match", "official_statement"} <= rules
    spike = next(a for a in intel.list_alerts(conn) if a["rule"] == "volume_spike" and "Sudan" in a["title"])
    crit = json.loads(spike["criteria"])
    assert crit["last_24h"] >= crit["threshold"] and crit["last_24h"] >= 2 * crit["mean"]
    assert spike["data_origin"] == "fixture"
    before = len(intel.list_alerts(conn))
    assert alerts.evaluate(conn) == 0 and len(intel.list_alerts(conn)) == before   # de-duplicated
    intel.acknowledge_alert(conn, spike["id"])
    assert spike["id"] not in {a["id"] for a in intel.list_alerts(conn, unacknowledged=True)}


def test_no_spike_without_baseline(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    docs = []
    for i in range(20):
        url = f"https://x.test/{i}"
        t = f"Sudan story number {i} wording {i}"
        docs.append(Document("gdelt", url, canonicalize_url(url), t, "x.test", "news_discovery", "unknown",
                             published_at=NOW - timedelta(hours=2), countries_mentioned=["Sudan"],
                             title_fingerprint=title_fingerprint(t)))
    db.save_documents(conn, docs)
    assign_events(conn)
    assert alerts.volume_spikes(conn, NOW) == 0                  # < 7 days of history -> no claim of "unusual"


def test_watchlist_validation(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        intel.add_watch(conn, "person", "someone")
    with pytest.raises(ValueError):
        intel.add_watch(conn, "keyword", " ")
    intel.add_watch(conn, "keyword", "grain corridor")
    intel.add_watch(conn, "keyword", "grain corridor")           # idempotent
    assert len(intel.list_watches(conn)) == 1


def test_report_workflow_and_baseline(rich):
    conn = db.connect(rich)
    rid = generate_report(conn, "country_profile", BaselineAnalyst(), countries=["Sudan"], live_only=False)
    rep = intel.get_report(conn, rid)
    content = json.loads(rep["content"])
    assert rep["status"] == "draft" and "NO AI ANALYSIS" in content["limitations"][1]
    assert all("Sudan" in e["countries_mentioned"] for e in content["events"])
    intel.set_report_status(conn, rid, "reviewed", "checked sources")
    assert intel.get_report(conn, rid)["reviewed_at"]
    with pytest.raises(ValueError):
        intel.set_report_status(conn, rid, "published")


def test_cached_analysis_reused(rich):
    conn = db.connect(rich)
    eid = conn.execute("SELECT id FROM events ORDER BY documents_count DESC LIMIT 1").fetchone()[0]
    a1 = analyse_event(conn, eid, BaselineAnalyst())
    a2 = analyse_event(conn, eid, BaselineAnalyst())
    assert a2["cached"] and a1["claims"] == a2["claims"]


def test_markdown_escaping_of_untrusted_text():
    assert md_escape("[click](javascript:x) <img> $5 *x*") == r"\[click\](javascript:x) \<img\> \$5 \*x\*"
    content = {
        "title": "[evil](http://x)", "report_name": "R", "status": "draft", "scope_text": "s",
        "window_start": "2026-01-01T00:00:00+00:00", "window_end": "2026-01-02T00:00:00+00:00",
        "generated_at": "2026-01-02T00:00:00+00:00", "provider": "baseline", "model": "m", "prompt_version": "p",
        "data_origin": "live", "synthesis": [], "open_questions": [], "events": [], "limitations": [],
        "rejected": [], "documents": {}, "statistics": {"events_analysed": 0, "documents": 0,
        "independent_origins": 0, "official_origins": 0, "categories": [], "countries_mentioned": [],
        "organizations": [], "corroboration_levels": []},
    }
    md = render_markdown(content)
    assert "[evil](http://x)" not in md and r"\[evil\]" in md


def test_email_requires_config_and_builds_message(monkeypatch):
    from gim import emailer
    for k in ("GIM_SMTP_HOST", "GIM_EMAIL_FROM", "GIM_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(emailer.EmailNotConfigured):
        emailer.config()
    msg = emailer.build_message("Brief", "body", "a@x.test", ["b@x.test"], b"%PDF-1.4", "r.pdf")
    assert msg["To"] == "b@x.test" and any(p.get_filename() == "r.pdf" for p in msg.iter_attachments())

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host

        def starttls(self, context):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, m):
            sent["subject"] = m["Subject"]

        def quit(self):
            pass

    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("GIM_SMTP_HOST", "smtp.x.test")
    monkeypatch.setenv("GIM_EMAIL_FROM", "gim@x.test")
    monkeypatch.setenv("GIM_EMAIL_TO", "me@x.test")
    monkeypatch.setenv("GIM_SMTP_USER", "u")
    assert emailer.send_report("S", "md", None, "r.pdf") == ["me@x.test"]
    assert sent == {"host": "smtp.x.test", "tls": True, "login": "u", "subject": "S"}
