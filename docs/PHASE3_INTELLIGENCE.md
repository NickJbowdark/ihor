# Phase 3 — The intelligence layer

*Written 2026-11-05. Schema v4. All automated tests pass.*

This phase turns GIM from a collection system into an analytical platform: an **AI analysis engine** that works with any provider, **six report types** with PDF export, a redesigned **dashboard** (overview, country/region monitor, event explorer, reports, alerts), **historical retrieval (RAG)**, **transparent alerts**, e-mail delivery, and **deployment files**.

## 0. Tested vs. still unverified

| | Status |
|---|---|
| ✅ Tested automatically (offline) | AI client against a **local fake OpenAI-compatible server**: JSON parsing, validation, the repair round, rejection of invented citations and fake quotes, invalid-JSON repair, the budget cap blocking *before* the call, API-key redaction, caching. Report generation (AI and baseline), Markdown + PDF rendering, prompt-injection neutralisation, FTS search and query-injection safety, no future leakage in history retrieval, alert rules and de-duplication, watchlists, e-mail sending (mocked SMTP), every dashboard page rendered headless (Streamlit AppTest) |
| ✅ Tested in a real browser (sandbox) | Full journey on the synthetic demo: overview → event → analysis → report generation → library → PDF button; AI-analysis rendering against the fake server |
| ⚠️ **Not verified: needs your API key / PC** | Real DeepSeek or Ollama output quality, exact model IDs and prices, how often real models violate the citation rules, cost per report in practice. Maps: the browser loads country outlines from `cdn.plot.ly`, which the sandbox blocks, so map rendering was not visible there |
| ⚠️ Not verified (unchanged from Phase 2.1) | Live source responses (run `python -m gim.validate`) |

Synthetic data is always labelled: titles start with `[SYNTHETIC]` / `[TEST FIXTURE]`, documents carry `data_origin='fixture'`, every page shows a red banner, and reports print *SYNTHETIC TEST DATA — NOT REAL INTELLIGENCE* on every page.

---

## 1. Analysis pipeline

```
Event (EVT-n) ──► EventBundle: documents (DOC-n), verbatim excerpts, corroboration, geography (unknown stays unknown)
              ──► Archive retrieval (FTS5/BM25): earlier events sharing a country/organisation, strictly before the event
              ──► Analyst.analyse_event()      LLMAnalyst (DeepSeek / Ollama / any OpenAI-compatible) or BaselineAnalyst
              ──► Validator (deterministic, per claim): cited ids exist? quote verbatim? type rules?
              ──► one repair round with the exact violations ──► still invalid → REJECTED (kept, shown, never used)
              ──► cache (key = evidence hashes + provider + model + prompt version)
Report ──► select events (scope, period, prominence) ──► analyse each (cached) ──► synthesis call (validated the same way)
       ──► deterministic statistics + bibliography ──► stored as DRAFT ──► Markdown / PDF / e-mail
```

**Claim types:** `REPORTED_FACT` (cites sources) · `ATTRIBUTED_CLAIM` ("X says…", needs a speaker) · `ASSESSMENT` (judgement, needs low/moderate/high confidence) · `CONTEXT` (model background knowledge, must not cite, shown as *unverified*).

**Event-analysis sections:** why it matters · key developments · actors & documented positions · background · source comparison · implications · uncertainty · indicators to watch.

**Safeguards against hallucination and manipulation**
1. Only supplied IDs can be cited; invented IDs are rejected deterministically.
2. Quotes must appear character-for-character in the cited text.
3. Source text is wrapped in `<source>` blocks; our delimiters inside untrusted text are neutralised; the prompt declares the content to be data, not instructions.
4. Official statements are positions, not facts; wire copies are one source (from Phase 2.1 corroboration).
5. Model background knowledge is visibly labelled *unverified*.
6. All output is a **draft** until a human marks it reviewed/final.
7. A monthly budget cap is checked before every call; every call is logged with tokens and cost.

**Changing provider** only touches `.env`. Ingestion never imports `gim.analysis`. The prompts carry a version (`gim-prompts/1.0`), so changing them invalidates the cache instead of silently mixing old and new analyses.

## 2. Reports

| Type | Default period | Scope |
|---|---|---|
| Daily Global Intelligence Brief | 1 day | optional region/country |
| Weekly Geopolitical Assessment | 7 days | optional |
| Country Intelligence Profile | 30 days | country required |
| Regional Conflict Assessment | 14 days | region required; conflict/humanitarian events |
| Diplomatic and Strategic Analysis | 14 days | diplomatic / international organisations / economic events |
| Custom Research Brief | 30 days | topic keywords required (full-text search) |

Every report contains:
- title, scope and period, provider/model/prompt version, status
- executive summary, regional overview, trends
- per-event assessments, implications, indicators, uncertainties, open questions
- deterministic statistics
- a standard **confidence and limitations** section
- a numbered **bibliography** with URLs
- an appendix of rejected statements

