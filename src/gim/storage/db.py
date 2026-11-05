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
  v3 (Phase 2.1): documents.source_country -> publisher_country, countries -> countries_mentioned;
                events + event_documents; source_validations; locks; run metrics
                (duplicates, duration, http status, error kind); source backoff/empty counters.
  v4 (Phase 3): event statistics columns (categories, regions, organizations, counts,
                corroboration level, prominence); FTS5 full-text archive over documents;
                watchlists, alerts, llm_calls (cost ledger), event_analyses (AI cache), reports.

Migration safety
  * Before upgrading an EXISTING database file, a full copy is written to
    data/backups/ (see gim.maintenance for restore).
  * Each migration step runs inside ONE explicit transaction (BEGIN IMMEDIATE ...
    COMMIT) including the PRAGMA user_version bump, so a crash leaves either the
    old or the new schema -- never a half-migrated one.

Timestamps: every *_at column is an ISO-8601 string in UTC with "+00:00".
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from gim.config import get_settings
from gim.models import Article, Document

SCHEMA_VERSION = 4

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

V2_TO_V3 = """
ALTER TABLE documents RENAME COLUMN source_country TO publisher_country;
ALTER TABLE documents RENAME COLUMN countries TO countries_mentioned;

ALTER TABLE source_status ADD COLUMN backoff_until TEXT;            -- do not contact before (Retry-After / failures)
ALTER TABLE source_status ADD COLUMN consecutive_empty INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_status ADD COLUMN duplicates_last_run INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_status ADD COLUMN last_duration_ms INTEGER;
ALTER TABLE source_status ADD COLUMN last_http_status INTEGER;
ALTER TABLE source_status ADD COLUMN last_error_kind TEXT;           -- network|http_4xx|http_5xx|rate_limited|parse|credentials|unexpected
ALTER TABLE source_status ADD COLUMN last_result TEXT;               -- ok|ok_empty|not_modified|partial|error|needs_credentials

ALTER TABLE ingest_runs ADD COLUMN duplicates INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingest_runs ADD COLUMN updated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingest_runs ADD COLUMN duration_ms INTEGER;
ALTER TABLE ingest_runs ADD COLUMN http_status INTEGER;
ALTER TABLE ingest_runs ADD COLUMN error_kind TEXT;

-- An EVENT groups documents that report the same happening. IDs are never reused
-- (AUTOINCREMENT), so "EVT-<id>" is a permanent reference for reports and the AI layer.
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    headline            TEXT NOT NULL,     -- title of the earliest member document
    first_published_at  TEXT NOT NULL,
    last_published_at   TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    countries_mentioned TEXT NOT NULL DEFAULT '',  -- union over member documents
    event_country       TEXT,              -- WHERE it happened. NULL = unknown
    countries_involved  TEXT,              -- WHO is party to it. NULL = unknown
    location_basis      TEXT NOT NULL DEFAULT 'unknown',  -- unknown|dataset|analyst|ai_extracted
    location_note       TEXT,
    clustering_method   TEXT NOT NULL      -- algorithm + version that created it
);
CREATE INDEX IF NOT EXISTS idx_events_last ON events(last_published_at);

CREATE TABLE IF NOT EXISTS event_documents (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,  -- one event per document
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    link_method TEXT NOT NULL,             -- seed | same_fingerprint | title_similarity
    similarity  REAL NOT NULL,
    linked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_documents_event ON event_documents(event_id);

-- Results of `python -m gim.validate`. A source counts as verified only through a PASS here.
CREATE TABLE IF NOT EXISTS source_validations (
    id              INTEGER PRIMARY KEY,
    source_id       TEXT NOT NULL,
    validated_at    TEXT NOT NULL,
    outcome         TEXT NOT NULL,         -- pass | fail | empty | skipped
    http_status     INTEGER,
    documents       INTEGER NOT NULL DEFAULT 0,
    valid_documents INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER,
    summary         TEXT,
    checks          TEXT NOT NULL DEFAULT '{}'   -- JSON detail
);
CREATE INDEX IF NOT EXISTS idx_source_validations_source ON source_validations(source_id, validated_at);

-- Prevents two collectors (scheduler + dashboard button) from running at once.
CREATE TABLE IF NOT EXISTS locks (
    name        TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""

V3_TO_V4 = """
ALTER TABLE events ADD COLUMN categories TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN regions TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN organizations TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN documents_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN publishers_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN independent_origins INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN official_origins INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN corroboration_level TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN prominence REAL NOT NULL DEFAULT 0;   -- transparent ranking formula, see pipeline/prominence.py
ALTER TABLE events ADD COLUMN data_origin TEXT NOT NULL DEFAULT 'live';
CREATE INDEX IF NOT EXISTS idx_events_prominence ON events(prominence);

