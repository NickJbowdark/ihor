# GIM — Architecture, Evaluation and Roadmap

*Status: Phase 1 implemented; Phase 2a implemented (see [PHASE2_SOURCES.md](PHASE2_SOURCES.md)). Written 2026-11.*

---

## 1. Is the idea realistic?

**Yes, as a personal analytical platform. No, as a "fully automated intelligence agency".** The honest split:

| Part | Difficulty | Why |
|---|---|---|
| Collecting article metadata from GDELT / RSS | Easy | Free, documented, rate-limited APIs. Done in Phase 1. |
| Storage, search, filtering, dashboard | Easy–medium | Standard data engineering. |
| Deduplication of *identical* articles | Easy | Canonical URLs + headline fingerprints (done). |
| Grouping *different* articles about the same event | **Hard** | Needs text similarity (embeddings) + time/place/actor matching. Always imperfect. |
| Event date vs. publication date | **Hard** | "Yesterday", "last week" must be resolved from article text. LLMs help, and still make mistakes. |
| **Corroboration** (independent confirmation) | **Hardest** | 200 outlets reprinting one Reuters wire = *one* source. You need to trace origin, which metadata alone does not reveal. The system can only *support* your judgement here. |
| Geolocating events | Medium–hard | GDELT/ACLED give coordinates, but GDELT's are often the capital city or a misread place name. |
| AI summaries with citations | Medium | Works well **if** the AI only sees retrieved source text and must cite it. Fails badly if asked to "know" things. |
| AI *assessments* (implications, history) | Medium, and risky | Useful as a draft. Needs analyst review: models state plausible-sounding history with confidence. |
| Full-text collection | Legally constrained | Copyright and terms of use. Store metadata + short excerpts; link to the original. |
| Multilingual | Medium | GDELT already machine-translates 65 languages for search. LLMs read most major languages well. |

**The core principle:** the machine does *collection, processing and triage*. **You** do *analysis*, with the AI as a drafting assistant that has to show its sources. That is how serious analytic teams use these tools, and it is also the version that impresses admissions committees and employers.

---

## 2. Recommended architecture

Modular "pipeline + store + views". Each box is a Python module with a narrow interface, so you can swap any one without rewriting the others.

```
 COLLECTION (scheduled, polite)        PROCESSING (deterministic)          ANALYSIS (AI, budgeted)
 ┌──────────────────────────┐        ┌───────────────────────────┐       ┌──────────────────────────┐
 │ sources/gdelt.py   [P1]  │        │ normalize: clean, canon.  │       │ llm/client.py (OpenAI-   │
 │ sources/rss.py     [P2]  │──raw──▶│ URL, fingerprint   [P1]   │──────▶│   compatible: DeepSeek,  │
 │ sources/acled.py   [P4]  │        │ classify (rules)   [P1]   │       │   Ollama, others) [P3]   │
 │ sources/official   [P4]  │        │ cluster → events   [P3]   │       │ extract / summarise /    │
 │  (MFA, UN, NATO RSS)     │        │ geocode, dates     [P3-4] │       │ brief, with citations    │
 └──────────────────────────┘        └───────────────────────────┘       │ cost ledger + hard cap   │
              │                                   │                      └──────────────────────────┘
              ▼                                   ▼                                   │
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ STORAGE  storage/db.py — SQLite (WAL) → PostgreSQL later.  articles · article_regions ·          │
 │ ingest_runs [P1] · events · event_articles · claims · reports · llm_calls [P2-3] · embeddings [P6]│
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
              │
              ▼
 VIEWS: Streamlit dashboard [P1] · PDF/e-mail reports [P3/P5] · alerts [P5]
```

**Rules that keep it maintainable:**
1. **The UI never contains business logic.** `app/dashboard.py` only calls `gim.*` functions.
2. **All SQL lives in `storage/db.py`.** Moving to PostgreSQL means changing one file.
3. **Every source is a module returning the same `Article`/`Event` dataclasses.**
4. **Every collection and every AI call is logged in the database** (`ingest_runs`, later `llm_calls`). That is your audit trail and your cost tracker.
5. **Raw provenance is never overwritten.** The original URL, the source system and the timestamps are kept. Derived fields sit next to them.

