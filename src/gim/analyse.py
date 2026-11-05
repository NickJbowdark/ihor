"""Analyse single events from the command line.

    python -m gim.analyse --event 42            # AI analysis (or baseline) of EVT-42, cached
    python -m gim.analyse --top 5 --days 2      # the 5 most prominent recent events
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from gim.analysis.engine import analyse_event, get_analyst
from gim.logging_setup import setup_logging
from gim.reports.render import Citations, claim_text
from gim.storage import db


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--event", type=int, action="append", default=[])
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--refresh", action="store_true", help="ignore cache")
    args = p.parse_args(argv)
    setup_logging()
    conn = db.connect()
    ids = list(args.event)
    if args.top:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        ids += [e["id"] for e in db.query_events(conn, since, order="prominence", limit=args.top)]
    if not ids:
        p.error("give --event N or --top N")
    analyst, problems = get_analyst(conn, prefer_ai=not args.no_ai)
    for m in problems:
        print(f"Note: {m}")
    for eid in ids:
        a = analyse_event(conn, eid, analyst, use_cache=not args.refresh)
        print(f"\n=== EVT-{eid} ({a['provider']}/{a['model']}{', cached' if a['cached'] else ''}) ===")
        cites = Citations({r: {} for r in a["evidence_refs"]})
        for c in a["claims"]:
            label, text = claim_text(c, cites)
            print(f"[{c['section']}] {label + ': ' if label else ''}{text}")
        print("Citations: " + ", ".join(f"[{n}]={ref}" for ref, n in cites.numbers.items()))
        if a["rejected"]:
            print(f"{len(a['rejected'])} statement(s) rejected by the validator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