-- Full-text archive (BM25 ranking) over all documents, kept in sync by triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, summary, content='documents', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE OF title, summary ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO documents_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
INSERT INTO documents_fts(documents_fts) VALUES ('rebuild');

CREATE TABLE IF NOT EXISTS watchlists (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- country | region | keyword | organization | source
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY,
    rule            TEXT NOT NULL,     -- volume_spike | watchlist_match | official_statement | multi_origin_event
    severity        TEXT NOT NULL,     -- info | notice | warning
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL,
    criteria        TEXT NOT NULL,     -- JSON: the exact numbers / rule that fired (transparency)
    event_id        INTEGER REFERENCES events(id),
    document_id     INTEGER REFERENCES documents(id),
    created_at      TEXT NOT NULL,
    dedup_key       TEXT NOT NULL UNIQUE,
    acknowledged_at TEXT,
    data_origin     TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);

-- Every AI call: the cost ledger behind the monthly budget cap.
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    duration_ms   INTEGER,
    ok            INTEGER NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_at ON llm_calls(at);

-- Cached per-event AI analyses (key = event content + provider + model + prompt version).
CREATE TABLE IF NOT EXISTS event_analyses (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES events(id),
    cache_key      TEXT NOT NULL UNIQUE,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    content        TEXT NOT NULL,      -- JSON: accepted claims, sections
    rejected       TEXT NOT NULL DEFAULT '[]'   -- JSON: model statements the validator rejected
);
CREATE INDEX IF NOT EXISTS idx_event_analyses_event ON event_analyses(event_id);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY,
    report_type  TEXT NOT NULL,
    title        TEXT NOT NULL,
    scope        TEXT NOT NULL,       -- JSON: regions / countries / topic
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',   -- draft | reviewed | final
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    content      TEXT NOT NULL,       -- JSON report structure
    markdown     TEXT NOT NULL,
    data_origin  TEXT NOT NULL DEFAULT 'live',    -- live | fixture | mixed
    reviewer_note TEXT,
    reviewed_at  TEXT
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


FUTURE_TOLERANCE = timedelta(hours=1)


def content_hash(title: str, summary: str) -> str:
    return hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()[:16]


def _csv(values: Iterable[str]) -> str:
    return ",".join(v.replace(",", " ") for v in values if v)