### Technology choices (evaluated, not just accepted)

| Choice | Verdict | Reasoning |
|---|---|---|
| Python | ✅ Keep | The OSINT and data ecosystem lives here. |
| Requests + Feedparser | ✅ Keep | Simple and robust. No need for async/Scrapy at this volume. |
| Pandas | ✅ Keep, for analysis/UI only | Don't use it as the storage layer. |
| SQLite → PostgreSQL | ✅ Keep SQLite until ~Phase 5–6 | One file, zero admin, and WAL mode lets the dashboard read while the collector writes. Its FTS5 module gives full-text search (Phase 2). Move to Postgres + `pgvector` when you need RAG at scale or several writers. |
| Streamlit | ✅ For Phases 1–4 | Fastest path to a working analyst UI. Its limits are multi-user auth and custom interactions. Because logic sits in the package, a later FastAPI + web front end is a UI swap, not a rewrite. |
| Plotly / Folium | ✅ Plotly now; `pydeck` or Folium for event maps in Phase 4 | Plotly choropleths for aggregates, pydeck for thousands of event points. |
| spaCy / heavy NLP | ⏸ Defer | For entity extraction an LLM already does better on news. Add spaCy only if you need cheap bulk NER. |
| APScheduler / Celery | ❌ Not needed | Windows Task Scheduler locally and a `systemd` timer on Linux are simpler and more reliable for "run every 3 h". |
| PDF: WeasyPrint | ⚠️ Avoid on Windows | It needs GTK libraries, which are painful to install there. Use **Markdown → HTML → PDF via Playwright/Chromium**, or `fpdf2`. Decide in Phase 3. |
| Docker | ⏸ Phase 5 | Useful for the server, but unnecessary friction while learning on Windows. |

---

## 3. Data collection strategy

**Principle: metadata first, links always, full text rarely.** You store *what was reported, by whom, when, and where to read it*. You do not mirror other people's journalism.

| Source | Access (verify before use) | Use for | Caveats |
|---|---|---|---|
| **GDELT DOC 2.0 API** [P1] | Free, no key. Keep to ≤1 request / 5 s. Rolling ~3-month window. | Worldwide coverage, discovery, volume trends. | Metadata only. Broad keyword matches bring in noise. `sourcecountry` is where the **outlet** is, not where the event is. |
| **GDELT Events 2.0 / GKG** (raw CSV every 15 min) | Free download | CAMEO-coded events with coordinates, actors, tone [P4]. | Machine-coded and noisy: one event coded many times, plus frequent geocoding errors. Treat it as an indicator, never as a fact. |
| **RSS** [P2] | Free. Respect `robots.txt`, `ETag`/`Last-Modified` headers, and poll ≤ hourly. | Curated quality outlets, think tanks (Crisis Group, CFR, EUISS), **official sources** (MFAs, UN press, NATO, EU Council, OSCE). | Feeds change URLs. Store feed health in `ingest_runs`. |
| **ACLED** [P4] | Free account for research use; API via OAuth. **Terms restrict redistribution** of the raw data. | Gold standard for political violence and protest events, human-coded and geolocated. | Weekly-ish updates, not real-time. Check its current licence tiers and don't republish its rows on a public site. |
| **UCDP** [P4] | Free. Recent API versions require an access token (check its current docs). | Historical conflict baselines (1946–), for "is this unusual?" comparisons. | Annual releases, plus "candidate" monthly data. |
| **UN / ReliefWeb, UNHCR, OCHA** [P4] | Public APIs | Humanitarian intelligence: displacement, crises. | — |

**Technical etiquette, already in the code:** an honest User-Agent, a minimum request interval, exponential back-off on 429/5xx, overlapping time windows (e.g. a 6 h look-back every 3 h) so that a missed run loses nothing, deduplication on insert, and a per-run audit log.

