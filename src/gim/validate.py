"""Real-world source validation with a readable diagnostic report.

    python -m gim.validate                      # all ENABLED implemented sources
    python -m gim.validate --all                # also disabled (catalogued RSS feeds etc.)
    python -m gim.validate --source un_news     # one source (repeatable)
    python -m gim.validate --record             # also save redacted raw responses as test fixtures

For every source it checks, in order:
  1. credentials present (else SKIPPED -- not a failure of the source)
  2. connection / HTTP           -> error kind: network, http_4xx, http_5xx, rate_limited
  3. response SCHEMA             -> is this really an RSS/Atom feed / the expected JSON?
  4. DOCUMENT QUALITY per item   -> title, safe URL, timestamp (present, not in the future),
                                    publisher, document type, attribution where required
  5. outcome:
       PASS   schema ok, >= 1 document, >= 95 % pass the required checks,
              >= 80 % carry a usable timestamp
       EMPTY  valid response but zero documents (inconclusive -- not a connection failure)
       FAIL   anything else, with the reasons listed
A successful HTTP response alone is NEVER a pass.

Results are written to the database (table source_validations -- this is what
the dashboard shows as "verified") and to reports/validation/ as Markdown + JSON.
Nothing is stored in the document tables and config/sources.toml is never edited.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

from gim.collect import CONNECTORS, RECORD_DIR, _record, classify_error
from gim.config import PROJECT_ROOT
from gim.logging_setup import setup_logging
from gim.models import Document
from gim.pipeline.normalize import is_safe_url
from gim.security import redact
from gim.sources.base import FetchState, NeedsCredentials
from gim.sources.http import PoliteHttp
from gim.sources.registry import DOC_TYPES, SourceSpec, get_registry
from gim.storage import db

REPORT_DIR = PROJECT_ROOT / "reports" / "validation"
REQUIRED_PASS_RATE = 0.95
TIMESTAMP_RATE = 0.80
VERIFIED_MAX_AGE = timedelta(days=30)
FUTURE_TOLERANCE = timedelta(hours=1)
OLD_WARNING = timedelta(days=5 * 365)


@dataclass
class SourceValidation:
    source_id: str
    name: str
    outcome: str = "fail"                 # pass | fail | empty | skipped
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_kind: str | None = None
    http_status: int | None = None
    duration_ms: int = 0
    documents: int = 0
    valid_documents: int = 0
    with_timestamp: int = 0
    schema_problems: list[str] = field(default_factory=list)
    field_failures: dict[str, int] = field(default_factory=dict)
    samples: list[dict] = field(default_factory=list)
    recorded: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# schema checks on the RAW response
# ---------------------------------------------------------------------------
def schema_problems(spec: SourceSpec, raw: list[tuple[str, bytes]]) -> list[str]:
    problems: list[str] = []
    for label, body in raw:
        if spec.connector == "rss":
            parsed = feedparser.parse(body)
            if not parsed.get("version"):
                problems.append(f"{label}: not recognised as RSS/Atom (HTML page or error message?)")
            elif parsed.bozo:
                problems.append(f"{label}: feed is malformed but readable "
                                f"({type(parsed.get('bozo_exception')).__name__}) -- warning-level")
            missing = sum(1 for e in parsed.entries if not e.get("link") or not e.get("title"))
            if missing:
                problems.append(f"{label}: {missing}/{len(parsed.entries)} entries lack link or title")
        else:
            try:
                data = json.loads(body)
            except ValueError:
                problems.append(f"{label}: not JSON")
                continue
            if spec.connector == "reliefweb":
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    problems.append(f"{label}: missing 'data' list")
                    continue
                if not isinstance(data.get("totalCount"), int):
                    problems.append(f"{label}: missing integer 'totalCount' (API format changed?)")
                bad = sum(1 for i in data["data"] if not isinstance(i, dict) or "title" not in (i.get("fields") or {}))
                if bad:
                    problems.append(f"{label}: {bad} items without fields.title")
            elif spec.connector == "gdelt":
                if not isinstance(data, dict) or ("articles" in data and not isinstance(data["articles"], list)):
                    problems.append(f"{label}: missing 'articles' list")
                    continue
                bad = sum(1 for a in data.get("articles", []) if not {"url", "title", "seendate"} <= set(a))
                if bad:
                    problems.append(f"{label}: {bad} articles missing url/title/seendate")
    # 'malformed but readable' alone does not fail a source
    return problems


def _blocking(problems: list[str]) -> list[str]:
    return [p for p in problems if "warning-level" not in p]


# ---------------------------------------------------------------------------
# per-document quality checks
# ---------------------------------------------------------------------------
def document_failures(doc: Document, now: datetime) -> tuple[list[str], list[str], bool]:
    """(required_failures, warnings, has_usable_timestamp)."""
    req: list[str] = []
    warn: list[str] = []
    if not (3 <= len(doc.title) <= 500):
        req.append("title_length")
    if not is_safe_url(doc.url):
        req.append("url_unsafe")
    if not doc.publisher:
        req.append("publisher_missing")
    if doc.doc_type not in DOC_TYPES:
        req.append("doc_type_invalid")
    if doc.doc_type in ("official_statement", "syndicated") and not doc.original_reporter:
        req.append("attribution_missing")
    ts_ok = doc.published_at is not None
    if doc.published_at is None:
        warn.append("timestamp_missing")
    elif doc.published_at.tzinfo is None:
        req.append("timestamp_not_utc")
        ts_ok = False
    elif doc.published_at > now + FUTURE_TOLERANCE:
        req.append("timestamp_in_future")
        ts_ok = False
    elif doc.published_at < now - OLD_WARNING:
        warn.append("timestamp_older_than_5y")
    if not doc.language:
        warn.append("language_missing")
    if doc.source_category != "news_discovery" and not doc.summary:
        warn.append("summary_missing")
    return req, warn, ts_ok


def evaluate(spec: SourceSpec, docs: list[Document], raw: list[tuple[str, bytes]],
             now: datetime | None = None) -> SourceValidation:
    """Pure evaluation of a fetched response (unit-testable without network)."""
    now = now or datetime.now(timezone.utc)
    v = SourceValidation(spec.id, spec.name)
    v.schema_problems = schema_problems(spec, raw)
    v.documents = len(docs)
    for d in docs:
        req, warn, ts_ok = document_failures(d, now)
        for f in req + warn:
            v.field_failures[f] = v.field_failures.get(f, 0) + 1
        if not req:
            v.valid_documents += 1
        if ts_ok:
            v.with_timestamp += 1
    v.samples = [{"title": d.title[:120], "url": d.url, "published_at": db.to_iso(d.published_at),
                  "publisher": d.publisher, "original_reporter": d.original_reporter, "doc_type": d.doc_type,
                  "countries_mentioned": d.countries_mentioned[:5]} for d in docs[:3]]
    blocking = _blocking(v.schema_problems)
    if blocking:
        v.reasons += [f"schema: {p}" for p in blocking]
    if v.documents == 0 and not blocking:
        v.outcome = "empty"
        v.reasons.append("valid response, but zero documents extracted (inconclusive -- try again later)")
        return v
    if v.documents:
        rate = v.valid_documents / v.documents
        ts_rate = v.with_timestamp / v.documents
        if rate < REQUIRED_PASS_RATE:
            v.reasons.append(f"only {rate:.0%} of documents pass required checks (need {REQUIRED_PASS_RATE:.0%})")
        if ts_rate < TIMESTAMP_RATE:
            v.reasons.append(f"only {ts_rate:.0%} of documents have a usable timestamp (need {TIMESTAMP_RATE:.0%})")
    for name, n in sorted(v.field_failures.items()):
        (v.warnings if name in ("timestamp_missing", "timestamp_older_than_5y", "language_missing",
                                "summary_missing") else v.reasons).append(f"{name}: {n} document(s)")
    v.warnings += [p for p in v.schema_problems if "warning-level" in p]
    v.outcome = "pass" if not v.reasons else "fail"
    return v


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------
def validate_source(spec: SourceSpec, http: PoliteHttp, *, record: bool = False, connector=None,
                    record_dir: Path = RECORD_DIR) -> SourceValidation:
    if not spec.implemented:
        return SourceValidation(spec.id, spec.name, "skipped", ["connector planned, not implemented"])
    missing = spec.missing_credentials()
    if missing:
        return SourceValidation(spec.id, spec.name, "skipped", [f"credentials missing: set {', '.join(missing)} in .env"],
                                error_kind="credentials")
    connector = connector or CONNECTORS[spec.connector](spec, http)
    started = time.monotonic()
    try:
        result = connector.fetch(FetchState())  # no ETag: always fetch a full response
    except NeedsCredentials as exc:
        return SourceValidation(spec.id, spec.name, "skipped", [str(exc)], error_kind="credentials")
    except Exception as exc:
        kind, status, _ = classify_error(exc)
        v = SourceValidation(spec.id, spec.name, "fail", [f"{kind}: {redact(str(exc))[:300]}"],
                             error_kind=kind, http_status=status)
        v.duration_ms = int((time.monotonic() - started) * 1000)
        return v
    v = evaluate(spec, result.documents, result.raw)
    v.http_status = 200
    v.duration_ms = int((time.monotonic() - started) * 1000)
    if result.partial:
        v.warnings += [f"partial: {redact(n)}" for n in result.notes]
    if record and result.raw:
        v.recorded = [p.name for p in _record(spec, result.raw, record_dir)]
    return v


def verification_label(row: dict | None, now: datetime | None = None) -> str:
    """Dashboard wording for a source's latest validation."""
    if not row:
        return "unverified"
    when = datetime.fromisoformat(row["validated_at"])
    day = when.strftime("%Y-%m-%d")
    if row["outcome"] == "pass":
        if (now or datetime.now(timezone.utc)) - when > VERIFIED_MAX_AGE:
            return f"verification expired ({day})"
        return f"verified {day}"
    return {"fail": f"FAILED {day}", "empty": f"inconclusive (empty) {day}",
            "skipped": f"not testable {day}"}.get(row["outcome"], row["outcome"])