# ---------------------------------------------------------------------------
# connection & migrations
# ---------------------------------------------------------------------------
def connect(db_path: Path | str | None = None, *, backup_dir: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_settings().db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # dashboard can read while the collector writes
    init_db(conn, path if str(path) != ":memory:" else None, backup_dir=backup_dir)
    return conn


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _statements(script: str) -> list[str]:
    """Split a SQL script into statements (executescript would COMMIT our transaction)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    return out


@contextmanager
def _migration_step(conn: sqlite3.Connection, to_version: int) -> Iterator[sqlite3.Connection]:
    """One atomic migration step: BEGIN IMMEDIATE ... user_version bump ... COMMIT."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute(f"PRAGMA user_version = {int(to_version)}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def backup_database(conn: sqlite3.Connection, dest: Path) -> Path:
    """Consistent online copy using SQLite's backup API (safe while others read/write)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def init_db(conn: sqlite3.Connection, path: Path | None = None, backup_dir: Path | None = None) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}).")
    if version == SCHEMA_VERSION:
        return
    has_data = _table_exists(conn, "articles") or _table_exists(conn, "documents")
    if path is not None and has_data:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bdir = backup_dir or path.parent / "backups"
        dest = backup_database(conn, bdir / f"{path.stem}-pre-v{SCHEMA_VERSION}-from-v{version}-{stamp}.sqlite3")
        import logging
        logging.getLogger(__name__).warning("Schema upgrade v%d -> v%d: backup written to %s",
                                            version, SCHEMA_VERSION, dest)
    if version < 2:
        with _migration_step(conn, 2):
            for stmt in _statements(SCHEMA_V2):
                conn.execute(stmt)
            if _table_exists(conn, "articles"):
                _migrate_v1_rows(conn)
            cols = _columns(conn, "ingest_runs")
            if "data_origin" not in cols:
                conn.execute("ALTER TABLE ingest_runs ADD COLUMN data_origin TEXT NOT NULL DEFAULT 'live'")
            if "notes" not in cols:
                conn.execute("ALTER TABLE ingest_runs ADD COLUMN notes TEXT")
    if version < 3:
        with _migration_step(conn, 3):
            for stmt in _statements(V2_TO_V3):
                conn.execute(stmt)
    if version < 4:
        with _migration_step(conn, 4):
            for stmt in _statements(V3_TO_V4):
                conn.execute(stmt)
            for (event_id,) in conn.execute("SELECT id FROM events").fetchall():
                refresh_event(conn, event_id)


def _migrate_v1_rows(conn: sqlite3.Connection) -> None:
    """Copy Phase 1 GDELT articles into the general documents table (inside a migration step)."""
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
        conn.execute("INSERT OR IGNORE INTO document_sources VALUES (?, 'gdelt', ?)", (doc_id, r["first_ingested_at"]))
        for (region,) in conn.execute("SELECT region FROM article_regions WHERE article_id = ?", (r["id"],)).fetchall():
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
    future_limit = to_iso(datetime.now(timezone.utc) + FUTURE_TOLERANCE)
    new = updated = 0
    with transaction(conn):
        for d in docs:
            chash = content_hash(d.title, d.summary)
            published = to_iso(d.published_at)
            timeline = published or now
            if published and published > future_limit:
                # A publication time in the future means a wrong clock/timezone at the source.
                # Keep the stated value for provenance, but sort by collection time.
                timeline = now
                d.provenance = {**d.provenance, "published_at_in_future": True}
            row = conn.execute("SELECT * FROM documents WHERE canonical_url = ?", (d.canonical_url,)).fetchone()
            if row is None:
                cur = conn.execute(
                    """INSERT INTO documents (source_id, external_id, canonical_url, url, title, summary,
                           publisher, original_reporter, doc_type, source_category, language, publisher_country,
                           countries_mentioned, categories, title_fingerprint, content_hash, published_at, updated_at,
                           event_date, timeline_at, collected_at, last_seen_at, data_origin, provenance)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d.source_id, d.external_id, d.canonical_url, d.url, d.title, d.summary, d.publisher,
                     d.original_reporter, d.doc_type, d.source_category, d.language, d.publisher_country,
                     _csv(d.countries_mentioned), _csv(d.categories), d.title_fingerprint, chash, published,
                     to_iso(d.updated_at), d.event_date, timeline, now, now, d.data_origin,
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
# events
# ---------------------------------------------------------------------------
LOCATION_BASES = {"unknown", "dataset", "analyst", "ai_extracted"}


def unassigned_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT d.id, d.timeline_at, d.title, d.title_fingerprint, d.countries_mentioned
           FROM documents d LEFT JOIN event_documents ed ON ed.document_id = d.id
           WHERE ed.document_id IS NULL ORDER BY d.timeline_at, d.id""")
    return [dict(r) for r in rows]


def event_members_since(conn: sqlite3.Connection, since_iso: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT e.id AS event_id, e.first_published_at, e.last_published_at,
                  d.id, d.timeline_at, d.title, d.title_fingerprint, d.countries_mentioned
           FROM events e JOIN event_documents ed ON ed.event_id = e.id JOIN documents d ON d.id = ed.document_id
           WHERE e.last_published_at >= ? ORDER BY e.id, d.id""", (since_iso,))
    return [dict(r) for r in rows]


