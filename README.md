# Global Intelligence Monitor (GIM)

A personal, AI-assisted **geopolitical OSINT platform**. It continuously collects open-source reporting on international developments, organises it with full source provenance, and (from Phase 3) drafts source-cited intelligence briefs for analyst review.

> **Status: Phase 2a.** Source registry (32 catalogued sources) · connectors for GDELT, UN News, Council of the EU, European Commission, ReliefWeb · provenance-rich document store (schema v2) · Source health page · demo mode.
>
> ⚠️ The connectors have been tested against **synthetic fixtures only** (the development sandbox had no network access). Verify them on your machine: see [docs/PHASE2_SOURCES.md §7](docs/PHASE2_SOURCES.md#7-how-to-test-each-source-on-windows).

## Quick start (Windows)

See **[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)** for the full step-by-step guide. In short:

```powershell
py -3.12 -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

```powershell
pip install -e ".[dev]"
```

```powershell
Copy-Item .env.example .env
```

```powershell
python -m pytest
```

```powershell
python -m gim.collect --source un_news --dry-run
```

```powershell
python -m gim.collect --force
```

```powershell
streamlit run app/dashboard.py
```

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**: feasibility, architecture, data-source strategy, AI model evaluation (incl. DeepSeek V4/V4.1), hosting and budget, roadmap, validation, security.
- **[docs/PHASE2_SOURCES.md](docs/PHASE2_SOURCES.md)**: Phase 1 audit, source catalogue, document types and provenance model, schema v2, next steps, per-source Windows testing.
- **[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)**: installation and first run.

## Project layout

```
gim/
├── app/dashboard.py          # Streamlit UI: Overview · Source health · Source catalogue
├── config/sources.toml       # SOURCE REGISTRY: every source, its access rules, licence, poll interval
├── src/gim/
│   ├── collect.py            # collector CLI (python -m gim.collect)
│   ├── demo.py               # demo database from synthetic fixtures
│   ├── config.py · logging_setup.py · models.py · regions.py
│   ├── ingest.py             # Phase 1 GDELT-only runner (legacy)
│   ├── sources/              # registry, polite HTTP client, connectors (gdelt, rss, reliefweb)
│   ├── pipeline/             # cleaning, keyword topics, place tagging, wire-copy detection
│   └── storage/db.py         # SQLite schema v2 + migration; all SQL lives here
├── tests/                    # pytest suite + fixtures (synthetic/ and your recorded/)
├── scripts/schedule_windows.ps1
├── data/  logs/              # runtime files, git-ignored
└── docs/
```

## Principles

1. **Provenance is never lost.** Every record keeps its original URL, source system and timestamps.
2. **Duplicates are not corroboration.** Syndicated copies are grouped and attributed to their original reporter, never counted as independent confirmation.
3. **No fixed reliability score per organisation.** Evidence is weighed per claim.
4. **Publication time ≠ event time.**
5. **AI drafts, the analyst decides.** Every AI statement must cite supplied sources. Unsourced background is labelled as such.
6. **Respect sources:** rate limits, licences, copyright, and no profiling of private individuals.
