# Phase 2 — Sources, connectors and provenance

> **Updated by Phase 2.1:** validation, events, geography fields, reliability and Windows testing are now described in [PHASE2_1_RELIABILITY.md](PHASE2_1_RELIABILITY.md). Where the two documents differ, Phase 2.1 is authoritative. In particular, sources are verified by `python -m gim.validate`, not by editing `verified` in the TOML (that field was removed).

*Status: increment 2a implemented: source registry, connector framework, GDELT, UN News, Council of the EU, European Commission, ReliefWeb (needs appname), source health, demo mode. Written 2026-11-04.*

> **Testing status, stated plainly.** The development sandbox could not reach any external source (every outbound request was refused by its proxy). **No connector has yet been run against a live response.** All automated tests use hand-written fixtures in `tests/fixtures/synthetic/` that imitate each source's documented format, and every fixture title starts with `[TEST FIXTURE]`. Section 7 explains how to verify each source on your Windows PC and turn real responses into regression tests.

---

## 1. Audit of the Phase 1 repository

| Finding | Problem | Phase 2 change |
|---|---|---|
| `articles` table was GDELT-shaped (title, domain, seen date) | No room for summaries, original reporter, document type, event date, or update tracking | New general **`documents`** table (schema v2) plus automatic migration of existing data |
| `ingest.py` hard-wired to GDELT | Every new source would have needed its own script | **Connector interface** (`sources/base.py`) plus a single **collector** (`gim.collect`) |
| No record of which sources exist or how they're doing | No way to see that a feed broke three days ago | **Registry** (`config/sources.toml`) plus a **`source_status`** table and a Source health page |
| Region tagging = "which GDELT query found it" | RSS / official documents arrive without a region | Gazetteer tagger (`pipeline/geo.py`): countries, capitals and regional bodies, then region |
| Syndication handled only by identical headlines | Wire copy with a changed headline wasn't detected | Agency-marker detection (`pipeline/attribution.py`) gives `syndicated` + `original_reporter` |
| Publication vs. event time existed only as a comment | — | Separate `published_at`, `updated_at`, `event_date`, `collected_at`, `last_seen_at` columns |
| No indication of where data came from | Demo data could be mistaken for real data | `data_origin` per document, a red DEMO banner, and a separate demo database |
| Kept (it worked) | — | Rate-limited GDELT client, cleaning of untrusted input, URL canonicalisation, parameterised SQL, all 17 Phase 1 tests (unchanged and passing) |

---

## 2. Principles built into the data model

1. **Discovery ≠ evidence.** GDELT and media feeds tell you *that* something is being reported. Official sources tell you *what an institution says*. Datasets (ACLED/UCDP) give *coded events under a stated method*. Each document carries `source_category` and `doc_type`, so the analysis layer can weigh them differently.
2. **Five document types** (`doc_type`):
   - `original_reporting` — the outlet's own journalism
   - `syndicated` — a reprint of someone else's report (`original_reporter` names the agency)
   - `official_statement` — a government / IO communication: authoritative about the *position*, not automatically about the facts it asserts
   - `dataset_record` — a row from a structured dataset, with that dataset's definitions
   - `analysis_opinion` — assessments, analysis, op-eds
   - plus `unknown`, honestly, for GDELT items whose origin can't be determined from metadata.
3. **No reliability score per organisation.** The registry intentionally has no such field, and a test enforces this. Phase 3 assesses evidence per claim: who says it, on what basis, how independent the sources are, how specific the claim is, and whether there are contradictions.
4. **Copies are not confirmations.** The *Story groups* view counts publishers and "distinct origins" separately. Syndicated items carry the agency name, so 150 reprints of one AP story show up as one origin.
5. **Provenance is append-only.** The original URL, the source, the timestamps, which collection sources delivered the document (`document_sources`), and earlier versions after corrections (`document_revisions`) are all kept.
6. **Data origin is always visible.** `live` vs `fixture` is stored per document, and the dashboard says which you're looking at and when collection last succeeded.

---

## 3. Source catalogue (what is in `config/sources.toml`)

✅ implemented & enabled · 🔑 implemented, needs credentials · 🧪 implemented as RSS, disabled until you verify the feed · 📝 catalogued, connector planned

