"""SQL for the intelligence layer (schema v4): archive search, statistics, watchlists,
alerts, AI cost ledger, cached analyses and reports.

Kept separate from db.py (collection/storage core) for readability; same rules:
parameterised SQL only, UTC ISO timestamps, no business logic.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from gim.storage.db import to_iso, transaction, utcnow_iso

# ---------------------------------------------------------------------------
# archive search (FTS5 / BM25)
# ---------------------------------------------------------------------------
_FTS_TOKEN = re.compile(r"[\w-]+", re.UNICODE)


def fts_query(text: str) -> str:
    """Turn free user text into a SAFE FTS5 query (every token quoted -> no FTS syntax injection).

    'Armenia Azerbaijan talks' -> '"armenia" OR "azerbaijan" OR "talks"'
    """
    from gim.pipeline.normalize import _STOPWORDS
    tokens = [t.lower() for t in _FTS_TOKEN.findall(text) if len(t) > 2 and t.lower() not in _STOPWORDS][:24]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))


def search_documents(conn: sqlite3.Connection, text: str, *, before: datetime | None = None,
                     after: datetime | None = None, limit: int = 50,
                     data_origins: list[str] | None = None) -> list[dict[str, Any]]:
    q = fts_query(text)
    if not q:
        return []
    where, params = ["documents_fts MATCH ?"], [q]
    if before:
        where.append("d.timeline_at < ?")
        params.append(to_iso(before))
    if after:
        where.append("d.timeline_at >= ?")
        params.append(to_iso(after))
    if data_origins:
        where.append(f"d.data_origin IN ({','.join('?' * len(data_origins))})")
        params.extend(data_origins)
    rows = conn.execute(
        f"""SELECT d.*, bm25(documents_fts) AS rank, ed.event_id
            FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid
            LEFT JOIN event_documents ed ON ed.document_id = d.id
            WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?""", (*params, int(limit)))
    return [dict(r) for r in rows]


def search_events(conn: sqlite3.Connection, text: str, *, before: datetime | None = None,
                  after: datetime | None = None, limit: int = 20, exclude_event: int | None = None,
                  data_origins: list[str] | None = None) -> list[dict[str, Any]]:
    """Events whose documents best match ``text`` (best document rank per event)."""
    docs = search_documents(conn, text, before=before, after=after, limit=limit * 8, data_origins=data_origins)
    best: dict[int, float] = {}
    for d in docs:
        if d["event_id"] and d["event_id"] != exclude_event:
            best[d["event_id"]] = min(best.get(d["event_id"], 0.0), d["rank"])
    if not best:
        return []
    ids = sorted(best, key=best.get)[:limit]
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return [{**rows[i], "match_rank": best[i]} for i in ids if i in rows]


# ---------------------------------------------------------------------------
# statistics for dashboards / maps / alerts
# ---------------------------------------------------------------------------
def daily_counts(conn: sqlite3.Connection, since: datetime, *, country: str | None = None,
                 region: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
    """Documents per UTC day (optionally for documents mentioning a country / in a region / with a topic)."""
    where, params = ["d.timeline_at >= ?"], [to_iso(since)]
    if country:
        where.append("(',' || d.countries_mentioned || ',') LIKE ?")
        params.append(f"%,{country},%")
    if region:
        where.append("d.id IN (SELECT document_id FROM document_regions WHERE region = ?)")
        params.append(region)
    if category:
        where.append("(',' || d.categories || ',') LIKE ?")
        params.append(f"%,{category},%")
    rows = conn.execute(
        f"""SELECT substr(d.timeline_at, 1, 10) AS day, COUNT(*) AS documents,
                   COUNT(DISTINCT ed.event_id) AS events
            FROM documents d LEFT JOIN event_documents ed ON ed.document_id = d.id
            WHERE {' AND '.join(where)} GROUP BY day ORDER BY day""", params)
    return [dict(r) for r in rows]


def country_mention_stats(conn: sqlite3.Connection, since: datetime, until: datetime | None = None,
                          data_origins: list[str] | None = None) -> list[dict[str, Any]]:
    """Per country: events MENTIONING it (attention), not events located there."""
    where, params = ["last_published_at >= ?"], [to_iso(since)]
    if until:
        where.append("first_published_at <= ?")
        params.append(to_iso(until))
    if data_origins:
        where.append(f"data_origin IN ({','.join('?' * len(data_origins))})")
        params.extend(data_origins)
    counts: dict[str, dict[str, Any]] = {}
    for r in conn.execute(f"SELECT countries_mentioned, categories, independent_origins, prominence FROM events "
                          f"WHERE {' AND '.join(where)}", params):
        for c in (r["countries_mentioned"] or "").split(","):
            if not c:
                continue
            s = counts.setdefault(c, {"country": c, "events": 0, "conflict_events": 0, "max_prominence": 0.0})
            s["events"] += 1
            if "conflict" in (r["categories"] or ""):
                s["conflict_events"] += 1
            s["max_prominence"] = max(s["max_prominence"], r["prominence"])
    return sorted(counts.values(), key=lambda x: -x["events"])


def located_events(conn: sqlite3.Connection, since: datetime, until: datetime | None = None) -> list[dict[str, Any]]:
    where, params = ["last_published_at >= ?", "event_country IS NOT NULL"], [to_iso(since)]
    if until:
        where.append("first_published_at <= ?")
        params.append(to_iso(until))
    return [dict(r) for r in conn.execute(f"SELECT * FROM events WHERE {' AND '.join(where)}", params)]


# ---------------------------------------------------------------------------
# watchlists
# ---------------------------------------------------------------------------
WATCH_KINDS = {"country", "region", "keyword", "organization", "source"}


def add_watch(conn: sqlite3.Connection, kind: str, value: str, name: str | None = None) -> int:
    if kind not in WATCH_KINDS:
        raise ValueError(f"kind must be one of {sorted(WATCH_KINDS)}")
    value = value.strip()
    if not value or len(value) > 200:
        raise ValueError("value must be 1-200 characters")
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO watchlists (name, kind, value, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, value) DO UPDATE SET active = 1, name = excluded.name",
            (name or f"{kind}: {value}", kind, value, utcnow_iso()))
    return int(cur.lastrowid or 0)


def list_watches(conn: sqlite3.Connection, active_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM watchlists" + (" WHERE active = 1" if active_only else "") + " ORDER BY kind, value"
    return [dict(r) for r in conn.execute(sql)]


def remove_watch(conn: sqlite3.Connection, watch_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE watchlists SET active = 0 WHERE id = ?", (watch_id,))


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------
def insert_alert(conn: sqlite3.Connection, *, rule: str, severity: str, title: str, detail: str,
                 criteria: dict[str, Any], dedup_key: str, event_id: int | None = None,
                 document_id: int | None = None, data_origin: str = "live") -> bool:
    """Insert once per dedup_key. Returns True if new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO alerts (rule, severity, title, detail, criteria, event_id, document_id,
               created_at, dedup_key, data_origin) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (rule, severity, title, detail, json.dumps(criteria, ensure_ascii=False, default=str), event_id,
         document_id, utcnow_iso(), dedup_key, data_origin))
    return cur.rowcount == 1


