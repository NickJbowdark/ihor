import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gim.pipeline.corroboration import summarise
from gim.pipeline.events import assign_events
from gim.sources.gdelt import article_to_document, parse_artlist
from gim.sources.registry import get_registry
from gim.sources.rss import parse_feed
from gim.storage import db

FIX = Path(__file__).parent / "fixtures" / "synthetic"
SINCE = datetime(2000, 1, 1, tzinfo=timezone.utc)


def cluster_docs():
    data = json.loads((FIX / "event_cluster.json").read_text("utf-8"))
    return [article_to_document(a, "europe", "fixture") for a in parse_artlist(data)]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.save_documents(c, cluster_docs())
    assign_events(c)
    return c


def _events(conn):
    evs = db.query_events(conn, SINCE)
    return evs, db.event_documents(conn, [e["id"] for e in evs])


def test_many_articles_one_event(conn):
    evs, members = _events(conn)
    by_size = sorted(evs, key=lambda e: -e["documents"])
    meeting = by_size[0]
    assert meeting["documents"] == 5                         # 5 reports of the meeting, ONE event
    assert len(evs) == 3                                     # + earthquake + report 2 weeks earlier
    titles = [d["title"] for d in members[meeting["id"]]]
    assert not any("Earthquake" in t for t in titles)
    methods = {d["link_method"] for d in members[meeting["id"]]}
    assert {"seed", "same_fingerprint", "title_similarity"} <= methods


def test_each_document_keeps_its_own_provenance(conn):
    evs, members = _events(conn)
    docs = max(members.values(), key=len)
    assert len({d["url"] for d in docs}) == len(docs)
    assert {d["publisher"] for d in docs} >= {"reuters.com", "reprint-a.test", "independent-paper.test"}


def test_wire_copies_are_not_independent(conn):
    evs, members = _events(conn)
    s = summarise(max(members.values(), key=len))
    assert s.documents == 5 and s.independent_origins == 3   # Reuters (3 copies) + 2 own reports
    reuters = next(o for o in s.origins if o.label == "Reuters")
    assert len(reuters.document_ids) == 3
    assert s.level == "multiple_origins" and "not verified" in s.label
    assert any("inferred" in c for c in s.caveats)


def test_assignment_is_incremental_and_ids_stable(conn):
    before = {e["id"]: e["documents"] for e in db.query_events(conn, SINCE)}
    assert assign_events(conn)["documents"] == 0              # nothing new -> nothing changes
    extra = cluster_docs()[0]
    extra.url = extra.canonical_url = "https://late-copy.test/geneva"
    extra.publisher = "late-copy.test"
    db.save_documents(conn, [extra])
    stats = assign_events(conn)
    assert stats == {"documents": 1, "new_events": 0, "linked": 1}
    after = {e["id"]: e["documents"] for e in db.query_events(conn, SINCE)}
    assert set(after) == set(before)                          # same event ids
    assert sum(after.values()) == sum(before.values()) + 1


def test_geography_unknown_by_default_and_never_from_publisher(conn):
    for e in db.query_events(conn, SINCE):
        assert e["event_country"] is None and e["countries_involved"] is None
        assert e["location_basis"] == "unknown"
        assert "United Kingdom" not in e["countries_mentioned"]   # publisher country of reuters.com
    meeting = max(db.query_events(conn, SINCE), key=lambda e: e["documents"])
    # 'Geneva' -> Switzerland is MENTIONED (the venue) but is not a party to the talks:
    # exactly why countries_mentioned != countries_involved != event_country.
    assert set(meeting["countries_mentioned"].split(",")) == {"Armenia", "Azerbaijan", "Switzerland"}


def test_set_event_location_requires_basis(conn):
    eid = db.query_events(conn, SINCE)[0]["id"]
    with pytest.raises(ValueError):
        db.set_event_location(conn, eid, event_country="Switzerland", countries_involved=None, basis="unknown")
    db.set_event_location(conn, eid, event_country="Switzerland", countries_involved=["Armenia", "Azerbaijan"],
                          basis="analyst", note="venue stated in all reports")
    e = next(x for x in db.query_events(conn, SINCE) if x["id"] == eid)
    assert (e["event_country"], e["countries_involved"], e["location_basis"]) == \
        ("Switzerland", "Armenia,Azerbaijan", "analyst")


def test_official_only_and_country_conflict():
    reg = get_registry()
    official = parse_feed((FIX / "eu_council_press.xml").read_bytes(), reg["eu_council_press"])
    rows = [{"id": i, **{k: getattr(d, k) for k in ("doc_type", "original_reporter", "publisher",
                                                      "title_fingerprint", "source_id", "title")}}
            for i, d in enumerate(official[:1])]
    s = summarise(rows)
    assert s.level == "official_only" and "not independently established" in s.label


def test_same_headline_different_countries_not_merged(tmp_path):
    from gim.pipeline.events import ClusterDoc, similarity
    t = datetime(2026, 11, 4, tzinfo=timezone.utc)
    a = ClusterDoc(1, t, "Explosion hits fuel depot near Kabul airport", "fa", frozenset({"Afghanistan"}))
    b = ClusterDoc(2, t, "Explosion hits fuel depot near Lagos airport", "fb", frozenset({"Nigeria"}))
    assert similarity(a, b)[0] == 0.0


def test_events_do_not_snowball():
    from datetime import timedelta
    from gim.pipeline.events import MAX_EVENT_SPAN, ClusterDoc, ClusterEvent, choose_event
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    title = "Foreign ministers of Armenia and Azerbaijan meet for peace talks in Geneva"
    member = ClusterDoc(1, t0, title, "fp1", frozenset({"Armenia", "Azerbaijan"}))
    old_event = ClusterEvent(1, t0, t0 + MAX_EVENT_SPAN - timedelta(hours=1), [member])
    late = ClusterDoc(2, t0 + MAX_EVENT_SPAN + timedelta(hours=1), title, "fp1", frozenset({"Armenia", "Azerbaijan"}))
    assert choose_event(late, [old_event])[0] is None          # identical headline, but beyond the 7-day span
    # members far away in time are not used for matching even inside the span
    near = ClusterDoc(3, t0 + timedelta(days=3), title, "fp1", frozenset({"Armenia", "Azerbaijan"}))
    assert choose_event(near, [ClusterEvent(1, t0, t0 + timedelta(days=3), [member])])[0] is None


def test_rich_synthetic_corpus_is_clearly_synthetic_and_clusters_sanely(tmp_path):
    from gim import demo
    demo.build_demo(tmp_path / "rich.db")
    conn = db.connect(tmp_path / "rich.db")
    docs = db.query_documents(conn, SINCE, limit=100000)
    assert all(d["data_origin"] == "fixture" for d in docs)
    assert all(d["title"].startswith(("[SYNTHETIC]", "[TEST FIXTURE]")) for d in docs)
    biggest = conn.execute("SELECT MAX(documents_count), MAX(independent_origins) FROM events").fetchone()
    assert biggest[0] <= 8 and biggest[1] <= 6          # copies of ONE synthetic story, no snowballing
