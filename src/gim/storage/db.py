"""SQLite storage layer.

Why SQLite first: zero installation, a single file you can back up by
copying, and fast enough for millions of rows. All SQL lives in this module,
so moving to PostgreSQL later means rewriting one file, not the app.

Always use parameterised queries (the ``?`` placeholders) -- never build SQL
with f-strings from user or source input (SQL injection). The few f-strings
below only insert "?" placeholders or fixed column names.

Schema history
  v1 (Phase 1): articles, article_regions, ingest_runs
  v2 (Phase 2): documents (all source types), document_regions, document_sources,
                document_revisions, source_status; ingest_runs gains data_origin/notes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from gim.config import get_settings
from gim.models import Article, Document

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS documents (
    id                 INTEGER PRIMARY KEY,
    source_id          TEXT NOT NULL,           -- registry id of the FIRST source that delivered it
    external_id        TEXT NOT NULL DEFAULT '',-- the source's own id / guid
    canonical_url      TEXT NOT NULL UNIQUE,    -- dedup key
    url                TEXT NOT NULL,           -- original URL (provenance)
    title              TEXT NOT NULL,           -- original title
    summary            TEXT NOT NULL DEFAULT '',
    publisher          TEXT NOT NULL DEFAULT '',-- who published THIS copy
    original_reporter  TEXT NOT NULL DEFAULT '',-- who originally reported / authored it, if identifiable
    doc_type           TEXT NOT NULL DEFAULT 'unknown',
    source_category    TEXT NOT NULL DEFAULT '',
    language           TEXT NOT NULL DEFAULT '',
    source_country     TEXT NOT NULL DEFAULT '',-- publisher's country, NOT the event's
    countries          TEXT NOT NULL DEFAULT '',-- comma-separated, places mentioned
    categories         TEXT NOT NULL DEFAULT '',
    title_fingerprint  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,           -- detects later corrections/updates
    published_at       TEXT,                    -- as stated by the source (UTC ISO)
    updated_at         TEXT,                    -- source-stated update time
    event_date         TEXT,                    -- date of the EVENT, when known (Phase 3+)
    timeline_at        TEXT NOT NULL,           -- published_at, or collected_at if unknown
    collected_at       TEXT NOT NULL,           -- when WE first stored it
    last_seen_at       TEXT NOT NULL,           -- last time any source returned it
    data_origin        TEXT NOT NULL DEFAULT 'live',  -- live | fixture
    provenance         TEXT NOT NULL DEFAULT '{}'     -- JSON: connector details
);
CREATE INDEX IF NOT EXISTS idx_documents_timeline    ON documents(timeline_at);
CREATE INDEX IF NOT EXISTS idx_documents_fingerprint ON documents(title_fingerprint);
CREATE INDEX IF NOT EXISTS idx_documents_source      ON documents(source_id);

CREATE TABLE IF NOT EXISTS document_regions (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    region      TEXT NOT NULL,
    PRIMARY KEY (document_id, region)
);
CREATE INDEX IF NOT EXISTS idx_document_regions_region ON document_regions(region);

-- The same URL can be delivered by several collection sources (e.g. GDELT and an RSS feed).
CREATE TABLE IF NOT EXISTS document_sources (
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (document_id, source_id)
);

-- Previous versions of a document when its source changes title/summary (corrections).
CREATE TABLE IF NOT EXISTS document_revisions (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    recorded_at  TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    updated_at   TEXT,
    content_hash TEXT NOT NULL
);

-- Operational health of each registry source.
CREATE TABLE IF NOT EXISTS source_status (
    source_id            TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'never_run',
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_error           TEXT,
    last_error_at        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    items_last_run       INTEGER NOT NULL DEFAULT 0,
    new_last_run         INTEGER NOT NULL DEFAULT 0,
    etag                 TEXT,
    last_modified        TEXT
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT NOT NULL,          -- registry source id
    region      TEXT NOT NULL,
    query       TEXT NOT NULL,
    timespan    TEXT NOT NULL,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | not_modified | skipped | error
    error       TEXT,
    data_origin TEXT NOT NULL DEFAULT 'live',
    notes       TEXT
);
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def content_hash(title: str, summary: str) -> str:
    return hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()[:16]


def _csv(values: Iterable[str]) -> str:
    return ",".join(v.replace(",", " ") for v in values if v)


# ---------------------------------------------------------------------------
# connection & migrations
# ---------------------------------------------------------------------------
def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_settings().db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # dashboard can read while the collector writes
    init_db(conn)
    return conn


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}).")
    if version == SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA_V2)  # idempotent: CREATE ... IF NOT EXISTS
    if version == 1 or _table_exists(conn, "articles"):
        _migrate_v1_to_v2(conn)
    cols = _columns(conn, "ingest_runs")
    with transaction(conn):
        if "data_origin" not in cols:
            conn.execute("ALTER TABLE ingest_runs ADD COLUMN data_origin TEXT NOT NULL DEFAULT 'live'")
        if "notes" not in cols:
            conn.execute("ALTER TABLE ingest_runs ADD COLUMN notes TEXT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Copy Phase 1 GDELT articles into the general documents table."""
    with transaction(conn):
        rows = conn.execute("SELECT * FROM articles").fetchall()
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO documents (source_id, canonical_url, url, title, publisher,
                       doc_type, source_category, language, source_country, categories, title_fingerprint,
                       content_hash, published_at, timeline_at, collected_at, last_seen_at, data_origin, provenance)
                   VALUES ('gdelt', ?, ?, ?, ?, 'unknown', 'news_discovery', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?)""",
                (r["canonical_url"], r["url"], r["title"], r["domain"], r["language"], r["source_country"],
                 r["categories"], r["title_fingerprint"], content_hash(r["title"], ""), r["seen_at"], r["seen_at"],
                 r["first_ingested_at"], r["last_ingested_at"],
                 json.dumps({"connector": "gdelt", "migrated_from": "v1"})),
            )
            if cur.rowcount != 1:
                continue
            doc_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO document_sources VALUES (?, 'gdelt', ?)",
                         (doc_id, r["first_ingested_at"]))
            for (region,) in conn.execute("SELECT region FROM article_regions WHERE article_id = ?", (r["id"],)):
                conn.execute("INSERT OR IGNORE INTO document_regions VALUES (?, ?)", (doc_id, region))
        conn.execute("DROP TABLE IF EXISTS article_regions")
        conn.execute("DROP TABLE IF EXISTS articles")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------
def save_documents(conn: sqlite3.Connection, docs: Iterable[Document]) -> tuple[int, int]:
    """Insert new documents and merge known ones. Returns (new, updated).

    Merge rules for a URL we already have:
      * remember that this source also delivered it (document_sources),
      * add any new regions,
      * fill EMPTY fields (summary, original_reporter, doc_type 'unknown') from the richer copy,
      * if the SAME source changed title/summary -> keep the old version in document_revisions.
    """
    now = utcnow_iso()
    new = updated = 0
    with transaction(conn):
        for d in docs:
            chash = content_hash(d.title, d.summary)
            published = to_iso(d.published_at)
            row = conn.execute("SELECT * FROM documents WHERE canonical_url = ?", (d.canonical_url,)).fetchone()
            if row is None:
                cur = conn.execute(
                    """INSERT INTO documents (source_id, external_id, canonical_url, url, title, summary,
                           publisher, original_reporter, doc_type, source_category, language, source_country,
                           countries, categories, title_fingerprint, content_hash, published_at, updated_at,
                           event_date, timeline_at, collected_at, last_seen_at, data_origin, provenance)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d.source_id, d.external_id, d.canonical_url, d.url, d.title, d.summary, d.publisher,
                     d.original_reporter, d.doc_type, d.source_category, d.language, d.source_country,
                     _csv(d.countries), _csv(d.categories), d.title_fingerprint, chash, published,
                     to_iso(d.updated_at), d.event_date, published or now, now, now, d.data_origin,
                     json.dumps(d.provenance, ensure_ascii=False, default=str)),
                )
                doc_id = cur.lastrowid
                new += 1
            else:
                doc_id = row["id"]
                sets: dict[str, Any] = {"last_seen_at": now}
                if not row["summary"] and d.summary:
                    sets["summary"] = d.summary
                if not row["original_reporter"] and d.original_reporter:
                    sets["original_reporter"] = d.original_reporter
                if row["doc_type"] == "unknown" and d.doc_type != "unknown":
                    sets["doc_type"] = d.doc_type
                if row["source_id"] == d.source_id and row["content_hash"] != chash:
                    conn.execute(
                        "INSERT INTO document_revisions (document_id, recorded_at, title, summary, updated_at, content_hash)"
                        " VALUES (?,?,?,?,?,?)",
                        (doc_id, now, row["title"], row["summary"], row["updated_at"], row["content_hash"]),
                    )
                    sets.update(title=d.title, summary=d.summary, content_hash=chash,
                                title_fingerprint=d.title_fingerprint, updated_at=to_iso(d.updated_at) or now)
                    updated += 1
                assignments = ", ".join(f"{k} = ?" for k in sets)
                conn.execute(f"UPDATE documents SET {assignments} WHERE id = ?", (*sets.values(), doc_id))
            conn.execute("INSERT OR IGNORE INTO document_sources VALUES (?, ?, ?)", (doc_id, d.source_id, now))
            for region in d.regions or ["global"]:
                conn.execute("INSERT OR IGNORE INTO document_regions VALUES (?, ?)", (doc_id, region))
    return new, updated