The PDF is generated in pure Python (fpdf2) with bundled DejaVu fonts, so it works on Windows without extra programs and renders non-Latin names.

## 3. Dashboard

| Page | What it answers |
|---|---|
| **Global Overview** | What is happening now? KPIs vs. the previous period, world map (attention by country, clearly labelled; established event locations as a separate layer), regional pulse, topic mix, top developments by transparent prominence, activity, latest alerts |
| **Country & Region Monitor** | How is a country/region developing? Trend vs. previous-period average, an event bubble timeline, topics, co-mentioned countries and organisations, official statements, watch button, one-click report |
| **Event Explorer** | Full-text archive search with filters. Event detail: location (analyst can set it, with a basis), AI analysis with clickable citations, sources, corroboration and origins, history (earlier related events), document timeline |
| **Intelligence Reports** | Generate (with progress and budget status), library, review workflow (draft → reviewed → final + note), PDF/Markdown download |
| **Alerts & Watchlists** | Alerts with their exact criteria; watch countries, regions, organisations, keywords, sources |
| Document Feed / Source Health / Source Catalogue / Settings | provenance-level data, operations, AI budget, database, backups |

**Geography rule, enforced everywhere:** maps show either *mentions* (labelled as attention) or *established event locations* (dataset/analyst). Publisher location is never used, and no coordinates are invented.

## 4. Alerts (transparent rules)

- **volume_spike:** documents mentioning a country in 24 h ≥ max(8, mean + 3σ of the previous 28 days) and ≥ 2× mean; requires ≥ 7 days of history.
- **multi_origin_event:** an event reaches ≥ 3 independent origins (not a verification).
- **official_statement:** a new official statement on diplomatic, conflict or economic topics.
- **watchlist_match:** an event matches a watch.

Every alert stores its numbers in `criteria`. Alerts are evaluated after every collection and de-duplicated.

## 5. Evaluating a real model (do this before trusting it)

1. Configure DeepSeek or Ollama (see [WINDOWS_SETUP.md §8](WINDOWS_SETUP.md#8-enable-ai-analysis-choose-one)).
2. Analyse 10 real events:

```powershell
python -m gim.analyse --top 10 --days 3
```

3. For each event, open the cited sources and record:
   - wrong facts
   - unsupported claims
   - missed key points
   - mislabelled claim types or confidence levels
   - how many statements were rejected
4. Compare against the baseline (`--no-ai`) and, if you can, a second provider. That table is the evidence for whether the model is good enough. It is also strong portfolio material.

## 6. Increment 3.1 (accuracy, resilience, presentation)

| Area | Change | Why |
|---|---|---|
| Event detection | `deterministic-title-v3`: **IDF-weighted** headline similarity | Generic words ("talks", "minister") no longer merge different stories; shared rare names (places, people) count more |
| Event detection | Pre-filter + statistics refreshed once per event | A full GDELT run (~1,250 documents) no longer does quadratic work or recomputes an event for every document |
| AI evidence | Prompt evidence = **one document per origin first** (officials and original reporting preferred), plus an explicit origin map (which DOCs are copies of which) | Large events made of wire copies no longer crowd out the independent reports; the model sees who actually produced the information |
| Historical context | Related earlier events ranked by shared countries/organisations, then text relevance, corroboration and recency | More relevant archive context |
| Resilience | A failed repair round keeps the valid first answer; an AI failure on one event, a hit budget cap, or a failed synthesis falls back to the baseline **and is stated in the report's limitations**; empty AI answers are not cached | One provider error no longer aborts a whole report |
| Reports | **Key judgments** (assessments, highest confidence first), **Events covered** table, **Changes compared with the previous period** (same scope, deterministic), **Source base** (types, institutions, publishers, languages); change data is also given to the model for the trends section | Professional brief structure; regional pattern changes are grounded in counts |
| Dashboard | "Generate report for this scope" opens the generator pre-filled; new reports display immediately; PDFs cached; overview activity aggregated in SQL; region filter uses event statistics | Usability and speed on large databases |

Prompt version is now `gim-prompts/1.1`, so earlier cached analyses are recomputed on demand rather than mixed with new ones.

## 7. Known limitations

- Retrieval is lexical (FTS5); semantic embeddings are a Phase 6 upgrade.
- Event grouping compares headlines only (`deterministic-title-v2`, with anti-snowball guards: ±48 h member window, 7-day max span).
- Topic tags are keyword rules (the baseline for a future AI classifier).
- Structured conflict data (ACLED/UCDP) is still planned, so no event has an *automatically* established location yet. The event-location layer fills only through analyst input until those connectors exist.
- Event *dates* (as opposed to publication times) are not yet extracted from article text; `event_date` stays empty unless a dataset provides it. This is planned as a validated AI extraction step.
- Streamlit is a single-user app; a multi-user product would need a different front end (the `gim` package is ready for that).
