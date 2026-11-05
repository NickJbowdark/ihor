# 🌐 Global Intelligence Monitor (GIM)

A personal, AI-assisted **geopolitical intelligence platform**. GIM collects open-source reporting and official statements worldwide and groups them into events with full provenance. It analyses those events with a source-grounded AI engine (DeepSeek, Ollama or any OpenAI-compatible model), and produces professional intelligence reports (PDF) for analyst review.

> **Status: Phase 3 (v0.3).** Complete application: dashboard (overview, country/region monitor, event explorer, reports, alerts), AI analysis engine with evidence validation, six report types with PDF export, historical archive search (RAG), transparent alerts, e-mail delivery, Hetzner deployment files.
>
> ⚠️ **Honest status.** Everything is tested automatically against **clearly labelled synthetic data** and a local fake AI server. **Live sources and real AI models are not yet verified.** The development sandbox had no access to them. See [docs/PHASE3_INTELLIGENCE.md §0](docs/PHASE3_INTELLIGENCE.md#0-tested-vs-still-unverified).

## What it does

- **Collect:** GDELT discovery, UN News, Council of the EU, European Commission, ReliefWeb, plus 27 more catalogued sources. Rate-limited, validated, health-monitored.
- **Organise:** documents → events. Wire copies count as one origin. Official statements are kept apart from facts. Publisher country ≠ countries mentioned ≠ event location.
- **Analyse:** per-event analysis covering significance, developments, actors and positions, background, source comparison, implications, uncertainty and indicators.
    - Every claim is typed and cited.
    - Invented citations are rejected deterministically.
    - Earlier events are retrieved from the archive for historical comparison.
- **Report:** daily brief, weekly assessment, country profile, regional conflict assessment, diplomatic/strategic analysis, custom research brief. Markdown, PDF and e-mail; draft → reviewed → final.
- **Alert:** volume spikes against a baseline, watchlists, official statements, multi-origin events. Every alert shows its exact criteria.

## Quick start (Windows)

Full guide: **[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)**. In short, inside the project folder:

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

See the whole application immediately on synthetic demo data:

```powershell
python -m gim.demo
```

```powershell
$env:GIM_DB_PATH="data/demo.sqlite3"; streamlit run app/dashboard.py
```

Then real data:

```powershell
Remove-Item Env:GIM_DB_PATH
```

```powershell
python -m gim.validate --record
```

```powershell
python -m gim.collect --force
```

```powershell
streamlit run app/dashboard.py
```

## Documentation

| Document | Content |
|---|---|
| [WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) | Installation, demo, AI configuration (DeepSeek / Ollama), daily use, automation, troubleshooting |
| [PHASE3_INTELLIGENCE.md](docs/PHASE3_INTELLIGENCE.md) | AI engine, safeguards, reports, dashboard, alerts, how to evaluate a real model, tested vs. unverified |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | 24/7 on a Hetzner VPS with Tailscale (prepared, not executed) |
| [PHASE2_1_RELIABILITY.md](docs/PHASE2_1_RELIABILITY.md) | Validation of real sources, events, geography, corroboration, reliability and security audit |
| [PHASE2_SOURCES.md](docs/PHASE2_SOURCES.md) | Source catalogue, access and licence notes |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Original feasibility study, AI model evaluation, budget, roadmap |

## Architecture

```
config/sources.toml ─► sources/ (connectors) ─► pipeline/ (clean, tag, events, corroboration)
                                                   │
                                     storage/ (SQLite v4: documents, events, FTS5 archive,
                                               alerts, reports, AI cost ledger, analyses)
                                                   │
          analysis/ (contracts · prompts · LLM client · engine · retrieval) ─► reports/ (builder · Markdown · PDF)
                                                   │
                         ui/ (Streamlit pages) · CLI (collect, validate, analyse, report, alerts, maintenance)
```

```
src/gim/
├── analysis/      contracts.py (frozen evidence + claim types + validator) · prompts.py · llm.py (DeepSeek/Ollama/
│                  OpenAI-compatible, budget cap) · engine.py (analyst, repair, cache) · retrieval.py (RAG) · bundle.py
├── reports/       specs.py (6 report types) · builder.py · render.py (Markdown) · pdf.py · fonts/ (DejaVu)
├── ui/            theme.py · components.py · data.py · auth.py · nav.py · pages/ (overview, monitor, events,
│                  reports, alerts, documents, sources, settings)
├── sources/       registry · polite HTTP · gdelt · rss · reliefweb
├── pipeline/      normalize · classify · geo (gazetteer + ISO3) · actors · attribution · events · corroboration · prominence
├── storage/       db.py (core + migrations) · intel.py (search, stats, alerts, reports, ledger)
├── collect.py · validate.py · alerts.py · analyse.py · report.py · maintenance.py · emailer.py · security.py · demo.py
app/dashboard.py   application shell (theme, login, navigation)
deploy/hetzner/    systemd units, Caddyfile, setup script
```

## Principles

1. **Provenance is never lost.** Every statement links to DOC ids with original URLs.
2. **Copies are not confirmations.** Wire stories count as one origin; there are no per-organisation reliability scores.
3. **Positions are not facts.** Official statements document what an institution says.
4. **Unknown stays unknown.** No guessed event locations or coordinates; publisher location is never event location.
5. **AI drafts, the analyst decides.** Claims are typed and validated; background knowledge is marked unverified; reports start as drafts.
6. **Synthetic data is always labelled** and kept in a separate database.
7. **Respect sources and budgets:** rate limits, licences, robots, and a hard monthly AI spending cap.
