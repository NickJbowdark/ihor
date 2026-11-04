# Phase 2.1 — Reliability, events, provenance, AI contracts

*Written 2026-11-04. Schema v3. 79 automated tests pass (1 skipped until you record real responses).*

## 0. Tested vs. assumed (read this first)

| Status | What |
|---|---|
| ✅ **Tested in the sandbox** (automated, offline) | Migrations v1→v2→v3 with automatic backup and full rollback on failure · UTC normalisation and the future-timestamp guard · Retry-After (seconds and HTTP-date), long Retry-After → backoff instead of blocking · exponential backoff after failures · ETag/Last-Modified stored, sent, kept on 304, cleared when the server stops sending them · collector lock (overlap, expiry after a crash) · failing sources never delete stored data · empty vs. failed vs. partial runs · duplicate counting and durations · secret redaction in logs, the DB, reports and recorded fixtures · backup/rotate/restore/integrity · event grouping and corroboration on synthetic data · validation logic (schema + quality) · AI contract validation · all dashboard pages render without errors (headless browser, demo data) |
| ✅ **Observed in the sandbox with real network calls** | Connection failures are classified as `network`, recorded with redacted messages, and never reported as a pass; the credential appeared 0 times in the report, the DB and the logs |
| ⚠️ **NOT yet verified — needs your PC** | That any real endpoint (GDELT, UN News, Council, Commission, ReliefWeb, media feeds) returns the format the parsers expect · real-world precision of event grouping and wire-copy detection · how often real feeds omit dates, attribution or language · real servers' ETag/304 and Retry-After behaviour |

Every synthetic fixture is in `tests/fixtures/synthetic/` and every title starts with `[TEST FIXTURE]`.

---

## 1. Audit: what existed, what was missing, what changed

| Requirement | Before (Phase 2a) | Gap found | Now |
|---|---|---|---|
| **1. Real-world validation** | `--dry-run` per source; `verified` was a hand-edited TOML flag | No all-sources command, no schema or quality checks, so an HTTP 200 HTML error page could look like success | `python -m gim.validate`: credentials → connection → schema → per-document quality → PASS / FAIL / EMPTY / SKIPPED, plus a Markdown + JSON report. "Verified" exists **only** as a dated PASS in `source_validations`; it expires after 30 days. The TOML flag was removed and the config is never edited automatically |
| **2. Events vs. articles** | Headline-fingerprint grouping in the UI only | No event entity and no permanent IDs | `events` + `event_documents` tables, `pipeline/events.py` (deterministic, incremental, stable `EVT-` IDs), assigned after each collection |
| **3. Geography** | `countries` and `source_country` fields; a choropleth of **publisher** countries | Ambiguous names; a map that could be misread as event locations | Fields renamed to `countries_mentioned` / `publisher_country`. Events get `event_country` and `countries_involved`, which default to **unknown** and can only be set with an explicit `location_basis`. Publisher countries are now a bar chart; the map shows only events with an established location |
| **4. Independence** | Agency markers + identical-headline grouping | Unmarked verbatim copies of an agency story counted as independent; official claims weren't separated | `pipeline/corroboration.py`: origins (wire agency / official / shared copy / publisher), inferred agency attribution for verbatim copies, levels `official_only` / `single_origin` / `official_plus_reporting` / `multiple_origins` with explicit caveats. No per-organisation score |
| **5a. Migration safety** | `executescript` + implicit transactions | **Bug:** SQLite schema changes ran in autocommit, so a crash mid-migration could leave a half-migrated DB | Each step runs in one `BEGIN IMMEDIATE … COMMIT` including the version bump. An existing DB is backed up automatically before any upgrade |
| **5b. Backups** | Manual file copy | No tooling | `python -m gim.maintenance backup / check / list / restore`, a daily scheduled backup, and a safety copy before every restore |
| **5c. UTC** | Mostly consistent | A future timestamp from a broken source clock would sort to the top forever | All stored times are UTC ISO. Future times are kept for provenance, sorted by collection time, and flagged |
| **5d. 429 / Retry-After** | Seconds only, max 120 s sleep | HTTP-date ignored; long waits blocked the run; GDELT client ignored the header | Both formats parsed; ≤120 s waits are slept, longer ones end the attempt and set `backoff_until` |
| **5e. ETag / Last-Modified** | Implemented for RSS | **Bug:** a stale validator was kept when the server stopped sending one | Stores exactly what the server sent; keeps the old value on 304 |
| **5f. Overlapping jobs** | Task Scheduler `IgnoreNew` only | The dashboard button could run at the same time as the scheduler | DB-based collector lock (30-min expiry, refreshed per source); dashboard buttons disabled while it's held; CLI exit code 3 |
| **5g. Env vars / secrets** | Values never displayed | **Bug:** `requests` puts full URLs (incl. `appname=`) into exception text, which landed in logs and in `last_error` | `gim/security.py`: every credential env value + generic patterns redacted in logs (console + JSON), DB errors, validation reports and recorded fixtures |
| **5h. Failure recovery** | Errors recorded; documents untouched | No backoff; a partly failed GDELT run counted as plain success | Exponential backoff (poll × 2^failures, max 24 h); `partial` result; tests prove that stored documents survive every error type |
| **6. Monitoring** | State, last success, items, last error | No new/duplicate split, duration, HTTP code, error kind, freshness, verification, live/cached distinction, or empty-vs-failed | All of these on the Source health page and in `python -m gim.collect --status`; empty runs are "ok — 0 items", flagged only after 5 in a row |
| **7. AI preparation** | Design notes only | No data contract | `gim/analysis/`: frozen input contracts, an output contract with claim types, a deterministic `validate_result`, a provider protocol, a non-AI baseline provider, and a read-only bundle builder (live data only by default) |