def create_event(conn: sqlite3.Connection, headline: str, at_iso: str, method: str) -> int:
    now = utcnow_iso()
    cur = conn.execute(
        """INSERT INTO events (headline, first_published_at, last_published_at, created_at, updated_at,
               clustering_method) VALUES (?,?,?,?,?,?)""", (headline, at_iso, at_iso, now, now, method))
    return int(cur.lastrowid)


def link_event_document(conn: sqlite3.Connection, event_id: int, document_id: int, method: str,
                        similarity: float) -> None:
    conn.execute("INSERT OR IGNORE INTO event_documents VALUES (?,?,?,?,?)",
                 (document_id, event_id, method, similarity, utcnow_iso()))


def refresh_event(conn: sqlite3.Connection, event_id: int) -> None:
    """Recompute an event's derived fields from its member documents.

    headline = earliest member's title; categories/regions/organizations = union over members;
    counts and corroboration from pipeline.corroboration; prominence from pipeline.prominence.
    """
    from gim.pipeline.actors import tag_organizations
    from gim.pipeline.corroboration import summarise
    from gim.pipeline.prominence import prominence

    docs = [dict(r) for r in conn.execute(
        """SELECT d.*, (SELECT GROUP_CONCAT(region) FROM document_regions WHERE document_id = d.id) AS regions
           FROM documents d JOIN event_documents ed ON ed.document_id = d.id
           WHERE ed.event_id = ? ORDER BY d.timeline_at, d.id""", (event_id,))]
    if not docs:
        return

    def union(key: str) -> list[str]:
        out: list[str] = []
        for d in docs:
            for v in (d.get(key) or "").split(","):
                if v and v not in out:
                    out.append(v)
        return out

    orgs: list[str] = []
    for d in docs:
        for o in tag_organizations(f"{d['title']} {d['summary']}"):
            if o not in orgs:
                orgs.append(o)
    summary = summarise(docs)
    categories = union("categories")
    origins = {d["data_origin"] for d in docs}
    conn.execute(
        """UPDATE events SET first_published_at=?, last_published_at=?, headline=?, countries_mentioned=?,
               categories=?, regions=?, organizations=?, documents_count=?, publishers_count=?,
               independent_origins=?, official_origins=?, corroboration_level=?, prominence=?, data_origin=?,
               updated_at=? WHERE id=?""",
        (docs[0]["timeline_at"], docs[-1]["timeline_at"], docs[0]["title"], ",".join(union("countries_mentioned")),
         ",".join(categories), ",".join(union("regions")), ",".join(orgs), summary.documents, summary.publishers,
         summary.independent_origins, summary.official_origins, summary.level,
         prominence(summary.independent_origins, summary.official_origins, summary.documents, categories),
         origins.pop() if len(origins) == 1 else "mixed", utcnow_iso(), event_id))


def set_event_location(conn: sqlite3.Connection, event_id: int, *, event_country: str | None,
                       countries_involved: list[str] | None, basis: str, note: str = "") -> None:
    """Record WHERE an event happened / WHO is involved, with the basis of that knowledge.

    Only an explicit basis (dataset, analyst, ai_extracted) may set it -- publisher or
    mentioned countries are never copied here automatically.
    """
    if basis not in LOCATION_BASES or basis == "unknown":
        raise ValueError(f"basis must be one of {sorted(LOCATION_BASES - {'unknown'})}")
    with transaction(conn):
        conn.execute(
            """UPDATE events SET event_country=?, countries_involved=?, location_basis=?, location_note=?,
                   updated_at=? WHERE id=?""",
            (event_country, _csv(countries_involved or []) or None, basis, note, utcnow_iso(), event_id))