| Source | Status | Access (as researched; verify) | Notes |
|---|---|---|---|
| GDELT DOC 2.0 | ✅ | Free, no key, ≤1 req/5 s | Discovery only |
| UN News (all) | ✅ | RSS, free | Official UN communication |
| Council of the EU press releases | ✅ | RSS, free; EU reuse policy | Sanctions packages, Council conclusions |
| European Commission Press Corner | ✅ | RSS, free; CC BY 4.0 | High volume of internal EU items |
| ReliefWeb reports | 🔑 | API v2, **pre-approved appname** (mandatory since 1 Nov 2025) | publisher = ReliefWeb, original_reporter = author org |
| BBC World, Al Jazeera, France 24, DW, Africanews | 🧪 | Public RSS, personal non-commercial use; headlines + links only | Feed URLs from memory/docs, **unverified** |
| UK FCDO, US State Dept | 🧪 | GOV.UK Atom (OGL v3); state.gov RSS (public domain) | Unverified URLs |
| Reuters, AP | 📝 | **No public RSS**, content is commercially licensed | Seen via GDELT; reprints detected as `syndicated` |
| NHK World, NATO, EEAS, French / German / Ukrainian MFA, UN Security Council | 📝 | Public sites; RSS locations to be confirmed | — |
| ACLED | 📝 | myACLED account, OAuth password grant, Bearer token; API access may need enabling on your account | Never republish rows |
| UCDP GED | 📝 | Token by e-mail, header `x-ucdp-access-token`, version pinned in the URL | Historical baseline, not real-time |
| HDX HAPI, UNHCR | 📝 | app_identifier / no key | Low-frequency, cached |
| World Bank, IMF, Eurostat | 📝 | Free | Keep reference period + release date |
| OFAC SLS, EU FSF sanctions list | 📝 | Free downloads | Track list *changes* |
| NASA FIRMS, Copernicus | 📝 | MAP_KEY / OAuth | Later phase. Thermal anomaly ≠ attack |

**What I couldn't verify, and how you do it:** run `python -m gim.validate --all`. Sources that PASS are recorded as verified, with a date. For a disabled feed that passes, set `enabled = true` if you want it collected. If one fails, find the real feed link on the source's website and update `url`.

---

## 4. Code structure added in Phase 2

```
config/sources.toml            # THE registry: static facts about every source
src/gim/
  collect.py                   # collector CLI: due-scheduling, health, dry-run, --record
  demo.py                      # builds data/demo.sqlite3 from synthetic fixtures
  models.py                    # + Document (general provenance model)
  sources/
    registry.py                # loads + validates sources.toml
    http.py                    # PoliteHttp: per-host rate limit, retries, 304s, size cap
    base.py                    # Connector interface, FetchState/FetchResult, NeedsCredentials
    gdelt.py                   # + GdeltConnector, article_to_document
    rss.py                     # generic RSS/Atom connector (feedparser on downloaded bytes)
    reliefweb.py               # ReliefWeb API v2 connector
  pipeline/
    geo.py                     # gazetteer place tagging -> countries, regions
    attribution.py             # wire-agency detection -> syndicated / original_reporter
  storage/db.py                # schema v2, migration, save/query documents, source_status
app/dashboard.py               # pages: Overview · Source health · Source catalogue
tests/fixtures/synthetic/      # hand-written format imitations ([TEST FIXTURE] titles)
tests/fixtures/recorded/       # YOUR real responses (git-ignored), auto-tested
```

**Adding a source** to an existing connector type means one `[[source]]` block in the TOML. **Adding a new connector type** means one subclass of `Connector` with a `fetch()` that returns `Document`s, one line in `collect.CONNECTORS`, one fixture, and one test.

---

## 5. Schema v2 (summary)

| Table | Purpose |
|---|---|
| `documents` | One row per canonical URL. Columns: `source_id`, `external_id`, `url`, `canonical_url`, `title`, `summary`, `publisher`, `original_reporter`, `doc_type`, `source_category`, `language`, `source_country`, `countries`, `categories`, `title_fingerprint`, `content_hash`, `published_at`, `updated_at`, `event_date`, `timeline_at`, `collected_at`, `last_seen_at`, `data_origin`, `provenance` (JSON) |
| `document_regions` | Many regions per document |
| `document_sources` | Which collection sources delivered this URL (e.g. GDELT *and* UN News) |
| `document_revisions` | Earlier title/summary versions when a source corrects an item |
| `source_status` | Per source: status, last attempt / success / error, consecutive failures, item counts, ETag / Last-Modified |
| `ingest_runs` | Audit log of every run (+ `data_origin`, `notes`) |

Migration from v1 runs automatically the first time Phase 2 code opens an old database. GDELT articles become `documents` with `doc_type='unknown'`, and their provenance is marked `migrated_from: v1`. **Back up `data\gim.sqlite3` first** (copy the file).