def write_report(results: list[SourceValidation], report_dir: Path = REPORT_DIR) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md = [f"# GIM source validation — {stamp} (UTC)", "",
          "| Source | Outcome | HTTP | Docs | Valid | Timestamp | ms | Main reason |",
          "|---|---|---|---|---|---|---|---|"]
    for v in results:
        md.append(f"| `{v.source_id}` | **{v.outcome.upper()}** | {v.http_status or '-'} | {v.documents} | "
                  f"{v.valid_documents} | {v.with_timestamp} | {v.duration_ms} | "
                  f"{(v.reasons[0] if v.reasons else '').replace('|', '/')[:120]} |")
    for v in results:
        md += ["", f"## {v.name} (`{v.source_id}`) — {v.outcome.upper()}"]
        if v.error_kind:
            md.append(f"- Error kind: `{v.error_kind}`")
        md += [f"- Reason: {r}" for r in v.reasons] + [f"- Warning: {w}" for w in v.warnings]
        if v.schema_problems:
            md += [f"- Schema: {p}" for p in v.schema_problems]
        for s in v.samples:
            md.append(f"- Sample: {s['published_at'] or 'no date'} · {s['doc_type']} · {s['publisher']}"
                      f"{' / ' + s['original_reporter'] if s['original_reporter'] else ''} · {s['title']} <{s['url']}>")
        if v.recorded:
            md.append(f"- Recorded fixtures: {', '.join(v.recorded)}")
    md_path = report_dir / f"validation_{stamp}.md"
    json_path = report_dir / f"validation_{stamp}.json"
    md_path.write_text(redact("\n".join(md)) + "\n", encoding="utf-8")
    json_path.write_text(redact(json.dumps([asdict(v) for v in results], indent=2, ensure_ascii=False)),
                         encoding="utf-8")
    return md_path, json_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Validate GIM sources against real responses.")
    p.add_argument("--source", action="append", help="source id (repeatable)")
    p.add_argument("--all", action="store_true", help="all implemented sources, including disabled ones")
    p.add_argument("--record", action="store_true", help="save redacted raw responses to tests/fixtures/recorded/")
    args = p.parse_args(argv)
    setup_logging()
    registry = get_registry()
    if args.source:
        unknown = [s for s in args.source if s not in registry]
        if unknown:
            print(f"Unknown source id(s): {', '.join(unknown)}")
            return 2
        specs = [registry[s] for s in args.source]
    else:
        specs = [s for s in registry.values() if s.implemented and (args.all or s.enabled)]
    http = PoliteHttp()
    conn = db.connect()
    results = []
    for spec in specs:
        print(f"Validating {spec.id} ...", flush=True)
        v = validate_source(spec, http, record=args.record)
        results.append(v)
        db.record_validation(conn, spec.id, v.outcome, http_status=v.http_status, documents=v.documents,
                             valid_documents=v.valid_documents, duration_ms=v.duration_ms,
                             summary=redact("; ".join(v.reasons))[:500],
                             checks=json.loads(redact(json.dumps(asdict(v), default=str))))
    md_path, _ = write_report(results)
    print(f"\n{'source':<22} {'outcome':<8} {'http':>4} {'docs':>5} {'valid':>5} {'ts':>4} {'ms':>6}  reason")
    for v in results:
        print(f"{v.source_id:<22} {v.outcome.upper():<8} {v.http_status or '-':>4} {v.documents:>5} "
              f"{v.valid_documents:>5} {v.with_timestamp:>4} {v.duration_ms:>6}  {(v.reasons or [''])[0][:70]}")
    print(f"\nFull report: {md_path}")
    return 0 if all(v.outcome in ("pass", "skipped") for v in results) else 1


if __name__ == "__main__":
    sys.exit(main())