---

## 2. Concepts as implemented

**Source → Document → Claim → Event**

- **Source:** a collection channel in `config/sources.toml`.
- **Document:** one URL (`DOC-<id>`), keeping its own publisher, original reporter, document type and timestamps. Documents are never merged or rewritten. Corrections are stored in `document_revisions`.
- **Claim:** a statement attributed to someone. Defined in `analysis/contracts.py`, extracted and checked in Phase 3.
- **Event:** a real-world happening (`EVT-<id>`), linking 1…n documents. Each link records the method (`seed`, `same_fingerprint`, `title_similarity`) and a similarity score, so every grouping decision can be audited.

**Event grouping (`deterministic-title-v1`).** Identical headline fingerprint, *or* ≥3 shared headline tokens with Jaccard ≥ 0.5 (countries count as normalised tokens), *and* compatible countries, within ±48 h of the event. The process is incremental, and events are never renumbered. Known limits: different wording for the same event may stay split, and identical generic headlines about different places are kept apart only if their countries differ.

**Four geography fields.**

| Field | Lives on | Meaning | Set by |
|---|---|---|---|
| `publisher_country` | document | where the outlet is based | GDELT metadata |
| `countries_mentioned` | document & event | names found in the text | gazetteer |
| `event_country` | event | where it happened | dataset / analyst / checked AI only (`location_basis`) |
| `countries_involved` | event | parties to it | same |

In the synthetic Geneva test, Switzerland is *mentioned* (the venue) but is not *involved*. That is exactly why the fields are separate.

**Corroboration levels** describe the sources and are never a truth score:

| Level | Meaning |
|---|---|
| `official_only` | the institution's position is documented; its factual claims are not independently established |
| `single_origin` | one producer of the information, however many copies exist |
| `official_plus_reporting` | an official statement plus one reporting origin |
| `multiple_origins` | ≥2 apparently independent origins; the label always says *independence not verified* |

---

## 3. AI interface (no AI implemented)

```
AnalysisRequest (frozen, request_id, window, contract_version)
 └─ EventBundle  EVT-id · headline · geography (None = unknown) · corroboration JSON · clustering_method
     ├─ EvidenceDocument  DOC-id · url · canonical_url · publisher · original_reporter · doc_type ·
     │                    published/updated/collected/event dates · countries_mentioned ·
     │                    publisher_country · content_hash · data_origin · delivered_by · provenance
     └─ EvidenceExcerpt   DOC-id#title / DOC-id#summary · verbatim stored text
AnalysisResult
 └─ Claim  text · claim_type (REPORTED_FACT | ATTRIBUTED_CLAIM | ASSESSMENT | CONTEXT) ·
           source_refs · event_ref · attributed_to · confidence · quote
```