def query_events(conn: sqlite3.Connection, since: datetime, until: datetime | None = None,
                 regions: list[str] | None = None, text: str | None = None,
                 data_origins: list[str] | None = None, limit: int = 2000, *,
                 countries: list[str] | None = None, categories: list[str] | None = None,
                 organizations: list[str] | None = None, source_ids: list[str] | None = None,
                 min_origins: int = 0, order: str = "recent") -> list[dict[str, Any]]:
    """Events overlapping [since, until]. order: recent | prominence."""
    where = ["e.last_published_at >= ?"]
    params: list[Any] = [to_iso(since)]
    for column, values in (("countries_mentioned", countries), ("categories", categories),
                           ("organizations", organizations)):
        if values:
            where.append("(" + " OR ".join(f"(',' || e.{column} || ',') LIKE ?" for _ in values) + ")")
            params.extend(f"%,{v},%" for v in values)
    if min_origins:
        where.append("(e.independent_origins + e.official_origins) >= ?")
        params.append(int(min_origins))
    if until:
        where.append("e.first_published_at <= ?")
        params.append(to_iso(until))
    member_filter = []
    if regions:   # events.regions is the union of member-document regions (schema v4)
        where.append("(" + " OR ".join("(',' || e.regions || ',') LIKE ?" for _ in regions) + ")")
        params.extend(f"%,{r},%" for r in regions)
    if data_origins:
        member_filter.append(f"d.data_origin IN ({','.join('?' * len(data_origins))})")
        params.extend(data_origins)
    if source_ids:
        member_filter.append(f"d.id IN (SELECT document_id FROM document_sources WHERE source_id IN ({','.join('?' * len(source_ids))}))")
        params.extend(source_ids)
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        member_filter.append("(d.title LIKE ? ESCAPE '\\' OR d.summary LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%"] * 2)
    if member_filter:
        where.append("e.id IN (SELECT ed.event_id FROM event_documents ed JOIN documents d ON d.id = ed.document_id"
                     " WHERE " + " AND ".join(member_filter) + ")")
    sql = f"""
        SELECT e.*, e.documents_count AS documents, e.publishers_count AS publishers
        FROM events e WHERE {' AND '.join(where)}
        ORDER BY {"e.prominence DESC, " if order == "prominence" else ""}e.last_published_at DESC, e.id DESC LIMIT ?"""
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def event_documents(conn: sqlite3.Connection, event_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not event_ids:
        return {}
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in event_ids}
    for chunk_start in range(0, len(event_ids), 500):
        chunk = event_ids[chunk_start:chunk_start + 500]
        rows = conn.execute(
            f"""SELECT ed.event_id, ed.link_method, ed.similarity, d.*,
                       (SELECT GROUP_CONCAT(source_id) FROM document_sources WHERE document_id = d.id) AS delivered_by
                FROM event_documents ed JOIN documents d ON d.id = ed.document_id
                WHERE ed.event_id IN ({','.join('?' * len(chunk))}) ORDER BY d.timeline_at, d.id""", chunk)
        for r in rows:
            out[r["event_id"]].append(dict(r))
    return out


