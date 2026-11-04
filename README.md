# Global Intelligence Monitor (GIM)

A personal, AI-assisted **geopolitical OSINT platform**. It continuously collects open-source reporting on international developments, organises it with full source provenance, and (from Phase 3) drafts source-cited intelligence briefs for analyst review.

> **Status: Phase 1.** GDELT collection by region → SQLite with deduplication → Streamlit dashboard (filters, story grouping, trends, source landscape, collection log).

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
python -m gim.ingest
```

```powershell
streamlit run app/dashboard.py
```

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**: feasibility, architecture, data-source strategy, AI model evaluation (incl. DeepSeek V4/V4.1), hosting and budget, roadmap, validation, security.
- **[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)**: installation and first run.

## Project layout

```
gim/
├── app/dashboard.py          # Streamlit UI (display only, no business logic)
├── src/gim/
│   ├── config.py             # settings from environment / .env
│   ├── logging_setup.py      # console + JSON-lines file logging
│   ├── models.py             # Article dataclass (shared contract)
│   ├── regions.py            # collection plan: regions and search terms
│   ├── ingest.py             # collection run (CLI: python -m gim.ingest)
│   ├── sources/gdelt.py      # GDELT DOC 2.0 client (rate-limited, retrying)
│   ├── pipeline/normalize.py # cleaning untrusted input, URL canonicalisation, fingerprints
│   ├── pipeline/classify.py  # transparent keyword category baseline
│   └── storage/db.py         # SQLite schema and all SQL
├── tests/                    # pytest suite (no network needed)
├── scripts/schedule_windows.ps1
├── data/  logs/              # runtime files, git-ignored
└── docs/
```

## Principles

1. **Provenance is never lost.** Every record keeps its original URL, source system and timestamps.
2. **Duplicates are not corroboration.** Syndicated copies are grouped, not counted as independent confirmation.
3. **Publication time ≠ event time.**
4. **AI drafts, the analyst decides.** Every AI statement must cite supplied sources. Unsourced background is labelled as such.
5. **Respect sources:** rate limits, licences, copyright, and no profiling of private individuals.
