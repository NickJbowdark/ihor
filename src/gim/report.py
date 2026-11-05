"""Generate intelligence reports from the command line.

    python -m gim.report --type daily_brief
    python -m gim.report --type weekly_assessment --region europe --pdf reports/weekly.pdf
    python -m gim.report --type country_profile --country Ukraine --days 30
    python -m gim.report --type custom_topic --topic "Black Sea grain corridor" --days 60
    python -m gim.report --type daily_brief --no-ai          # deterministic baseline only
    python -m gim.report --list                              # previous reports
    python -m gim.report --export 12 --pdf report12.pdf      # export a stored report
    python -m gim.report --type daily_brief --email          # e-mail it (SMTP settings in .env)

Uses the AI provider configured in .env (DeepSeek / Ollama / ...). Without one, the
deterministic baseline is used and the report says so. Every report is a DRAFT until reviewed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gim.analysis.engine import get_analyst
from gim.analysis.llm import LLMError
from gim.logging_setup import setup_logging
from gim.reports.builder import ReportError, generate_report
from gim.reports.pdf import render_pdf
from gim.reports.specs import SPECS
from gim.storage import db, intel


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", choices=sorted(SPECS))
    p.add_argument("--region", action="append", default=[])
    p.add_argument("--country", action="append", default=[])
    p.add_argument("--topic")
    p.add_argument("--days", type=int)
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--include-synthetic", action="store_true", help="allow fixture data (demo databases)")
    p.add_argument("--pdf", type=Path, help="also write a PDF")
    p.add_argument("--md", type=Path, help="also write Markdown")
    p.add_argument("--email", action="store_true", help="send the report by e-mail (configure SMTP in .env)")
    p.add_argument("--list", action="store_true")
    p.add_argument("--export", type=int, help="export an existing report id")
    args = p.parse_args(argv)
    setup_logging()
    conn = db.connect()

    if args.list:
        for r in intel.list_reports(conn):
            print(f"#{r['id']:<4} {r['created_at'][:16]} {r['status']:<8} {r['data_origin']:<7} {r['title'][:80]}")
        return 0
    if args.export:
        rid = args.export
    else:
        if not args.type:
            p.error("--type is required")
        analyst, problems = get_analyst(conn, prefer_ai=not args.no_ai)
        for msg in problems:
            print(f"Note: {msg}")
        try:
            rid = generate_report(conn, args.type, analyst, regions=args.region, countries=args.country,
                                  topic=args.topic, days=args.days, live_only=not args.include_synthetic,
                                  progress=lambda i, n, t: print(f"[{i}/{n}] {t}"))
        except (ReportError, LLMError) as exc:
            print(f"Report not generated: {exc}")
            return 1
    rep = intel.get_report(conn, rid)
    if not rep:
        print(f"No report #{rid}")
        return 1
    print(f"\nReport #{rid}: {rep['title']} ({rep['status']}, data: {rep['data_origin']})")
    if args.md:
        args.md.write_text(rep["markdown"], encoding="utf-8")
        print(f"Markdown: {args.md}")
    if args.pdf:
        content = json.loads(rep["content"])
        content["status"] = rep["status"]
        args.pdf.parent.mkdir(parents=True, exist_ok=True)
        args.pdf.write_bytes(render_pdf(content))
        print(f"PDF: {args.pdf}")
    if args.email:
        from gim.emailer import EmailNotConfigured, send_report
        content = json.loads(rep["content"])
        content["status"] = rep["status"]
        try:
            to = send_report(f"[GIM {rep['status'].upper()}] {rep['title']}", rep["markdown"], render_pdf(content),
                             f"gim_report_{rid}.pdf")
            print(f"E-mailed to {', '.join(to)}")
        except EmailNotConfigured as exc:
            print(f"E-mail not sent: {exc}")
            return 1
        except Exception as exc:  # SMTP errors must not lose the stored report
            from gim.security import redact
            print(f"E-mail failed: {redact(str(exc))}")
            return 1
    if not args.md and not args.pdf and not args.email:
        print(rep["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
