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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gim.config import PROJECT_ROOT, get_settings
from gim.models import Document
from gim.pipeline.events import assign_events
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
    cluster = json.loads((FIXTURES / "event_cluster.json").read_text("utf-8"))
    docs += [article_to_document(a, "europe", "fixture") for a in parse_artlist(cluster)]
    return docs


SYNTH_TOPICS = {
    "diplomatic": "Diplomatic talks",
    "conflict": "Security incident",
    "political": "Election and government",
    "economic": "Trade and sanctions",
    "humanitarian": "Humanitarian situation",
    "international_orgs": "Security Council session",
}
# deliberately artificial publishers (reserved .test domain) and wire-agency markers
SYNTH_PUBLISHERS = [("daily-a.test", "United Kingdom"), ("courier-b.test", "United States"),
                    ("tribune-c.test", "India"), ("gazette-d.test", "France"), ("herald-e.test", "Kenya"),
                    ("times-f.test", "Brazil"), ("post-g.test", "Japan"), ("journal-h.test", "Germany")]
CODE_WORDS = ["ka", "lo", "mi", "ne", "pu", "ra", "si", "to", "vu", "ze", "bo", "da", "fe", "gi", "hu", "jo"]
SYNTH_COUNTRIES = ["Ukraine", "Russia", "Sudan", "Iran", "Israel", "China", "Taiwan", "Mali", "Niger", "India",
                   "Pakistan", "Venezuela", "Brazil", "Ethiopia", "Somalia", "Armenia", "Azerbaijan", "Serbia",
                   "Kosovo", "Yemen", "Saudi Arabia", "North Korea", "South Korea", "Haiti", "Myanmar"]


def synthetic_documents(days: int = 35, seed: int = 7, now: datetime | None = None) -> list[Document]:
    """Deterministic SYNTHETIC corpus for the demo dashboard.

    Headlines are obviously artificial ("[SYNTHETIC] story kalo mira tope: Diplomatic talks involving Sudan and Chad"):
    the corpus exercises maps, trends, events, alerts and reports WITHOUT inventing plausible
    geopolitical claims. Includes wire copies, official statements and a volume spike.
    """
    import random

    from gim.pipeline.attribution import classify_reporting
    from gim.pipeline.classify import classify_title
    from gim.pipeline.geo import COUNTRY_REGION, tag_places
    from gim.pipeline.normalize import canonicalize_url, title_fingerprint

    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    docs: list[Document] = []
    item = 0
    for day in range(days, -1, -1):
        n_stories = rng.randint(5, 9) + (6 if day == 0 else 0)
        for _ in range(n_stories):
            item += 1
            topic = rng.choice(list(SYNTH_TOPICS))
            # last day: spike in reporting mentioning one country (exercises the volume alert)
            first = "Sudan" if (day == 0 and rng.random() < 0.7) else rng.choice(SYNTH_COUNTRIES)
            second = rng.choice([c for c in SYNTH_COUNTRIES if c != first])
            hint = {"conflict": " (military)", "diplomatic": " (talks)", "economic": " (sanctions)",
                    "humanitarian": " (refugees)"}.get(topic, "")
            # three unique code words per story keep unrelated synthetic stories apart
            code = " ".join(rng.choice(CODE_WORDS) + rng.choice(CODE_WORDS) for _ in range(3))
            title = f"[SYNTHETIC] story {code}: {SYNTH_TOPICS[topic]}{hint} involving {first} and {second}"
            base_time = now - timedelta(days=day, hours=rng.uniform(2, 20))
            n_copies = rng.choice([1, 1, 1, 2, 3, 5])
            wire = rng.random() < 0.35
            official = topic in ("international_orgs", "economic") and rng.random() < 0.4
            for k in range(n_copies):
                pub, pub_country = rng.choice(SYNTH_PUBLISHERS)
                url = f"https://{pub}/synthetic/{item}-{k}"
                summary = "Synthetic summary text for testing. (Reuters) -" if wire else "Synthetic summary text."
                doc_type, reporter = classify_reporting(pub, f"{title} {summary}", "unknown")
                countries, regions = tag_places(title)
                docs.append(Document(
                    source_id="gdelt", url=url, canonical_url=canonicalize_url(url), title=title, publisher=pub,
                    source_category="news_discovery", doc_type=doc_type, language="en",
                    published_at=base_time + timedelta(minutes=17 * k), summary=summary,
                    original_reporter=reporter, publisher_country=pub_country, countries_mentioned=countries,
                    regions=regions or [COUNTRY_REGION.get(first, "global")],
                    categories=classify_title(title) or [topic], title_fingerprint=title_fingerprint(title),
                    data_origin="fixture", provenance={"synthetic": True, "generator": "gim.demo", "seed": seed}))
            if official:
                url = f"https://statements.test/synthetic/{item}"
                countries, regions = tag_places(title)
                docs.append(Document(
                    source_id="un_news", url=url, canonical_url=canonicalize_url(url), title=title,
                    publisher="Synthetic Institution (test)", source_category="official_un",
                    doc_type="official_statement", language="en", published_at=base_time + timedelta(hours=1),
                    summary="Synthetic official statement text.", original_reporter="Synthetic Institution (test)",
                    countries_mentioned=countries, regions=regions or ["global"],
                    categories=classify_title(title) or [topic], title_fingerprint=title_fingerprint(title),
                    data_origin="fixture", provenance={"synthetic": True, "generator": "gim.demo", "seed": seed}))
    return docs


def build_demo(path: Path = DEMO_DB, rich: bool = True) -> int:
    if path.resolve() == get_settings().db_path.resolve():
        raise SystemExit("Refusing to write fixtures into the configured LIVE database (GIM_DB_PATH).")
    if path.exists():
        path.unlink()
    docs = load_fixture_documents()
    newest = max(d.published_at for d in docs if d.published_at)
    shift = datetime.now(timezone.utc) - timedelta(hours=2) - newest
    for d in docs:
        d.data_origin = "fixture"
        d.published_at = d.published_at + shift if d.published_at else None
        d.updated_at = d.updated_at + shift if d.updated_at else None
        d.provenance["fixture_time_shift_hours"] = round(shift.total_seconds() / 3600, 1)
    if rich:
        docs += synthetic_documents()
    conn = db.connect(path)
    run = db.start_run(conn, "fixtures", "-", str(FIXTURES), "-", data_origin="fixture")
    new, _ = db.save_documents(conn, docs)
    db.finish_run(conn, run, fetched=len(docs), new=new, notes="synthetic fixtures loaded by gim.demo")
    assign_events(conn)
    from gim.alerts import evaluate
    from gim.storage import intel
    intel.add_watch(conn, "country", "Sudan", "Demo watch: Sudan")
    intel.add_watch(conn, "organization", "NATO", "Demo watch: NATO")
    evaluate(conn)
    conn.close()
    return new


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--small", action="store_true", help="only the hand-written fixtures (no synthetic corpus)")
    n = build_demo(rich=not p.parse_args().small)
    print(f"Demo database written to {DEMO_DB} with {n} synthetic documents.")
    print('PowerShell:  $env:GIM_DB_PATH="data/demo.sqlite3"; streamlit run app/dashboard.py')


if __name__ == "__main__":
    main()
