"""Database maintenance: backups, integrity checks, restore.

    python -m gim.maintenance backup              # consistent online backup (keeps the newest 14)
    python -m gim.maintenance check               # PRAGMA integrity_check + schema version
    python -m gim.maintenance list                # show backups
    python -m gim.maintenance restore <file> --yes

Backups use SQLite's backup API, so they are consistent even while the
dashboard or the collector is using the database. Restore first verifies the
backup's integrity, then saves a safety copy of the CURRENT database, and
refuses to run while a collector holds the lock.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from gim.config import get_settings
from gim.storage import db

KEEP = 14


def backup_dir() -> Path:
    return get_settings().db_path.parent / "backups"


def make_backup(conn: sqlite3.Connection | None = None, dest_dir: Path | None = None, keep: int = KEEP) -> Path:
    settings = get_settings()
    own = conn is None
    conn = conn or db.connect()
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = db.backup_database(conn, (dest_dir or backup_dir()) / f"{settings.db_path.stem}-{stamp}.sqlite3")
    finally:
        if own:
            conn.close()
    rotate(dest.parent, keep)
    return dest


def rotate(directory: Path, keep: int = KEEP) -> list[Path]:
    """Delete the oldest REGULAR backups beyond ``keep``. Pre-migration and pre-restore copies are kept."""
    regular = sorted(p for p in directory.glob("*.sqlite3") if "-pre-" not in p.name)
    removed = regular[:-keep] if len(regular) > keep else []
    for p in removed:
        p.unlink()
    return removed


def integrity(path: Path) -> tuple[bool, str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return result == "ok", result, version


def restore(backup: Path, *, target: Path | None = None) -> Path:
    target = target or get_settings().db_path
    ok, detail, version = integrity(backup)
    if not ok:
        raise SystemExit(f"Backup failed integrity check: {detail}")
    if version > db.SCHEMA_VERSION:
        raise SystemExit(f"Backup has schema v{version}, newer than this code (v{db.SCHEMA_VERSION}).")
    safety = None
    if target.exists():
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        try:
            if db._table_exists(conn, "locks") and db.get_lock(conn, "collector"):
                raise SystemExit("A collector is running (lock held). Stop it and try again.")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safety = db.backup_database(conn, target.parent / "backups" / f"{target.stem}-pre-restore-{stamp}.sqlite3")
        finally:
            conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup, target)
    return safety or target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    sub.add_parser("check")
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("file", type=Path)
    r.add_argument("--yes", action="store_true", help="confirm overwriting the current database")
    args = p.parse_args(argv)
    path = get_settings().db_path

    if args.cmd == "backup":
        print(f"Backup written: {make_backup()}")
    elif args.cmd == "check":
        if not path.exists():
            print(f"No database at {path}")
            return 1
        ok, detail, version = integrity(path)
        print(f"{path}: integrity={'ok' if ok else detail}, schema v{version} (code expects v{db.SCHEMA_VERSION})")
        return 0 if ok else 1
    elif args.cmd == "list":
        for b in sorted(backup_dir().glob("*.sqlite3")):
            print(f"{b.name:<60} {b.stat().st_size / 1e6:8.1f} MB")
    elif args.cmd == "restore":
        if not args.yes:
            print(f"This replaces {path} with {args.file}. A safety copy is made first. Re-run with --yes.")
            return 1
        safety = restore(args.file)
        print(f"Restored {args.file} -> {path}. Previous database saved as {safety}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
