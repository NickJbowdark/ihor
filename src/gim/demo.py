"""Build a DEMO database from the synthetic test fixtures.

    python -m gim.demo                 # creates data/demo.sqlite3
    $env:GIM_DB_PATH="data/demo.sqlite3"; streamlit run app/dashboard.py

Every document is stored with data_origin='fixture', and the dashboard shows a
DEMO banner. Timestamps are shifted so the newest fixture item is "now" (the
shift is recorded in each document's provenance). Never mix demo data into
your real database: this command refuses to write to the configured live DB.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from gim.config import PROJECT_ROOT, get_settings
from gim.models import Document
from gim.sources.gdelt import article_to_document, parse_artlist
from gim.sources.registry import get_registry
from gim.sources.reliefweb import parse_reports
from gim.sources.rss import parse_feed
from gim.storage import db

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "synthetic"
DEMO_DB = PROJECT_ROOT / "data" / "demo.sqlite3"


def load_fixture_documents() -> list[Document]:
    reg = get_registry()
    docs: list[Document] = []
    for sid in ("un_news", "eu_council_press", "eu_commission_press"):
        docs += parse_feed((FIXTURES / f"{sid}.xml").read_bytes(), reg[sid])
    docs += parse_reports(json.loads((FIXTURES / "reliefweb_reports.json").read_text("utf-8")))
    data = json.loads((FIXTURES / "gdelt_artlist.json").read_text("utf-8"))
    docs += [article_to_document(a, "europe" if "NATO" in a.title else "middle_east", "fixture")
             for a in parse_artlist(data)]
    return docs


def build_demo(path: Path = DEMO_DB) -> int:
    if path.resolve() == get_settings().db_path.resolve():
        raise SystemExit("Refusing to write fixtures into the configured LIVE database (GIM_DB_PATH).")
    if path.exists():
        path.unlink()
    docs = load_fixture_documents()
    newest = max(d.published_at for d in docs if d.published_at)
    shift = datetime.now(timezone.utc) - newest
    for d in docs:
        d.data_origin = "fixture"
        d.published_at = d.published_at + shift if d.published_at else None
        d.updated_at = d.updated_at + shift if d.updated_at else None
        d.provenance["fixture_time_shift_hours"] = round(shift.total_seconds() / 3600, 1)
    conn = db.connect(path)
    run = db.start_run(conn, "fixtures", "-", str(FIXTURES), "-", data_origin="fixture")
    new, _ = db.save_documents(conn, docs)
    db.finish_run(conn, run, fetched=len(docs), new=new, notes="synthetic fixtures loaded by gim.demo")
    conn.close()
    return new


def main() -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    n = build_demo()
    print(f"Demo database written to {DEMO_DB} with {n} synthetic documents.")
    print('PowerShell:  $env:GIM_DB_PATH="data/demo.sqlite3"; streamlit run app/dashboard.py')


if __name__ == "__main__":
    main()