# ---------------------------------------------------------------------------
# source health
# ---------------------------------------------------------------------------
def get_source_status(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM source_status")}


def record_source_attempt(conn: sqlite3.Connection, source_id: str, *, result: str, error: str | None = None,
                          error_kind: str | None = None, items: int = 0, new: int = 0, duplicates: int = 0,
                          duration_ms: int | None = None, http_status: int | None = None,
                          etag: str | None = None, last_modified: str | None = None,
                          backoff_until: str | None = None) -> None:
    """Update a source's health after one collection attempt.

    result: ok | ok_empty | not_modified | partial | error | needs_credentials
      * ok / ok_empty / partial count as SUCCESS (the source answered and parsed).
        ok_empty is tracked separately: zero items is not a failure, but a long
        series of empty runs is suspicious (consecutive_empty).
      * not_modified (HTTP 304) is a success that keeps the previous validators.
      * error increments consecutive_failures (-> degraded, then failing at 3).
    Never touches stored documents: a failing source cannot delete collected data.
    """
    now = utcnow_iso()
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO source_status (source_id) VALUES (?)", (source_id,))
        common = dict(last_attempt_at=now, last_result=result, last_duration_ms=duration_ms,
                      last_http_status=http_status, backoff_until=backoff_until)
        if result in ("ok", "ok_empty", "partial"):
            sets = {**common, "status": "ok", "last_success_at": now, "consecutive_failures": 0,
                    "items_last_run": items, "new_last_run": new, "duplicates_last_run": duplicates,
                    # store exactly what the server sent this time (None clears a stale validator)
                    "etag": etag, "last_modified": last_modified,
                    "last_error_kind": error_kind if result == "partial" else None}
            if result == "partial":
                sets.update(last_error=error, last_error_at=now)
            conn.execute(_update_sql(sets) + ", consecutive_empty = CASE WHEN ? THEN consecutive_empty + 1 ELSE 0 END"
                         " WHERE source_id = ?", (*sets.values(), result == "ok_empty", source_id))
            return
        if result == "not_modified":
            sets = {**common, "status": "ok", "last_success_at": now, "consecutive_failures": 0,
                    "items_last_run": 0, "new_last_run": 0, "duplicates_last_run": 0,
                    "etag": etag, "last_modified": last_modified}
            conn.execute(_update_sql(sets) + " WHERE source_id = ?", (*sets.values(), source_id))
            return
        if result == "needs_credentials":
            sets = {**common, "status": "needs_credentials", "last_error": error, "last_error_kind": "credentials"}
            conn.execute(_update_sql(sets) + " WHERE source_id = ?", (*sets.values(), source_id))
            return
        sets = {**common, "last_error": error, "last_error_at": now, "last_error_kind": error_kind or "unexpected"}
        conn.execute(
            _update_sql(sets) + ", consecutive_failures = consecutive_failures + 1,"
            " status = CASE WHEN consecutive_failures + 1 >= 3 THEN 'failing' ELSE 'degraded' END WHERE source_id = ?",
            (*sets.values(), source_id))


def _update_sql(sets: dict[str, Any]) -> str:
    # keys are fixed column names from this module -- never user input
    return "UPDATE source_status SET " + ", ".join(f"{k} = ?" for k in sets)


# ---------------------------------------------------------------------------
# locks (single collector at a time)
# ---------------------------------------------------------------------------
def acquire_lock(conn: sqlite3.Connection, name: str, owner: str, ttl: timedelta = timedelta(minutes=30)) -> bool:
    """Atomically take a named lock. An expired lock (crashed run) is taken over."""
    now = datetime.now(timezone.utc)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT owner, expires_at FROM locks WHERE name = ?", (name,)).fetchone()
        if row and row["owner"] != owner and datetime.fromisoformat(row["expires_at"]) > now:
            conn.execute("ROLLBACK")
            return False
        conn.execute("INSERT OR REPLACE INTO locks (name, owner, acquired_at, expires_at) VALUES (?,?,?,?)",
                     (name, owner, to_iso(now), to_iso(now + ttl)))
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def release_lock(conn: sqlite3.Connection, name: str, owner: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))


def get_lock(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
    if row and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc):
        return dict(row)
    return None


# ---------------------------------------------------------------------------
# validations
# ---------------------------------------------------------------------------
def record_validation(conn: sqlite3.Connection, source_id: str, outcome: str, *, http_status: int | None,
                      documents: int, valid_documents: int, duration_ms: int | None, summary: str,
                      checks: dict[str, Any]) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO source_validations (source_id, validated_at, outcome, http_status, documents,
                   valid_documents, duration_ms, summary, checks) VALUES (?,?,?,?,?,?,?,?,?)""",
            (source_id, utcnow_iso(), outcome, http_status, documents, valid_documents, duration_ms, summary,
             json.dumps(checks, ensure_ascii=False, default=str)),
        )


def latest_validations(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT v.* FROM source_validations v
           JOIN (SELECT source_id, MAX(id) AS id FROM source_validations GROUP BY source_id) m ON m.id = v.id""")
    return {r["source_id"]: dict(r) for r in rows}


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
               error: str | None = None, status: str | None = None, notes: str | None = None, *,
               duplicates: int = 0, updated: int = 0, duration_ms: int | None = None,
               http_status: int | None = None, error_kind: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            """UPDATE ingest_runs SET finished_at=?, fetched=?, new=?, status=?, error=?, notes=?,
                   duplicates=?, updated=?, duration_ms=?, http_status=?, error_kind=? WHERE id=?""",
            (utcnow_iso(), fetched, new, status or ("error" if error else "ok"), error, notes,
             duplicates, updated, duration_ms, http_status, error_kind, run_id),
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
        r["source_country"] = r["publisher_country"]
        r["domain"] = r["publisher"]
        r["first_ingested_at"] = r["collected_at"]
    return rows
