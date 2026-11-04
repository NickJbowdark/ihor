"""Collection run: query GDELT for each region and store the results.

Usage (from the project root, with the virtual environment active):

    python -m gim.ingest                      # all regions, last 24 hours
    python -m gim.ingest --region europe --timespan 7d
    python -m gim.ingest --region africa --topic "ceasefire,peace talks"

Exit code is 0 if at least one region succeeded, 1 otherwise, so a scheduler
(Windows Task Scheduler, cron, systemd) can detect failures.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from gim.config import get_settings
from gim.logging_setup import setup_logging
from gim.regions import REGIONS, build_query
from gim.sources.gdelt import GdeltClient, GdeltError
from gim.storage import db

log = logging.getLogger("gim.ingest")


@dataclass
class RegionResult:
    region: str
    fetched: int = 0
    new: int = 0
    error: str | None = None


def ingest_regions(region_keys: list[str], timespan: str = "24h", topic: str | None = None,
                   max_records: int = 250, conn=None, client: GdeltClient | None = None) -> list[RegionResult]:
    settings = get_settings()
    own_conn = conn is None
    conn = conn or db.connect()
    client = client or GdeltClient(settings)
    results: list[RegionResult] = []
    try:
        for key in region_keys:
            region = REGIONS[key]
            query = build_query(region, topic=topic, language=settings.gdelt_language)
            run_id = db.start_run(conn, "gdelt", key, query, timespan)
            result = RegionResult(key)
            try:
                articles = client.search_articles(query, timespan=timespan, max_records=max_records)
                result.fetched = len(articles)
                result.new = db.save_articles(conn, articles, key)
                log.info("Region %s: fetched=%d new=%d", key, result.fetched, result.new,
                         extra={"region": key, "fetched": result.fetched, "new": result.new})
            except (GdeltError, ValueError) as exc:
                result.error = str(exc)
                log.error("Region %s failed: %s", key, exc, extra={"region": key})
            except Exception as exc:  # keep going with other regions, but record it
                result.error = f"{type(exc).__name__}: {exc}"
                log.exception("Unexpected error for region %s", key)
            db.finish_run(conn, run_id, result.fetched, result.new, result.error)
            results.append(result)
    finally:
        if own_conn:
            conn.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect geopolitical news metadata from GDELT.")
    parser.add_argument("--region", choices=["all", *REGIONS], default="all")
    parser.add_argument("--timespan", default="24h", help="e.g. 30min, 24h, 7d, 2w (GDELT max ~3 months)")
    parser.add_argument("--topic", default=None, help="optional comma-separated extra terms (OR'd)")
    parser.add_argument("--max-records", type=int, default=250, help="per region, max 250")
    args = parser.parse_args(argv)

    setup_logging()
    keys = list(REGIONS) if args.region == "all" else [args.region]
    results = ingest_regions(keys, args.timespan, args.topic, args.max_records)

    print("\nRegion          fetched   new   status")
    for r in results:
        status = "ok" if not r.error else f"ERROR: {r.error[:60]}"
        print(f"{r.region:<15} {r.fetched:>7} {r.new:>5}   {status}")
    return 0 if any(r.error is None for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
