"""SQLite storage layer.

Why SQLite first: zero installation, a single file you can back up by
copying, and fast enough for millions of article rows. All SQL lives in this
module, so moving to PostgreSQL later means rewriting one file, not the app.

Always use parameterised queries (the ``?`` placeholders) -- never build SQL
with f-strings from user or source input (SQL injection).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from gim.config import get_settings
from gim.models import Article

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id                 INTEGER PRIMARY KEY,
    canonical_url      TEXT NOT NULL UNIQUE,   -- dedup key for the SAME article
    url                TEXT NOT NULL,          -- original URL as reported (provenance)
    title              TEXT NOT NULL,
    title_fingerprint  TEXT NOT NULL,          -- groups syndicated copies of one story
    domain             TEXT NOT NULL DEFAULT '',
    language           TEXT NOT NULL DEFAULT '',
    source_country     TEXT NOT NULL DEFAULT '', -- where the OUTLET is, not the event
    seen_at            TEXT NOT NULL,          -- UTC ISO-8601; when GDELT first saw it
    first_ingested_at  TEXT NOT NULL,          -- when WE first stored it
    last_ingested_at   TEXT NOT NULL,          -- last time a collection run returned it
    source             TEXT NOT NULL,          -- collection system, e.g. 'gdelt'
    categories         TEXT NOT NULL DEFAULT ''-- comma-separated rule-based tags
);
CREATE INDEX IF NOT EXISTS idx_articles_seen_at     ON articles(seen_at);
CREATE INDEX IF NOT EXISTS idx_articles_fingerprint ON articles(title_fingerprint);

-- One article may be relevant to several regions.
CREATE TABLE IF NOT EXISTS article_regions (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    region     TEXT NOT NULL,
    PRIMARY KEY (article_id, region)
);
CREATE INDEX IF NOT EXISTS idx_article_regions_region ON article_regions(region);

-- Audit trail of every collection run: what was asked, what came back, errors.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT NOT NULL,
    region      TEXT NOT NULL,
    query       TEXT NOT NULL,
    timespan    TEXT NOT NULL,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    error       TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_settings().db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard read while the collector writes.
    conn.execute("PRAGMA journal_mode = WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}).")
    conn.executescript(SCHEMA)
    # Future migrations go here:  if version < 2: ALTER TABLE ...
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def save_articles(conn: sqlite3.Connection, articles: Iterable[Article], region: str) -> int:
    """Insert new articles, refresh known ones. Returns the number of NEW articles."""
    now = utcnow_iso()
    new = 0
    with transaction(conn):
        for a in articles:
            cur = conn.execute(
                """
                INSERT INTO articles (canonical_url, url, title, title_fingerprint, domain, language,
                                      source_country, seen_at, first_ingested_at, last_ingested_at,
                                      source, categories)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_url) DO NOTHING
                """,
                (a.canonical_url, a.url, a.title, a.title_fingerprint, a.domain, a.language,
                 a.source_country, to_iso(a.seen_at), now, now, a.source, ",".join(a.categories)),
            )
            if cur.rowcount == 1:
                new += 1
                article_id = cur.lastrowid
            else:
                article_id = conn.execute(
                    "SELECT id FROM articles WHERE canonical_url = ?", (a.canonical_url,)
                ).fetchone()[0]
                conn.execute("UPDATE articles SET last_ingested_at = ? WHERE id = ?", (now, article_id))
            conn.execute(
                "INSERT OR IGNORE INTO article_regions (article_id, region) VALUES (?, ?)",
                (article_id, region),
            )
    return new


def start_run(conn: sqlite3.Connection, source: str, region: str, query: str, timespan: str) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO ingest_runs (started_at, source, region, query, timespan) VALUES (?, ?, ?, ?, ?)",
            (utcnow_iso(), source, region, query, timespan),
        )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, fetched: int = 0, new: int = 0,
               error: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE ingest_runs SET finished_at = ?, fetched = ?, new = ?, status = ?, error = ? WHERE id = ?",
            (utcnow_iso(), fetched, new, "error" if error else "ok", error, run_id),
        )


def query_articles(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime | None = None,
    regions: list[str] | None = None,
    categories: list[str] | None = None,
    text: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Filtered article search. Returns plain dicts (easy to load into pandas)."""
    where = ["a.seen_at >= ?"]
    params: list[Any] = [to_iso(since)]
    if until:
        where.append("a.seen_at <= ?")
        params.append(to_iso(until))
    if regions:
        where.append(f"r.region IN ({','.join('?' * len(regions))})")
        params.extend(regions)
    if categories:
        where.append("(" + " OR ".join("(',' || a.categories || ',') LIKE ?" for _ in categories) + ")")
        params.extend(f"%,{c},%" for c in categories)
    if text:
        where.append("a.title LIKE ? ESCAPE '\\'")
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    sql = f"""
        SELECT a.id, a.seen_at, a.title, a.url, a.domain, a.language, a.source_country,
               a.categories, a.title_fingerprint, a.first_ingested_at,
               GROUP_CONCAT(DISTINCT r.region) AS regions
        FROM articles a JOIN article_regions r ON r.article_id = a.id
        WHERE {' AND '.join(where)}
        GROUP BY a.id
        ORDER BY a.seen_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