`validate_result()` rejects any of the following, whichever model produced the output:
- citations of evidence that wasn't supplied
- quotes that aren't verbatim in the cited text
- attributed claims without a speaker
- assessments without a confidence level
- "context" that pretends to have sources

A provider is any object with `analyse(request) -> AnalysisResult`. Switching from DeepSeek to Ollama or another provider changes only `gim/analysis/providers.py` and `.env`. Ingestion never imports `gim.analysis`.

---

## 4. Windows: testing real sources step by step

Run in PowerShell, from the project root, with the venv active (`.\.venv\Scripts\Activate.ps1`).

**1. Update, reinstall, run the offline tests:**

```powershell
git pull
```

```powershell
pip install -e ".[dev]"
```

```powershell
python -m pytest
```

Expected: `79 passed, 1 skipped`.

**2. Back up the current database.** The schema upgrade also makes its own backup automatically:

```powershell
python -m gim.maintenance backup
```

```powershell
python -m gim.maintenance check
```

**3. Validate all enabled sources against the real internet.** This is the main command:

```powershell
python -m gim.validate --record
```

It prints a table and the path of the full report (`reports\validation\validation_<time>.md`). Open the report:

```powershell
notepad (Get-ChildItem reports\validation\*.md | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
```

**4. Turn the recorded real responses into regression tests:**

```powershell
python -m pytest tests/test_recorded.py -v
```

**5. Validate the disabled catalogue feeds as well** (media, FCDO, State Department):

```powershell
python -m gim.validate --all
```

For a feed that PASSES and that you want collected, set `enabled = true` in `config\sources.toml`.

**6. ReliefWeb**, after receiving your appname (put `GIM_RELIEFWEB_APPNAME=...` in `.env`):

```powershell
python -m gim.validate --source reliefweb_reports --record
```

**7. Collect for real, then look at health and events:**

```powershell
python -m gim.collect --force
```

```powershell
python -m gim.collect --status
```

```powershell
streamlit run app/dashboard.py
```

**8. Register the scheduler** (collection every 30 min + daily backup):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
```

### Reading validation outcomes

| Outcome | Meaning | What to do |
|---|---|---|
| PASS | schema ok, ≥1 document, ≥95 % pass the required checks, ≥80 % have usable timestamps | nothing; the source shows "verified <date>" |
| FAIL `network` | no connection (firewall, DNS, proxy, site down) | open the URL in a browser; retry later |
| FAIL `http_4xx` | 403/404: URL moved or access refused | find the current feed link on the site and update `url` |
| FAIL `rate_limited` | 429 | wait; do not lower poll intervals |
| FAIL `schema: not recognised as RSS/Atom` | HTTP 200, but an HTML page (cookie wall, redesign) | find the real feed URL |
| FAIL quality (`timestamp_in_future`, `attribution_missing`, …) | the parser or the source has a problem | send me the report; I'll fix the parser against the recorded response |
| EMPTY | valid but zero items | re-validate later; not a connection failure |
| SKIPPED `credentials` | `.env` lacks the key | add it, or ignore |

### Restoring a backup (rollback)

```powershell
python -m gim.maintenance list
```

```powershell
python -m gim.maintenance restore data\backups\<file>.sqlite3 --yes
```

The restore refuses to run while a collector holds the lock. It checks the backup's integrity first, and saves the current database as `*-pre-restore-*` before overwriting it. Pre-migration backups are named `*-pre-v3-from-v2-*` and are never rotated away.

---

## 5. Remaining limitations (honest list)

- Event grouping compares headlines only (not summaries). Expect missed merges for differently worded reports, which Phase 3 adds semantic similarity for. Also expect rare false merges for generic same-day headlines.
- Wire-copy detection needs an agency marker or an exact headline match with the agency's own copy. Rewritten agency stories without attribution look independent, so the label always says *independence not verified*.
- `countries_mentioned` comes from a gazetteer of about 140 countries and capitals. Ambiguous names (Georgia, Jordan, Chad, Niger) can mis-tag.
- The collector lock is per database file. Two PCs collecting into two different databases won't see each other (not a supported setup).
- ReliefWeb fetches the newest 100 reports per run (no pagination yet).
