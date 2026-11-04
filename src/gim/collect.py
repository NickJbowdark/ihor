"""Phase 2 collector: runs registry sources, stores documents, tracks source health.

Usage (project root, venv active):

    python -m gim.collect                       # every enabled source that is DUE
    python -m gim.collect --force               # every enabled source now
    python -m gim.collect --source un_news      # one source (even if disabled)
    python -m gim.collect --source bbc_world --dry-run    # fetch+parse, store nothing
    python -m gim.collect --source un_news --record       # also save raw response as a test fixture
    python -m gim.collect --status              # print the source health table

Exit code: 0 if no selected source failed, 1 otherwise (for schedulers).
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gim.config import PROJECT_ROOT
from gim.logging_setup import setup_logging
from gim.pipeline.normalize import clean_text
from gim.sources.base import Connector, FetchState, NeedsCredentials
from gim.sources.gdelt import GdeltConnector
from gim.sources.http import PoliteHttp
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


@dataclass
class SourceRunResult:
    source_id: str
    status: str               # ok | not_modified | needs_credentials | error | skipped
    fetched: int = 0
    new: int = 0
    updated: int = 0
    message: str = ""


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_due(spec: SourceSpec, status_row: dict | None, now: datetime | None = None) -> bool:
    if not status_row or not status_row.get("last_attempt_at"):
        return True
    now = now or datetime.now(timezone.utc)
    last = _parse_iso(status_row["last_attempt_at"])
    return now - last >= timedelta(minutes=spec.poll_minutes)


def health_label(spec: SourceSpec, row: dict | None, now: datetime | None = None) -> str:
    """Human status for the dashboard: adds 'planned', 'disabled', 'stale' to the stored status."""
    if not spec.implemented:
        return "planned"
    if spec.missing_credentials():
        return "needs_credentials"
    if not row:
        return "never_run" if spec.enabled else "disabled"
    status = row["status"]
    if status == "ok" and row.get("last_success_at"):
        age = (now or datetime.now(timezone.utc)) - _parse_iso(row["last_success_at"])
        if spec.enabled and age > timedelta(minutes=spec.poll_minutes * 3):
            return "stale"
    if status == "needs_credentials":  # credentials have since been added
        return "never_run"
    return status if spec.enabled or status != "ok" else "ok (disabled)"


def _record(spec: SourceSpec, raw: list[tuple[str, bytes]], record_dir: Path) -> list[Path]:
    record_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = []
    for label, body in raw:
        p = record_dir / f"{spec.id}__{stamp}__{label}"
        p.write_bytes(body)
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
    try:
        result = connector.fetch(state)
    except NeedsCredentials as exc:
        msg = f"needs credentials: {exc}"
        if not dry_run:
            db.record_source_attempt(conn, spec.id, status="needs_credentials", error=msg)
            db.finish_run(conn, run_id, status="skipped", error=msg)
        return SourceRunResult(spec.id, "needs_credentials", message=msg)
    except Exception as exc:  # any connector failure is recorded, never crashes the whole run
        msg = clean_text(f"{type(exc).__name__}: {exc}", 400)
        log.error("Source %s failed: %s", spec.id, msg, extra={"source": spec.id})
        if not dry_run:
            db.record_source_attempt(conn, spec.id, status="error", error=msg)
            db.finish_run(conn, run_id, error=msg)
        return SourceRunResult(spec.id, "error", message=msg)

    notes = "; ".join(result.notes) or None
    if record and result.raw:
        paths = _record(spec, result.raw, record_dir)
        notes = ((notes + "; ") if notes else "") + f"recorded {len(paths)} fixture file(s)"
        log.info("Recorded fixtures: %s", ", ".join(p.name for p in paths))
    if result.not_modified:
        if not dry_run:
            db.record_source_attempt(conn, spec.id, status="not_modified", etag=result.etag,
                                     last_modified=result.last_modified)
            db.finish_run(conn, run_id, status="not_modified", notes=notes)
        return SourceRunResult(spec.id, "not_modified", message="unchanged since last run (HTTP 304)")
    if dry_run:
        sample = "; ".join(d.title[:70] for d in result.documents[:3])
        return SourceRunResult(spec.id, "ok", fetched=len(result.documents),
                               message=f"DRY RUN, nothing stored. e.g.: {sample}" + (f" | {notes}" if notes else ""))
    new, updated = db.save_documents(conn, result.documents)
    db.record_source_attempt(conn, spec.id, status="ok", items=len(result.documents), new=new,
                             etag=result.etag, last_modified=result.last_modified)
    db.finish_run(conn, run_id, fetched=len(result.documents), new=new, notes=notes)
    log.info("Source %s: fetched=%d new=%d updated=%d", spec.id, len(result.documents), new, updated,
             extra={"source": spec.id, "fetched": len(result.documents), "new": new, "updated": updated})
    return SourceRunResult(spec.id, "ok", len(result.documents), new, updated, notes or "")


def collect(source_ids: list[str] | None = None, *, force: bool = False, dry_run: bool = False,
            record: bool = False, conn=None, http: PoliteHttp | None = None) -> list[SourceRunResult]:
    registry = get_registry()
    own = conn is None
    conn = conn or db.connect()
    http = http or PoliteHttp()
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
        return [run_source(s, conn, http, dry_run=dry_run, record=record) for s in specs]
    finally:
        if own:
            conn.close()


def print_status(conn=None) -> None:
    conn = conn or db.connect()
    status = db.get_source_status(conn)
    print(f"{'source':<22} {'state':<18} {'last success (UTC)':<26} {'items':>5}  last error")
    for spec in get_registry().values():
        row = status.get(spec.id)
        print(f"{spec.id:<22} {health_label(spec, row):<18} {(row or {}).get('last_success_at') or '-':<26} "
              f"{(row or {}).get('items_last_run', 0):>5}  {((row or {}).get('last_error') or '')[:60]}")


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
    results = collect(args.source, force=args.force, dry_run=args.dry_run, record=args.record)
    if not results:
        print("Nothing to do: no enabled source is due. Use --force or --source <id>.")
        return 0
    print(f"\n{'source':<22} {'status':<18} {'fetched':>7} {'new':>5} {'upd':>4}  message")
    for r in results:
        print(f"{r.source_id:<22} {r.status:<18} {r.fetched:>7} {r.new:>5} {r.updated:>4}  {r.message[:90]}")
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