def list_alerts(conn: sqlite3.Connection, *, since: datetime | None = None, unacknowledged: bool = False,
                limit: int = 300) -> list[dict[str, Any]]:
    where, params = ["1=1"], []
    if since:
        where.append("created_at >= ?")
        params.append(to_iso(since))
    if unacknowledged:
        where.append("acknowledged_at IS NULL")
    rows = conn.execute(f"SELECT * FROM alerts WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                        (*params, int(limit)))
    return [dict(r) for r in rows]


def acknowledge_alert(conn: sqlite3.Connection, alert_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE alerts SET acknowledged_at = ? WHERE id = ?", (utcnow_iso(), alert_id))


# ---------------------------------------------------------------------------
# AI cost ledger
# ---------------------------------------------------------------------------
def record_llm_call(conn: sqlite3.Connection, *, provider: str, model: str, purpose: str, input_tokens: int,
                    output_tokens: int, cost_usd: float, duration_ms: int | None, ok: bool,
                    error: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO llm_calls (at, provider, model, purpose, input_tokens, output_tokens, cost_usd,
                   duration_ms, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (utcnow_iso(), provider, model, purpose, input_tokens, output_tokens, cost_usd, duration_ms,
             int(ok), error))


def month_spend(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    r = conn.execute(
        """SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost_usd,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens
           FROM llm_calls WHERE at >= ?""", (to_iso(start),)).fetchone()
    return dict(r)


def recent_llm_calls(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------------------
# cached event analyses
# ---------------------------------------------------------------------------
def get_event_analysis(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM event_analyses WHERE cache_key = ?", (cache_key,)).fetchone()
    return dict(r) if r else None


def latest_event_analysis(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM event_analyses WHERE event_id = ? ORDER BY id DESC LIMIT 1", (event_id,)).fetchone()
    return dict(r) if r else None


def save_event_analysis(conn: sqlite3.Connection, *, event_id: int, cache_key: str, provider: str, model: str,
                        prompt_version: str, content: dict, rejected: list) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT OR REPLACE INTO event_analyses (event_id, cache_key, provider, model, prompt_version,
                   created_at, content, rejected) VALUES (?,?,?,?,?,?,?,?)""",
            (event_id, cache_key, provider, model, prompt_version, utcnow_iso(),
             json.dumps(content, ensure_ascii=False), json.dumps(rejected, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
REPORT_STATUSES = ("draft", "reviewed", "final")


def save_report(conn: sqlite3.Connection, *, report_type: str, title: str, scope: dict, window_start: str,
                window_end: str, provider: str, model: str, content: dict, markdown: str,
                data_origin: str) -> int:
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO reports (report_type, title, scope, window_start, window_end, created_at, provider,
                   model, content, markdown, data_origin) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (report_type, title, json.dumps(scope, ensure_ascii=False), window_start, window_end, utcnow_iso(),
             provider, model, json.dumps(content, ensure_ascii=False), markdown, data_origin))
    return int(cur.lastrowid)


def list_reports(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, report_type, title, scope, window_start, window_end, created_at, status, provider, model, "
        "data_origin, reviewed_at FROM reports ORDER BY id DESC LIMIT ?", (limit,))]


def get_report(conn: sqlite3.Connection, report_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return dict(r) if r else None


def set_report_status(conn: sqlite3.Connection, report_id: int, status: str, note: str = "") -> None:
    if status not in REPORT_STATUSES:
        raise ValueError(f"status must be one of {REPORT_STATUSES}")
    with transaction(conn):
        conn.execute("UPDATE reports SET status = ?, reviewer_note = ?, reviewed_at = ? WHERE id = ?",
                     (status, note, utcnow_iso() if status != "draft" else None, report_id))


def window(days: float, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=days), now
