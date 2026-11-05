"""Regression tests for migration safety, UTC handling, rate limits, caching, locking, recovery."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gim import collect, maintenance
from gim.sources.base import Connector, FetchResult
from gim.sources.http import FetchError, PoliteHttp, parse_retry_after
from gim.sources.registry import get_registry
from gim.sources.rss import parse_feed
from gim.storage import db

FIX = Path(__file__).parent / "fixtures" / "synthetic"
REG = get_registry()
SINCE = datetime(2000, 1, 1, tzinfo=timezone.utc)


def un_docs():
    return parse_feed((FIX / "un_news.xml").read_bytes(), REG["un_news"])


class Stub(Connector):
    def __init__(self, spec, result=None, exc=None):
        super().__init__(spec, None)
        self.result, self.exc = result, exc

    def fetch(self, state):
        self.state = state
        if self.exc:
            raise self.exc
        return self.result


# --- migrations ---------------------------------------------------------------
def _make_v2(path):
    conn = sqlite3.connect(path)
    for stmt in db._statements(db.SCHEMA_V2):
        conn.execute(stmt)
    conn.execute("""INSERT INTO documents (source_id, canonical_url, url, title, title_fingerprint, content_hash,
                     timeline_at, collected_at, last_seen_at, source_country, countries)
                    VALUES ('un_news','https://a.test/1','https://a.test/1','T','fp','h','2026-10-01T00:00:00+00:00',
                            '2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','France','Sudan,Chad')""")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def test_v2_to_v3_migration_keeps_data_and_backs_up(tmp_path):
    path = tmp_path / "gim.sqlite3"
    _make_v2(path)
    conn = db.connect(path, backup_dir=tmp_path / "bk")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT publisher_country, countries_mentioned FROM documents").fetchone()
    assert tuple(row) == ("France", "Sudan,Chad")
    backups = list((tmp_path / "bk").glob(f"*-pre-v{db.SCHEMA_VERSION}-from-v2-*.sqlite3"))
    assert len(backups) == 1
    assert sqlite3.connect(backups[0]).execute("PRAGMA user_version").fetchone()[0] == 2


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    path = tmp_path / "gim.sqlite3"
    _make_v2(path)
    monkeypatch.setattr(db, "V2_TO_V3", db.V2_TO_V3 + "\nTHIS IS NOT SQL;\n")
    with pytest.raises(sqlite3.OperationalError):
        db.connect(path, backup_dir=tmp_path / "bk")
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    assert "source_country" in cols and "publisher_country" not in cols   # rename rolled back
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone()


def test_fresh_database_needs_no_backup(tmp_path):
    db.connect(tmp_path / "new.sqlite3", backup_dir=tmp_path / "bk")
    assert not (tmp_path / "bk").exists()


# --- UTC ------------------------------------------------------------------------
def test_timestamps_are_normalised_to_utc(tmp_path):
    cet = timezone(timedelta(hours=1))
    assert db.to_iso(datetime(2026, 11, 4, 10, 0, tzinfo=cet)) == "2026-11-04T09:00:00+00:00"
    assert db.to_iso(datetime(2026, 11, 4, 10, 0)) == "2026-11-04T10:00:00+00:00"   # naive = UTC by convention
    doc = un_docs()[0]
    doc.published_at = datetime.now(timezone.utc) + timedelta(days=2)               # broken source clock
    conn = db.connect(tmp_path / "t.db")
    db.save_documents(conn, [doc])
    row = db.query_documents(conn, SINCE)[0]
    assert row["published_at"] > row["timeline_at"]          # stated value kept, sorted by collection time
    assert '"published_at_in_future": true' in row["provenance"]


# --- 429 / Retry-After ------------------------------------------------------------
def test_parse_retry_after():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert parse_retry_after("30") == 30
    assert parse_retry_after("Wed, 04 Nov 2026 12:10:00 GMT", now) == 600
    assert parse_retry_after("garbage") is None and parse_retry_after(None) is None


class Resp:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}

    def iter_content(self, n):
        return iter([b""])

    def close(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses, self.headers, self.slept = list(responses), {}, []

    def request(self, *a, **kw):
        return self.responses.pop(0)


def test_long_retry_after_aborts_and_sets_backoff(tmp_path):
    slept = []
    http = PoliteHttp(session=Session([Resp(429, {"Retry-After": "3600"})]), sleep=slept.append)
    with pytest.raises(FetchError) as exc:
        http.get("https://example.test/feed")
    assert exc.value.kind == "rate_limited" and exc.value.retry_after_s == 3600
    assert not [s for s in slept if s > 100]                  # did not block for an hour

    conn = db.connect(tmp_path / "t.db")
    spec = REG["un_news"]
    collect.run_source(spec, conn, None, connector=Stub(spec, exc=exc.value))
    row = db.get_source_status(conn)["un_news"]
    assert row["last_error_kind"] == "rate_limited" and row["backoff_until"]
    assert not collect.is_due(spec, row)
    assert collect.is_due(spec, row, now=datetime.now(timezone.utc) + timedelta(hours=2))


def test_short_retry_after_is_slept_then_retried():
    slept = []
    http = PoliteHttp(session=Session([Resp(503, {"Retry-After": "7"}), Resp(304)]), sleep=slept.append)
    assert http.get("https://example.test/feed").not_modified
    assert 7 in slept


def test_failures_back_off_exponentially():
    spec = REG["un_news"]   # 30 min poll
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    row = {"last_attempt_at": "2026-11-04T11:00:00+00:00", "consecutive_failures": 0}
    assert collect.is_due(spec, row, now)
    row["consecutive_failures"] = 2                            # 30 * 4 = 120 min
    assert not collect.is_due(spec, row, now)
    assert collect.is_due(spec, row, now + timedelta(hours=1))


# --- ETag / Last-Modified ------------------------------------------------------------
def test_validators_stored_replaced_and_kept_on_304(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    spec = REG["un_news"]
    collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult(un_docs(), etag='"v1"', last_modified="L1")))
    assert db.get_source_status(conn)["un_news"]["etag"] == '"v1"'
    stub = Stub(spec, FetchResult(not_modified=True, etag='"v1"', last_modified="L1"))
    r = collect.run_source(spec, conn, None, connector=stub)
    assert r.status == "not_modified" and stub.state.etag == '"v1"'          # validator was sent
    row = db.get_source_status(conn)["un_news"]
    assert row["etag"] == '"v1"' and row["last_result"] == "not_modified"
    collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult(un_docs(), etag=None)))
    assert db.get_source_status(conn)["un_news"]["etag"] is None             # server stopped sending: cleared


# --- failure recovery / no data loss -------------------------------------------------
def test_failing_source_never_loses_stored_documents(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    spec = REG["un_news"]
    collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult(un_docs())))
    before = len(db.query_documents(conn, SINCE))
    for exc in (FetchError("HTTP 503", 503), ValueError("bad json"), RuntimeError("boom")):
        r = collect.run_source(spec, conn, None, connector=Stub(spec, exc=exc))
        assert r.status == "error"
    assert len(db.query_documents(conn, SINCE)) == before == 3
    row = db.get_source_status(conn)["un_news"]
    assert row["status"] == "failing" and row["last_success_at"] and row["last_error_kind"] == "unexpected"
    kinds = [x["error_kind"] for x in db.recent_runs(conn)[:3]]
    assert kinds == ["unexpected", "parse", "http_5xx"]


def test_empty_is_not_failure_but_tracked(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    spec = REG["un_news"]
    for i in range(collect.SUSPICIOUS_EMPTY_RUNS):
        r = collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult([])))
        assert r.status == "ok_empty"
        row = db.get_source_status(conn)["un_news"]
        assert row["consecutive_failures"] == 0
        if i == 0:
            assert collect.health_label(spec, row) == "ok_empty"
    assert collect.health_label(spec, row) == "empty_suspicious"
    collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult(un_docs())))
    assert db.get_source_status(conn)["un_news"]["consecutive_empty"] == 0


def test_run_metrics_and_partial(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    spec = REG["gdelt"]
    docs = un_docs()
    collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult(docs)))
    r = collect.run_source(spec, conn, None, connector=Stub(spec, FetchResult(docs, notes=["africa: HTTP 500"], partial=True)))
    assert (r.status, r.new, r.duplicates) == ("partial", 0, 3)
    run = db.recent_runs(conn)[0]
    assert run["duplicates"] == 3 and run["duration_ms"] is not None and run["status"] == "partial"
    assert collect.health_label(spec, db.get_source_status(conn)["gdelt"]) == "partial"


# --- overlapping collectors ----------------------------------------------------------------
def test_lock_prevents_overlap_and_expires(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.acquire_lock(conn, "collector", "A")
    assert not db.acquire_lock(conn, "collector", "B")
    with pytest.raises(collect.CollectorBusy):
        collect.collect(["un_news"], conn=conn, http=object())
    db.release_lock(conn, "collector", "A")
    assert db.acquire_lock(conn, "collector", "B", ttl=timedelta(seconds=-1))   # already expired (crashed run)
    assert db.acquire_lock(conn, "collector", "C")


# --- backups ----------------------------------------------------------------------------
def test_backup_rotate_restore(tmp_path, monkeypatch):
    path = tmp_path / "gim.sqlite3"
    conn = db.connect(path)
    db.save_documents(conn, un_docs())
    b = maintenance.make_backup(conn, tmp_path / "bk", keep=2)
    assert maintenance.integrity(b)[0]
    for i in range(3):
        (tmp_path / "bk" / f"gim-2000010{i}T000000Z.sqlite3").write_bytes(b.read_bytes())
    maintenance.rotate(tmp_path / "bk", keep=2)
    assert len(list((tmp_path / "bk").glob("*.sqlite3"))) == 2
    conn.execute("DELETE FROM documents")
    conn.commit()
    conn.close()
    maintenance.restore(b, target=path)
    assert len(db.query_documents(db.connect(path), SINCE)) == 3
    assert list((tmp_path / "backups").glob("gim-pre-restore-*.sqlite3"))


def test_restore_refuses_corrupt_backup(tmp_path):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database")
    with pytest.raises(Exception):
        maintenance.restore(bad, target=tmp_path / "gim.sqlite3")