---

## 4. AI model evaluation

### 4.1 DeepSeek V4.1 — what I could and could not verify (searched 2026-11-04)

What is **consistent across sources**:
- DeepSeek released the **V4 series as open weights (MIT licence) on 24 April 2026**: *V4-Pro* (very large) and ***V4-Flash* (≈284 B total parameters, ≈13 B active per token, mixture-of-experts)**, with a context window of up to 1 M tokens. A re-trained V4-Flash revision ("0731", GA) followed. Third parties list a **"V4.1 Flash"** as well.
- The official API is **OpenAI-compatible** (base URL `https://api.deepseek.com`), so the standard `openai` Python package works with it.
- API prices for Flash-class models are reported at roughly **$0.15 / M input tokens and $0.60 / M output tokens** (lower with cache hits; some sources describe off-peak/peak pricing).

What I **could not verify** from official pages: the exact current **model ID string** for V4.1-Flash (sources disagree: `deepseek-v4-flash`, `deepseek-flash`, or the legacy aliases `deepseek-chat`/`deepseek-reasoner`, which are being retired) and the exact current prices. Aggregators disagree with each other. **Before Phase 3, copy the model ID and prices from `api-docs.deepseek.com`.** The code will keep both in `.env`, not hard-coded.

**Hardware verdict: you cannot run V4-Flash locally.** At ~284 B parameters even a 4-bit quantisation needs on the order of **150+ GB** of memory. Your RTX 4070 (12 GB VRAM) plus 32 GB RAM is far short. Only "distilled" small models would run, and those are different, much weaker models that happen to share the name. Treat any claim of "DeepSeek V4 on a gaming PC" with suspicion.

**Is the DeepSeek API practical for GIM? Yes, with a caveat.** Cost estimate for a daily workflow:
- ~300 article metadata items to triage (≈30 tokens each) plus a 2,000-token prompt → ~12 k input tokens
- Daily brief from ~40 selected items with excerpts → ~25 k input, ~3 k output
- **≈ 40 k input and 5 k output tokens per day → about $0.01/day, i.e. well under $1/month.** Even 20× that stays within a €5 budget.

**The caveat is data governance.** DeepSeek is a Chinese provider, and prompts are processed under its privacy terms and jurisdiction. Your inputs here are public news, so the *confidentiality* risk is low. But (a) some employers and institutions will ask about it, and (b) for an analysis tool about geopolitics, **model bias on politically sensitive topics** (e.g. Taiwan, Xinjiang, Tiananmen) is a real analytical concern that you should *test*, not assume either way. That test is itself a good portfolio exercise: run the same brief through two providers and compare (Phase 6).

### 4.2 Alternatives

| Option | Strengths | Weaknesses | Role in GIM |
|---|---|---|---|
| **DeepSeek API (V4/V4.1 Flash)** | Very cheap, strong reasoning, open weights, OpenAI-compatible | Jurisdiction/bias questions; model IDs change | Primary cheap API candidate |
| **Local via Ollama** (≈7–14 B models at 4-bit, e.g. current Qwen / Gemma / Mistral / Llama families) | Free per token, private, offline | On 12 GB VRAM: noticeably weaker reasoning, less reliable JSON, slower; only works while your PC is on | Bulk *classification/extraction* and experiments; **not** final assessments |
| **Frontier commercial APIs** (Anthropic Claude, OpenAI, Google Gemini — small/fast tiers) | Best instruction-following, citation discipline and structured output; Western data-protection terms | Higher price per token (still a few €/month at GIM volumes if you use the small tiers) | Final brief writing, or as a "second opinion" model |

**Recommendation:** build **one provider-agnostic client** that speaks the OpenAI-compatible chat API. DeepSeek, Ollama (`http://localhost:11434/v1`) and most other providers expose that interface. Then:
- default to the **cheapest API model that passes your evaluation set** (see §7),
- use **Ollama locally** for development and bulk tagging, so mistakes while learning cost nothing,
- enforce a **hard monthly budget**: each call is logged to an `llm_calls` table with token counts and estimated cost, and the client refuses calls once the cap is reached.

