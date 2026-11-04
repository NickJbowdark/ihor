import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gim import collect
from gim.sources.base import Connector, FetchResult
from gim.sources.registry import get_registry
from gim.sources.rss import parse_feed
from gim.storage import db

FIX = Path(__file__).parent / "fixtures" / "synthetic"
REG = get_registry()
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _un_docs():
    return parse_feed((FIX / "un_news.xml").read_bytes(), REG["un_news"])


def test_save_documents_dedup_merge_and_revision(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    docs = _un_docs()
    assert db.save_documents(conn, docs) == (3, 0)
    assert db.save_documents(conn, docs) == (0, 0)
    # Same source corrects a headline -> revision kept
    docs[0].title = docs[0].title + " (updated)"
    assert db.save_documents(conn, docs[:1]) == (0, 1)
    row = db.query_documents(conn, SINCE, text="(updated)")[0]
    assert row["revisions"] == 1
    old = conn.execute("SELECT title FROM document_revisions").fetchone()[0]
    assert not old.endswith("(updated)")


def test_same_url_from_two_sources_is_one_document(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    rss_doc = _un_docs()[0]
    gdelt_copy = parse_feed((FIX / "un_news.xml").read_bytes(), REG["un_news"])[0]
    gdelt_copy.source_id, gdelt_copy.summary, gdelt_copy.doc_type = "gdelt", "", "unknown"
    db.save_documents(conn, [gdelt_copy])
    db.save_documents(conn, [rss_doc])
    rows = db.query_documents(conn, SINCE)
    assert len(rows) == 1
    assert set(rows[0]["delivered_by"].split(",")) == {"gdelt", "un_news"}
    assert rows[0]["summary"] and rows[0]["doc_type"] == "official_statement"  # enriched


def test_query_filters(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.save_documents(conn, _un_docs())
    council = parse_feed((FIX / "eu_council_press.xml").read_bytes(), REG["eu_council_press"])
    db.save_documents(conn, council)
    assert len(db.query_documents(conn, SINCE, source_ids=["eu_council_press"])) == 3
    assert len(db.query_documents(conn, SINCE, source_categories=["official_un"])) == 3
    assert len(db.query_documents(conn, SINCE, regions=["africa"])) == 2   # Sudan + Sahel
    assert len(db.query_documents(conn, SINCE, text="Darfur")) == 1         # summary is searched
    assert db.query_documents(conn, SINCE, data_origins=["fixture"]) == []


def test_migration_from_v1(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE articles (id INTEGER PRIMARY KEY, canonical_url TEXT UNIQUE, url TEXT, title TEXT,
            title_fingerprint TEXT, domain TEXT, language TEXT, source_country TEXT, seen_at TEXT,
            first_ingested_at TEXT, last_ingested_at TEXT, source TEXT, categories TEXT);
        CREATE TABLE article_regions (article_id INTEGER, region TEXT, PRIMARY KEY(article_id, region));
        CREATE TABLE ingest_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, source TEXT,
            region TEXT, query TEXT, timespan TEXT, fetched INTEGER, new INTEGER, status TEXT, error TEXT);
        INSERT INTO articles VALUES (1, 'https://a.test/x', 'https://a.test/x', 'Old headline', 'fp', 'a.test',
            'English', 'France', '2026-10-01T00:00:00+00:00', '2026-10-01T01:00:00+00:00',
            '2026-10-01T01:00:00+00:00', 'gdelt', 'political');
        INSERT INTO article_regions VALUES (1, 'europe');
        INSERT INTO ingest_runs VALUES (1, 'x', 'y', 'gdelt', 'europe', 'q', '24h', 1, 1, 'ok', NULL);
        PRAGMA user_version = 1;
    """)
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    rows = db.query_documents(conn, SINCE, regions=["europe"])
    assert len(rows) == 1 and rows[0]["title"] == "Old headline" and rows[0]["publisher"] == "a.test"
    assert json.loads(rows[0]["provenance"])["migrated_from"] == "v1"
    assert db.recent_runs(conn)[0]["data_origin"] == "live"
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='articles'").fetchone()


class StubConnector(Connector):
    def __init__(self, spec, docs=None, exc=None):
        super().__init__(spec, http=None)
        self.docs, self.exc = docs or [], exc

    def fetch(self, state):
        if self.exc:
            raise self.exc
        return FetchResult(documents=self.docs, raw=[("feed.xml", b"<rss/>")])


def test_run_source_health_transitions(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    spec = REG["un_news"]
    r = collect.run_source(spec, conn, None, connector=StubConnector(spec, _un_docs()))
    assert (r.status, r.fetched, r.new) == ("ok", 3, 3)
    assert collect.health_label(spec, db.get_source_status(conn)["un_news"]) == "ok"
    for _ in range(3):
        r = collect.run_source(spec, conn, None, connector=StubConnector(spec, exc=RuntimeError("HTTP 503")))
    row = db.get_source_status(conn)["un_news"]
    assert r.status == "error" and row["status"] == "failing" and row["consecutive_failures"] == 3
    assert "503" in row["last_error"] and row["last_success_at"]          # last success still known
    assert {x["status"] for x in db.recent_runs(conn)} == {"ok", "error"}


def test_dry_run_and_record_store_nothing(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    spec = REG["un_news"]
    r = collect.run_source(spec, conn, None, dry_run=True, record=True,
                           connector=StubConnector(spec, _un_docs()), record_dir=tmp_path / "rec")
    assert r.status == "ok" and "DRY RUN" in r.message
    assert db.query_documents(conn, SINCE) == [] and db.get_source_status(conn) == {}
    assert len(list((tmp_path / "rec").iterdir())) == 1


def test_due_and_planned(tmp_path):
    spec = REG["un_news"]
    assert collect.is_due(spec, None)
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert not collect.is_due(spec, {"last_attempt_at": "2026-11-04T11:50:00+00:00"}, now)
    assert collect.is_due(spec, {"last_attempt_at": "2026-11-04T11:00:00+00:00"}, now)
    conn = db.connect(tmp_path / "t.db")
    assert collect.run_source(REG["acled"], conn, None).status == "skipped"
    assert collect.health_label(REG["acled"], None) == "planned"


def test_demo_builder(tmp_path, monkeypatch):
    from gim import demo
    n = demo.build_demo(tmp_path / "demo.db")
    conn = db.connect(tmp_path / "demo.db")
    rows = db.query_documents(conn, datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert n == len(rows) > 10
    assert {r["data_origin"] for r in rows} == {"fixture"}
    assert all(r["title"].startswith("[TEST FIXTURE]") for r in rows)