def query_documents(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime | None = None,
    regions: list[str] | None = None,
    categories: list[str] | None = None,
    text: str | None = None,
    source_ids: list[str] | None = None,
    doc_types: list[str] | None = None,
    source_categories: list[str] | None = None,
    data_origins: list[str] | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    where = ["d.timeline_at >= ?"]
    params: list[Any] = [to_iso(since)]

    def _in(column: str, values: list[str] | None) -> None:
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)

    if until:
        where.append("d.timeline_at <= ?")
        params.append(to_iso(until))
    if regions:
        where.append(f"d.id IN (SELECT document_id FROM document_regions WHERE region IN ({','.join('?' * len(regions))}))")
        params.extend(regions)
    if categories:
        where.append("(" + " OR ".join("(',' || d.categories || ',') LIKE ?" for _ in categories) + ")")
        params.extend(f"%,{c},%" for c in categories)
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(d.title LIKE ? ESCAPE '\\' OR d.summary LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%"] * 2)
    _in("d.source_id", source_ids)
    _in("d.doc_type", doc_types)
    _in("d.source_category", source_categories)
    _in("d.data_origin", data_origins)
    sql = f"""
        SELECT d.*,
               (SELECT GROUP_CONCAT(region) FROM document_regions WHERE document_id = d.id) AS regions,
               (SELECT GROUP_CONCAT(source_id) FROM document_sources WHERE document_id = d.id) AS delivered_by,
               (SELECT COUNT(*) FROM document_revisions WHERE document_id = d.id) AS revisions
        FROM documents d
        WHERE {' AND '.join(where)}
        ORDER BY d.timeline_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# source health
# ---------------------------------------------------------------------------
def get_source_status(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM source_status")}


def record_source_attempt(conn: sqlite3.Connection, source_id: str, *, status: str, error: str | None = None,
                          items: int = 0, new: int = 0, etag: str | None = None,
                          last_modified: str | None = None) -> None:
    """status: ok | not_modified | error | needs_credentials."""
    now = utcnow_iso()
    success = status in ("ok", "not_modified")
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO source_status (source_id) VALUES (?)", (source_id,))
        if success:
            conn.execute(
                """UPDATE source_status SET status='ok', last_attempt_at=?, last_success_at=?,
                       consecutive_failures=0, items_last_run=?, new_last_run=?,
                       etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified)
                   WHERE source_id=?""",
                (now, now, items, new, etag, last_modified, source_id),
            )
        elif status == "needs_credentials":
            conn.execute(
                "UPDATE source_status SET status='needs_credentials', last_attempt_at=?, last_error=? WHERE source_id=?",
                (now, error, source_id),
            )
        else:
            conn.execute(
                """UPDATE source_status SET last_attempt_at=?, last_error=?, last_error_at=?,
                       consecutive_failures=consecutive_failures+1,
                       status=CASE WHEN consecutive_failures+1 >= 3 THEN 'failing' ELSE 'degraded' END
                   WHERE source_id=?""",
                (now, error, now, source_id),
            )


# ---------------------------------------------------------------------------
# run log
# ---------------------------------------------------------------------------
def start_run(conn: sqlite3.Connection, source: str, region: str, query: str, timespan: str,
              data_origin: str = "live") -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO ingest_runs (started_at, source, region, query, timespan, data_origin) VALUES (?,?,?,?,?,?)",
            (utcnow_iso(), source, region, query, timespan, data_origin),
        )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, fetched: int = 0, new: int = 0,
               error: str | None = None, status: str | None = None, notes: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE ingest_runs SET finished_at=?, fetched=?, new=?, status=?, error=?, notes=? WHERE id=?",
            (utcnow_iso(), fetched, new, status or ("error" if error else "ok"), error, notes, run_id),
        )


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 1 compatibility API (GDELT articles) -- kept so existing code/tests work
# ---------------------------------------------------------------------------
def save_articles(conn: sqlite3.Connection, articles: Iterable[Article], region: str) -> int:
    from gim.sources.gdelt import article_to_document  # local import: avoids a storage->sources cycle

    docs = []
    for a in articles:
        d = article_to_document(a, region, query="")
        d.regions = [region]  # Phase 1 semantics: only the queried region
        docs.append(d)
    return save_documents(conn, docs)[0]


def query_articles(conn: sqlite3.Connection, since: datetime, until: datetime | None = None,
                   regions: list[str] | None = None, categories: list[str] | None = None,
                   text: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
    rows = query_documents(conn, since, until, regions, categories, text, limit=limit)
    for r in rows:
        r["seen_at"] = r["timeline_at"]
        r["domain"] = r["publisher"]
        r["first_ingested_at"] = r["collected_at"]
    return rows