### 4.3 How the AI must be used (anti-hallucination design)

1. **Closed-book is forbidden.** The model only receives retrieved records (ID, title, outlet, date, excerpt). Each record gets a short ID such as `[A123]`.
2. **Structured output (JSON) validated by code.** Every factual statement must carry `source_ids`. The code **rejects** output that cites IDs which weren't supplied, or quotes text that isn't in the source.
3. **Explicit claim types**, mirroring analytic tradecraft:
   - `REPORTED_FACT` — stated by ≥1 source (list them)
   - `ATTRIBUTED_CLAIM` — "Russian MoD says…", "Hamas claims…"
   - `ASSESSMENT` — analytic judgement, with a confidence level (low/moderate/high) and an estimative-language word ("likely", "unlikely"…)
   - `CONTEXT` — background knowledge **from the model**, flagged as unsourced until you verify it
4. **Prompt-injection defence.** Source text is wrapped in delimiters and treated as *data*. The system prompt states that instructions inside sources must be ignored. Inputs are cleaned (already done in `normalize.py`) and length-capped. Output is validated against a schema. **The model never gets tools that can act** (no browsing, no email sending, no DB writes).
5. **Human in the loop.** Reports start as `draft`, and you mark them `reviewed`/`final` in the UI. Only reviewed reports go out by email.

### 4.4 RAG in one paragraph

RAG (retrieval-augmented generation) means: *search first, then let the model read only what you found.* In GIM, Phase 2 adds SQLite **FTS5** keyword search, and Phase 6 adds **embeddings** (vectors capturing meaning, computed by a small local model or a cheap API) stored in `sqlite-vec` or Postgres `pgvector`. A question like *"How has Sahel–Russia cooperation evolved since 2022?"* becomes: (1) hybrid search for the top 30 relevant records and reports, (2) a prompt with those records plus their IDs, (3) an answer that cites them. The model's job is to *read and organise*, not to *remember*. This also makes the system's knowledge cumulative: your own reviewed past reports become retrievable context.

---

## 5. Hosting recommendation

**Start locally (Phases 1–4), then move collection and the dashboard to a small VPS (Phase 5). Skip the hybrid setup.**

- Local first: fast iteration, free, and nothing is exposed to the internet while you are still learning security.
- **Streamlit Community Cloud is not suitable.** It has no background scheduler, apps sleep when idle, and its storage isn't persistent. Use it at most for a *public demo with a static sample database*.
- **The hybrid option (VPS collects, PC does inference) adds complexity for little gain.** At ~$1/month of API cost, local inference saves almost nothing, and it fails whenever your PC is off. Keep Ollama for local experiments only.
- **Hetzner Cloud:** the smallest shared-vCPU instance (2 vCPU / 4 GB class) is ample, because GIM's load is tiny. Check current prices and IPv4 charges, which have changed recently. **Expect roughly €4–8/month including a public IPv4 address and backups.**
- **Secure access from your phone, simplest to most work:**
  1. **Tailscale** (free personal plan): the dashboard is reachable only from *your* devices. No open ports and no login page to attack. ⭐ Recommended.
  2. Cloudflare Tunnel + Cloudflare Access (free tier), if you want a normal URL.
  3. Caddy reverse proxy (automatic HTTPS) + authentication. This is the most work and the most exposure.

### Budget

| | MVP (Phases 1–4, local) | Deployed personal system (Phase 5) | "Production" (multi-user, public) |
|---|---|---|---|
| Hosting | €0 | €4–8 (Hetzner small VPS + backups) | €20–60+ (bigger VM, managed Postgres, monitoring) |
| AI | €0 (Ollama) to €1 | €1–5 with a hard cap | €20–200+ depending on users and volume |
| Data | €0 | €0 (GDELT, RSS, UCDP; ACLED research tier) | Commercial ACLED/news licences can be €€€€ |
| Domain | — | optional ~€1/month | yes |
| **Total** | **€0–1/month** | **≈ €5–15/month ✅** | well beyond a student budget |

