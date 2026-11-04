from datetime import datetime, timezone

from gim.ingest import ingest_regions
from gim.sources.gdelt import GdeltError, parse_artlist
from gim.storage import db

SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_save_and_dedup(tmp_path, gdelt_payload):
    conn = db.connect(tmp_path / "t.sqlite3")
    arts = parse_artlist(gdelt_payload)
    assert db.save_articles(conn, arts, "europe") == 3
    assert db.save_articles(conn, arts, "europe") == 0          # same URLs -> no duplicates
    assert db.save_articles(conn, arts[:1], "middle_east") == 0  # new region link only
    rows = db.query_articles(conn, SINCE)
    assert len(rows) == 3
    tagged = [r for r in rows if "middle_east" in r["regions"]]
    assert len(tagged) == 1 and "europe" in tagged[0]["regions"]


def test_filters(tmp_path, gdelt_payload):
    conn = db.connect(tmp_path / "t.sqlite3")
    db.save_articles(conn, parse_artlist(gdelt_payload), "africa")
    assert len(db.query_articles(conn, SINCE, categories=["humanitarian"])) == 1
    assert len(db.query_articles(conn, SINCE, text="ceasefire")) == 2
    assert len(db.query_articles(conn, SINCE, text="100%_")) == 0  # wildcards escaped
    assert db.query_articles(conn, SINCE, regions=["europe"]) == []
    assert len(db.query_articles(conn, datetime(2026, 11, 4, tzinfo=timezone.utc))) == 2


class StubClient:
    def __init__(self, payload):
        self.payload = payload

    def search_articles(self, query, timespan, max_records):
        if "Sahel" in query:
            raise GdeltError("boom")
        return parse_artlist(self.payload)


def test_ingest_records_runs_and_survives_errors(tmp_path, gdelt_payload):
    conn = db.connect(tmp_path / "t.sqlite3")
    results = ingest_regions(["europe", "africa"], conn=conn, client=StubClient(gdelt_payload))
    assert results[0].new == 3 and results[0].error is None
    assert results[1].error == "boom"
    runs = db.recent_runs(conn)
    assert {r["status"] for r in runs} == {"ok", "error"}