**Merge rule when two sources deliver the same URL:** keep one row, record both sources, fill empty fields from the richer copy (e.g. the RSS summary fills in a GDELT headline-only row, and an `unknown` type becomes `official_statement`). A change by the *same* source creates a revision.

**Planned for 2b:** a `dataset_events` table for ACLED/UCDP. These sources are not documents but coded events with their own IDs, event dates, precision codes, actors, fatality estimates and dataset version. They'll be linked to documents later, during event clustering.

---

## 6. Implementation steps

| Step | Content | State |
|---|---|---|
| **2a** | Registry, connector framework, polite HTTP, GDELT/UN News/Council/Commission/ReliefWeb, schema v2 + migration, health page, demo mode, recorded-fixture tests | ✅ code done — **needs your live verification (§7)** |
| **2b** | `dataset_events` table; **UCDP** connector (token, version-pinned, paged) then **ACLED** connector (OAuth + refresh, weekly window); comparison view that keeps each dataset's definitions separate | next, once you have the tokens |
| **2c** | Enable verified media feeds; add multilingual feeds (France 24 FR, DW DE, Al Jazeera Arabic…), NATO / EEAS / MFA feeds; per-source keyword filters for high-volume feeds | after 2a verification |
| **2d** | Economic & sanctions: World Bank / Eurostat indicators (reference period + release date), OFAC & EU sanctions list change tracking | after 2b |
| **2e** | SQLite FTS5 full-text search; full statement text for official sources whose licence permits it (EU, US, UK OGL) | before Phase 3 |

---

## 7. How to test each source on Windows

With the virtual environment active, from the project root. First, pull the new code and reinstall (a new dependency, `feedparser`, was added):

```powershell
git pull
```

```powershell
pip install -e ".[dev]"
```

```powershell
python -m pytest
```

Expected (Phase 2.1): `79 passed, 1 skipped` (the skipped test waits for your recorded responses).

**Back up your Phase 1 database** before the automatic migration:

```powershell
Copy-Item data\gim.sqlite3 data\gim-phase1-backup.sqlite3
```

**Verify one source at a time** with a dry run (fetches and parses, stores nothing):

```powershell
python -m gim.collect --source un_news --dry-run
```

You should see `ok` and a few sample titles. Then do the same for `eu_council_press`, `eu_commission_press` and `gdelt`. If a source reports `ParseError: not a valid RSS/Atom feed`, the URL now returns a web page instead of a feed: open it in your browser and find the RSS link on the site.

**Record a real response as a regression fixture:**

```powershell
python -m gim.collect --source un_news --dry-run --record
```

```powershell
python -m pytest tests/test_recorded.py -v
```

Recorded files land in `tests\fixtures\recorded\`. They are git-ignored, because they contain third-party content.

**ReliefWeb:** request an appname at https://apidoc.reliefweb.int/, put it in `.env` as `GIM_RELIEFWEB_APPNAME=...`, then:

```powershell
python -m gim.collect --source reliefweb_reports --dry-run
```

**Real collection and health:**

```powershell
python -m gim.collect --force
```

```powershell
python -m gim.collect --status
```

**Look at the demo** (synthetic data, separate database, red banner):

```powershell
python -m gim.demo
```

```powershell
$env:GIM_DB_PATH="data/demo.sqlite3"; streamlit run app/dashboard.py
```

Back to live data in the same PowerShell window:

```powershell
Remove-Item Env:GIM_DB_PATH; streamlit run app/dashboard.py
```

**After a source passes** `python -m gim.validate`, it shows as *verified <date>* on the Source health page. No manual flag is needed. For disabled media feeds that pass, set `enabled = true`.

**Scheduling:** re-run `scripts\schedule_windows.ps1`. It now calls `python -m gim.collect` every 30 minutes, and each source is fetched only when its own `poll_minutes` interval has passed.

---

## 8. Known limitations (by design, for now)

- **Place tagging records mentions, not event locations.** "Russia condemns US strike in Yemen" is tagged with three countries. Ambiguous names (Georgia, Jordan, Chad) can mis-tag.
- **Wire detection relies on markers.** A reprint with all attribution stripped is not detected. Identical-headline grouping catches some of these.
- **Topic tags are still keyword rules.** They're the baseline that the Phase 3 AI classifier must beat.
- **Summaries are the source's own feed summary,** capped at 600 characters. GIM does not fetch full article pages.
- **ReliefWeb fetches the newest 100 reports per run.** After a long pause, older items in the gap are skipped (a note is logged). Pagination is a small follow-up.