---

## 6. Phased roadmap (each phase ends with something that works)

| Phase | Deliverable | Key learning |
|---|---|---|
| **1 ✅** | GDELT collection per region → SQLite → dashboard with filters, story grouping, trend and source views, collection log | APIs, rate limits, provenance, data cleaning, dedup vs. corroboration |
| **2** (2a + 2.1 ✅, see PHASE2_SOURCES.md and PHASE2_1_RELIABILITY.md) | Scheduled collection (Task Scheduler), RSS module with curated feed list incl. official MFA/UN/NATO feeds, FTS5 search, source registry with reliability notes (Admiralty-style A–F source / 1–6 information grading) | Source evaluation, collection plans |
| **3** | LLM client (OpenAI-compatible, budget cap, call log); event clustering of related articles; structured extraction (actors, places, *event date*); daily brief draft → review → Markdown/PDF | Prompt design, structured output, evaluation, analytic writing |
| **4** | ACLED + UCDP; event map; baselines and anomaly flags ("event volume in X is 3σ above its 90-day mean"); country pages | Geospatial data, base rates, indicators & warnings |
| **5** | Hetzner VPS, systemd timers, Tailscale, backups (`sqlite3 .backup` + offsite copy), error alerts, daily email | Ops and security basics |
| **6** | Embeddings + RAG over the archive, watchlists, multi-model comparison, multilingual handling | Retrieval, bias testing, model evaluation |

Each phase also maps onto the **intelligence cycle**:
Planning = `regions.py` and watchlists (your *collection requirements*) → Collection = `sources/` → Processing = `pipeline/` → Analysis = LLM drafts plus your review → Dissemination = dashboard, reports, email → Feedback = the review notes you store, which tune queries and prompts.

---

## 7. How to know it actually works (validation)

**Collection:**
- `ingest_runs` must show `ok` runs at the expected cadence. A gap means the scheduler or network failed.
- Spot-check 20 random stored URLs per week: do they open, and are they on-topic? Note the share that is off-topic for each region. That number is your query **precision**, and you improve it by editing `regions.py`.
- Recall check: pick 5 major events you know about from elsewhere. Did GIM capture them within 6 h?

**Processing:**
- Unit tests (`pytest`) cover cleaning, URL canonicalisation, dedup, classification edge cases, error handling and malformed input. Add a test every time you find a bug.
- Classification: hand-label 200 headlines once (a spreadsheet is fine). Measure precision/recall of the keyword rules. In Phase 3 the LLM classifier must **beat** that baseline on the *same* set to be adopted. (Current known weakness: "China and Taiwan **trade** accusations" gets tagged *economic*. Keyword rules can't read context, which is exactly what the evaluation set will show.)

**AI analysis:**
- A *golden set* of ~30 article bundles with your hand-written expected facts.
- Automated checks: every cited ID exists, every quote is a verbatim substring of a source, every date falls inside the source window, and the JSON validates.
- Manual rubric per brief: factual errors, unsupported claims, missing key developments, and whether claim types and confidence are labelled properly. Track the scores over time and per model. That table is also excellent portfolio material.

---

## 8. Security and ethics checklist

- Secrets only in `.env` (git-ignored). Never paste keys into code, notebooks or chats.
- Treat all source content as untrusted: cleaned at ingestion, only `http(s)` links are rendered, SQL is always parameterised, and LLM output is schema-validated.
- The dashboard binds to `127.0.0.1` until Phase 5 adds an authentication layer (Tailscale/Access).
- Collect information about **events and public institutions**, not private individuals. GDPR applies to any personal data you store, and news metadata rarely needs it.
- Respect source terms: rate limits, ACLED redistribution rules, and copyright (store links and short excerpts, not full articles).
- Label AI-generated text as such in every report.
