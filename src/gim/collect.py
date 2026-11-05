"""Phase 2 collector: runs registry sources, stores documents, tracks source health.

Usage (project root, venv active):

    python -m gim.collect                       # every enabled source that is DUE
    python -m gim.collect --force               # every enabled source now
    python -m gim.collect --source un_news      # one source (even if disabled)
    python -m gim.collect --source bbc_world --dry-run    # fetch+parse, store nothing
    python -m gim.collect --source un_news --record       # also save raw response as a test fixture
    python -m gim.collect --status              # print the source health table

For a full diagnostic of every source (schema + document quality + report), use
``python -m gim.validate`` instead.

Reliability rules implemented here:
  * Only ONE collector runs at a time (database lock; a crashed run's lock expires).
  * A failing source never deletes or modifies stored documents.
  * Failed sources back off exponentially (poll interval x 2^failures, max 24 h);
    a server's long Retry-After is respected via ``backoff_until``.
  * Error messages are redacted before they reach logs or the database.

Exit code: 0 if no selected source failed, 1 otherwise, 3 if another collector holds the lock.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gim.config import PROJECT_ROOT
from gim.logging_setup import setup_logging
from gim.pipeline.normalize import clean_text
from gim.pipeline.events import assign_events
from gim.security import redact, redact_bytes
from gim.sources.base import Connector, FetchState, NeedsCredentials, ParseError
from gim.sources.gdelt import GdeltConnector, GdeltError
from gim.sources.http import FetchError, PoliteHttp
from gim.sources.registry import SourceSpec, get_registry
from gim.sources.reliefweb import ReliefWebConnector
from gim.sources.rss import RssConnector
from gim.storage import db

log = logging.getLogger("gim.collect")

CONNECTORS: dict[str, type[Connector]] = {
    "gdelt": GdeltConnector,
    "rss": RssConnector,
    "reliefweb": ReliefWebConnector,
}
RECORD_DIR = PROJECT_ROOT / "tests" / "fixtures" / "recorded"
LOCK_NAME = "collector"
MAX_BACKOFF = timedelta(hours=24)
SUSPICIOUS_EMPTY_RUNS = 5


@dataclass
class SourceRunResult:
    source_id: str
    status: str               # ok | ok_empty | partial | not_modified | needs_credentials | error | skipped | backoff
    fetched: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    duration_ms: int = 0
    error_kind: str | None = None
    message: str = ""


def classify_error(exc: BaseException) -> tuple[str, int | None, float | None]:
    """(error_kind, http_status, retry_after_seconds) for any connector exception."""
    if isinstance(exc, FetchError):
        return exc.kind, exc.status, exc.retry_after_s
    if isinstance(exc, (ParseError, ValueError, json.JSONDecodeError)):
        return "parse", None, None
    if isinstance(exc, GdeltError):
        msg = str(exc)
        return ("rate_limited" if "rate limit" in msg else "parse" if "rejected" in msg else "network"), None, None
    return "unexpected", None, None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_due(spec: SourceSpec, status_row: dict | None, now: datetime | None = None) -> bool:
    """Due when the poll interval has passed -- stretched exponentially after failures,
    and never before a server-requested backoff time."""
    if not status_row or not status_row.get("last_attempt_at"):
        return True
    now = now or datetime.now(timezone.utc)
    backoff_until = _parse_iso(status_row.get("backoff_until"))
    if backoff_until and now < backoff_until:
        return False
    failures = int(status_row.get("consecutive_failures") or 0)
    interval = min(timedelta(minutes=spec.poll_minutes) * (2 ** min(failures, 6)), max(MAX_BACKOFF, timedelta(minutes=spec.poll_minutes)))
    return now - _parse_iso(status_row["last_attempt_at"]) >= interval


def health_label(spec: SourceSpec, row: dict | None, now: datetime | None = None) -> str:
    """Operational state for the dashboard.

    planned | disabled | needs_credentials | never_run | ok | ok_empty | empty_suspicious |
    partial | stale | degraded | failing | backoff
    """
    if not spec.implemented:
        return "planned"
    if spec.missing_credentials():
        return "needs_credentials"
    if not row:
        return "never_run" if spec.enabled else "disabled"
    now = now or datetime.now(timezone.utc)
    status = row["status"]
    if status == "needs_credentials":  # credentials have since been added
        return "never_run"
    backoff_until = _parse_iso(row.get("backoff_until"))
    if status in ("degraded", "failing"):
        return "backoff" if backoff_until and backoff_until > now and status == "degraded" else status
    if row.get("last_success_at") and spec.enabled:
        age = now - _parse_iso(row["last_success_at"])
        if age > timedelta(minutes=spec.poll_minutes * 3):
            return "stale"
    if int(row.get("consecutive_empty") or 0) >= SUSPICIOUS_EMPTY_RUNS:
        return "empty_suspicious"
    last = row.get("last_result") or "ok"
    if last in ("ok_empty", "partial"):
        return last
    return "ok" if spec.enabled else "ok (disabled)"


def _record(spec: SourceSpec, raw: list[tuple[str, bytes]], record_dir: Path) -> list[Path]:
    record_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = []
    for label, body in raw:
        p = record_dir / f"{spec.id}__{stamp}__{label}"
        p.write_bytes(redact_bytes(body))  # never store credentials inside fixtures
        paths.append(p)
    return paths


def run_source(spec: SourceSpec, conn, http: PoliteHttp, *, dry_run: bool = False, record: bool = False,
               connector: Connector | None = None, record_dir: Path = RECORD_DIR) -> SourceRunResult:
    if not spec.implemented:
        return SourceRunResult(spec.id, "skipped", message="connector not implemented yet (planned)")
    status_row = db.get_source_status(conn).get(spec.id) or {}
    state = FetchState(
        etag=None if (dry_run or record) else status_row.get("etag"),
        last_modified=None if (dry_run or record) else status_row.get("last_modified"),
        last_success_at=_parse_iso(status_row.get("last_success_at")),
    )
    connector = connector or CONNECTORS[spec.connector](spec, http)
    run_id = None if dry_run else db.start_run(conn, spec.id, ",".join(spec.default_regions) or "-", spec.url, "-")
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = connector.fetch(state)
    except NeedsCredentials as exc:
        msg = f"needs credentials: {exc}"
        if not dry_run:
            db.record_source_attempt(conn, spec.id, result="needs_credentials", error=msg, duration_ms=elapsed())
            db.finish_run(conn, run_id, status="skipped", error=msg, error_kind="credentials", duration_ms=elapsed())
        return SourceRunResult(spec.id, "needs_credentials", error_kind="credentials", message=msg)
    except Exception as exc:  # any connector failure is recorded, never crashes the whole run
        kind, http_status, retry_after = classify_error(exc)
        msg = redact(clean_text(f"{type(exc).__name__}: {exc}", 400))
        backoff = (datetime.now(timezone.utc) + timedelta(seconds=retry_after)) if retry_after else None
        log.error("Source %s failed (%s): %s", spec.id, kind, msg, extra={"source": spec.id, "error_kind": kind})
        if not dry_run:
            db.record_source_attempt(conn, spec.id, result="error", error=msg, error_kind=kind,
                                     duration_ms=elapsed(), http_status=http_status,
                                     backoff_until=db.to_iso(backoff))
            db.finish_run(conn, run_id, error=msg, error_kind=kind, duration_ms=elapsed(), http_status=http_status)
        return SourceRunResult(spec.id, "error", duration_ms=elapsed(), error_kind=kind, message=msg)

    notes = redact("; ".join(result.notes)) or None
    if record and result.raw:
        paths = _record(spec, result.raw, record_dir)
        notes = ((notes + "; ") if notes else "") + f"recorded {len(paths)} fixture file(s)"
        log.info("Recorded fixtures: %s", ", ".join(p.name for p in paths))
    if result.not_modified:
        if not dry_run:
            db.record_source_attempt(conn, spec.id, result="not_modified", etag=result.etag,
                                     last_modified=result.last_modified, duration_ms=elapsed(), http_status=304)
            db.finish_run(conn, run_id, status="not_modified", notes=notes, duration_ms=elapsed(), http_status=304)
        return SourceRunResult(spec.id, "not_modified", duration_ms=elapsed(),
                               message="unchanged since last run (HTTP 304) -- stored copies remain current")
    fetched = len(result.documents)
    outcome = "partial" if result.partial else ("ok_empty" if fetched == 0 else "ok")
    if dry_run:
        sample = "; ".join(d.title[:70] for d in result.documents[:3])
        return SourceRunResult(spec.id, outcome, fetched=fetched, duration_ms=elapsed(),
                               message=f"DRY RUN, nothing stored. e.g.: {sample}" + (f" | {notes}" if notes else ""))
    new, updated = db.save_documents(conn, result.documents)
    duplicates = fetched - new - updated
    db.record_source_attempt(conn, spec.id, result=outcome, items=fetched, new=new, duplicates=duplicates,
                             etag=result.etag, last_modified=result.last_modified, duration_ms=elapsed(),
                             http_status=200, error=notes if outcome == "partial" else None,
                             error_kind="partial" if outcome == "partial" else None)
    db.finish_run(conn, run_id, fetched=fetched, new=new, notes=notes, status=outcome, duplicates=duplicates,
                  updated=updated, duration_ms=elapsed(), http_status=200)
    log.info("Source %s: %s fetched=%d new=%d updated=%d duplicates=%d", spec.id, outcome, fetched, new, updated,
             duplicates, extra={"source": spec.id, "fetched": fetched, "new": new, "duplicates": duplicates})
    return SourceRunResult(spec.id, outcome, fetched, new, updated, duplicates, elapsed(), message=notes or "")


def collect(source_ids: list[str] | None = None, *, force: bool = False, dry_run: bool = False,
            record: bool = False, conn=None, http: PoliteHttp | None = None) -> list[SourceRunResult]:
    """Run sources. Raises CollectorBusy if another collector holds the lock."""
    registry = get_registry()
    own = conn is None
    conn = conn or db.connect()
    http = http or PoliteHttp()
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    locked = False
    try:
        if source_ids:
            unknown = [s for s in source_ids if s not in registry]
            if unknown:
                raise SystemExit(f"Unknown source id(s): {', '.join(unknown)}. See config/sources.toml")
            specs = [registry[s] for s in source_ids]
        else:
            status = db.get_source_status(conn)
            specs = [s for s in registry.values()
                     if s.enabled and s.implemented and (force or is_due(s, status.get(s.id)))]
        if not specs:
            return []
        if not dry_run:
            if not db.acquire_lock(conn, LOCK_NAME, owner):
                raise CollectorBusy(db.get_lock(conn, LOCK_NAME))
            locked = True
        results = []
        for spec in specs:
            results.append(run_source(spec, conn, http, dry_run=dry_run, record=record))
            if locked:
                db.acquire_lock(conn, LOCK_NAME, owner)  # refresh expiry after each source
        if not dry_run:
            stats = assign_events(conn)
            log.info("Event assignment: %s", stats, extra=stats)
            from gim.alerts import evaluate as evaluate_alerts  # local import: alerts -> storage only
            try:
                log.info("Alerts: %d new", evaluate_alerts(conn))
            except Exception:  # alerts must never break collection
                log.exception("Alert evaluation failed")
        return results
    finally:
        if locked:
            db.release_lock(conn, LOCK_NAME, owner)
        if own:
            conn.close()


class CollectorBusy(RuntimeError):
    def __init__(self, lock: dict | None):
        held = f" (held by {lock['owner']} since {lock['acquired_at']}, expires {lock['expires_at']})" if lock else ""
        super().__init__("another collection is already running" + held)
        self.lock = lock


def print_status(conn=None) -> None:
    from gim.validate import verification_label  # local import: validate imports this module

    conn = conn or db.connect()
    status = db.get_source_status(conn)
    validations = db.latest_validations(conn)
    print(f"{'source':<22} {'state':<18} {'verification':<26} {'last success (UTC)':<26} "
          f"{'items':>5} {'new':>4} {'dup':>4} {'ms':>6}  last error")
    for spec in get_registry().values():
        row = status.get(spec.id) or {}
        print(f"{spec.id:<22} {health_label(spec, row or None):<18} "
              f"{verification_label(validations.get(spec.id)):<26} {row.get('last_success_at') or '-':<26} "
              f"{row.get('items_last_run', 0):>5} {row.get('new_last_run', 0):>4} {row.get('duplicates_last_run', 0):>4} "
              f"{row.get('last_duration_ms') or 0:>6}  {(row.get('last_error_kind') or '')} {(row.get('last_error') or '')[:50]}")
    lock = db.get_lock(conn, LOCK_NAME)
    if lock:
        print(f"\nCollector running: {lock['owner']} since {lock['acquired_at']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run GIM source connectors.")
    p.add_argument("--source", action="append", help="source id (repeatable). Default: all enabled & due")
    p.add_argument("--force", action="store_true", help="ignore poll intervals")
    p.add_argument("--dry-run", action="store_true", help="fetch and parse, store nothing")
    p.add_argument("--record", action="store_true", help="save raw responses to tests/fixtures/recorded/")
    p.add_argument("--status", action="store_true", help="print source health and exit")
    args = p.parse_args(argv)

    setup_logging()
    if args.status:
        print_status()
        return 0
    try:
        results = collect(args.source, force=args.force, dry_run=args.dry_run, record=args.record)
    except CollectorBusy as exc:
        print(f"Skipped: {exc}")
        return 3
    if not results:
        print("Nothing to do: no enabled source is due. Use --force or --source <id>.")
        return 0
    print(f"\n{'source':<22} {'status':<18} {'fetched':>7} {'new':>5} {'upd':>4} {'dup':>5} {'ms':>6}  message")
    for r in results:
        print(f"{r.source_id:<22} {r.status:<18} {r.fetched:>7} {r.new:>5} {r.updated:>4} {r.duplicates:>5} "
              f"{r.duration_ms:>6}  {r.message[:90]}")
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
