"""Intelligence alerts with TRANSPARENT criteria (no predictions).

    python -m gim.alerts            # evaluate rules now (also runs after every collection)

Rules (each alert stores the exact numbers that triggered it in ``criteria``):
  volume_spike        documents mentioning a country in the last 24 h are
                      >= max(MIN_SPIKE_DOCS, mean + 3 x std) of the previous 28 days AND >= 2 x mean.
                      Needs >= 7 days of history; otherwise no alert (insufficient baseline).
  watchlist_match     a new/updated event matches an active watch (country, region,
                      organisation, keyword (full-text), or collection source).
  official_statement  a new official statement on a diplomatic / conflict / economic topic.
  multi_origin_event  an event reaches >= 3 independent origins (reported by several
                      apparently independent sources -- NOT a verification).
Alerts are de-duplicated (one per rule + subject + day/threshold).
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

from gim.logging_setup import setup_logging
from gim.storage import db, intel

MIN_SPIKE_DOCS = 8
SPIKE_SIGMA = 3.0
BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 7
MULTI_ORIGIN_THRESHOLD = 3
OFFICIAL_TOPICS = {"diplomatic", "conflict", "economic", "international_orgs"}


def _csv(v: str | None) -> set[str]:
    return {x for x in (v or "").split(",") if x}


def volume_spikes(conn, now: datetime) -> int:
    start = now - timedelta(days=BASELINE_DAYS + 1)
    counts: dict[str, dict[str, int]] = {}
    rows = conn.execute("SELECT substr(timeline_at,1,10) AS day, countries_mentioned, data_origin FROM documents "
                        "WHERE timeline_at >= ?", (db.to_iso(start),))
    recent_cut = db.to_iso(now - timedelta(hours=24))
    recent: dict[str, int] = {}
    origin_of: dict[str, set[str]] = {}
    for r in conn.execute("SELECT countries_mentioned, data_origin FROM documents WHERE timeline_at >= ?",
                          (recent_cut,)):
        for c in _csv(r["countries_mentioned"]):
            recent[c] = recent.get(c, 0) + 1
            origin_of.setdefault(c, set()).add(r["data_origin"])
    baseline_end = (now - timedelta(hours=24)).strftime("%Y-%m-%d")
    for r in rows:
        if r["day"] >= baseline_end:
            continue
        for c in _csv(r["countries_mentioned"]):
            counts.setdefault(c, {})
            counts[c][r["day"]] = counts[c].get(r["day"], 0) + 1
    new = 0
    first_day = conn.execute("SELECT MIN(substr(timeline_at,1,10)) FROM documents").fetchone()[0]
    if not first_day:
        return 0
    history_days = (now.date() - datetime.fromisoformat(first_day).date()).days
    if history_days < MIN_BASELINE_DAYS:
        return 0
    days = min(BASELINE_DAYS, history_days - 1)
    for country, today in recent.items():
        series = [counts.get(country, {}).get((now - timedelta(days=d + 1)).strftime("%Y-%m-%d"), 0)
                  for d in range(days)]
        mean = statistics.fmean(series) if series else 0.0
        std = statistics.pstdev(series) if len(series) > 1 else 0.0
        threshold = max(MIN_SPIKE_DOCS, mean + SPIKE_SIGMA * std)
        if today >= threshold and today >= 2 * mean:
            origins = origin_of.get(country, {"live"})
            new += intel.insert_alert(
                conn, rule="volume_spike", severity="warning" if today >= 3 * max(mean, 1) else "notice",
                title=f"Unusual reporting volume: {country}",
                detail=f"{today} documents mentioning {country} in the last 24 h vs. a {days}-day average of "
                       f"{mean:.1f}/day (std {std:.1f}). Volume measures attention, not event severity.",
                criteria={"country": country, "last_24h": today, "baseline_days": days, "mean": round(mean, 2),
                          "std": round(std, 2), "threshold": round(threshold, 2), "rule": f"count >= max({MIN_SPIKE_DOCS}, mean + {SPIKE_SIGMA}*std) and >= 2*mean"},
                dedup_key=f"spike:{country}:{now:%Y-%m-%d}",
                data_origin=origins.pop() if len(origins) == 1 else "mixed")
    return new


def event_rules(conn, now: datetime, lookback_hours: int = 48) -> int:
    since = db.to_iso(now - timedelta(hours=lookback_hours))
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE updated_at >= ?", (since,))]
    watches = intel.list_watches(conn)
    new = 0
    for e in events:
        ref = f"EVT-{e['id']}"
        origin = e["data_origin"]
        # multi-origin
        if e["independent_origins"] >= MULTI_ORIGIN_THRESHOLD:
            new += intel.insert_alert(
                conn, rule="multi_origin_event", severity="notice",
                title=f"Reported by {e['independent_origins']} independent origins: {e['headline'][:90]}",
                detail=f"{ref} has {e['documents_count']} documents from {e['independent_origins']} apparently "
                       f"independent origins ({e['corroboration_level']}). Independence is not verified.",
                criteria={"independent_origins": e["independent_origins"], "threshold": MULTI_ORIGIN_THRESHOLD},
                dedup_key=f"multi:{e['id']}:{MULTI_ORIGIN_THRESHOLD}", event_id=e["id"], data_origin=origin)
        # watchlists
        if watches:
            delivered = {r[0] for r in conn.execute(
                "SELECT DISTINCT ds.source_id FROM document_sources ds JOIN event_documents ed "
                "ON ed.document_id = ds.document_id WHERE ed.event_id = ?", (e["id"],))}
            for w in watches:
                hit = False
                if w["kind"] == "country":
                    hit = w["value"] in _csv(e["countries_mentioned"])
                elif w["kind"] == "region":
                    hit = w["value"] in _csv(e["regions"])
                elif w["kind"] == "organization":
                    hit = w["value"] in _csv(e["organizations"])
                elif w["kind"] == "source":
                    hit = w["value"] in delivered
                elif w["kind"] == "keyword":
                    q = intel.fts_query(w["value"])
                    hit = bool(q) and conn.execute(
                        "SELECT 1 FROM documents_fts f JOIN event_documents ed ON ed.document_id = f.rowid "
                        "WHERE documents_fts MATCH ? AND ed.event_id = ? LIMIT 1", (q, e["id"])).fetchone() is not None
                if hit:
                    new += intel.insert_alert(
                        conn, rule="watchlist_match", severity="info",
                        title=f"Watchlist '{w['name']}': {e['headline'][:100]}",
                        detail=f"{ref} matches watch {w['kind']} = {w['value']}.",
                        criteria={"watch_id": w["id"], "kind": w["kind"], "value": w["value"]},
                        dedup_key=f"watch:{w['id']}:{e['id']}", event_id=e["id"], data_origin=origin)
    # official statements
    docs = conn.execute(
        """SELECT d.id, d.title, d.publisher, d.categories, d.data_origin, ed.event_id FROM documents d
           LEFT JOIN event_documents ed ON ed.document_id = d.id
           WHERE d.doc_type = 'official_statement' AND d.collected_at >= ?""", (since,))
    for d in docs:
        topics = _csv(d["categories"]) & OFFICIAL_TOPICS
        if topics:
            new += intel.insert_alert(
                conn, rule="official_statement", severity="info",
                title=f"Official statement ({d['publisher'][:40]}): {d['title'][:90]}",
                detail=f"Official statement tagged {', '.join(sorted(topics))}. It documents the institution's "
                       "position; factual claims in it are not independently established.",
                criteria={"topics": sorted(topics)}, dedup_key=f"official:{d['id']}",
                event_id=d["event_id"], document_id=d["id"], data_origin=d["data_origin"])
    return new


def evaluate(conn, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    with db.transaction(conn):
        return volume_spikes(conn, now) + event_rules(conn, now)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    setup_logging()
    conn = db.connect()
    n = evaluate(conn)
    print(f"{n} new alert(s).")
    for a in intel.list_alerts(conn, unacknowledged=True, limit=20):
        print(f"[{a['severity']:<7}] {a['created_at'][:16]} {a['rule']:<18} {a['title'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
