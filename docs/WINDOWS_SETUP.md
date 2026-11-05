# Windows installation & usage guide

Every command runs in **PowerShell** (Start menu → type "PowerShell"). Run them one at a time.
Tested assumptions: Windows 10/11, Python 3.12. Your PC (Ryzen 7 7800X3D, RTX 4070, 32 GB) is more than enough.

---

## 1. Install the tools (once)

```powershell
winget install --id Python.Python.3.12 -e
```

```powershell
winget install --id Git.Git -e
```

**Close and reopen PowerShell**, then check:

```powershell
py -3.12 --version
```

> No `winget`? Install Python from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**) and Git from https://git-scm.com/download/win.

## 2. Get the code

```powershell
cd $HOME\Documents
```

```powershell
git clone https://github.com/nickjbowdark/ihor.git gim
```

```powershell
cd gim
```

```powershell
git checkout claude/lucid-thompson-ssnima
```

Already cloned earlier? Then update instead:

```powershell
git pull
```

## 3. Virtual environment and dependencies

```powershell
py -3.12 -m venv .venv
```

Allow PowerShell to run the activation script (once per Windows user):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Activate it. **Do this in every new PowerShell window**; the prompt then starts with `(.venv)`:

```powershell
.\.venv\Scripts\Activate.ps1
```

```powershell
python -m pip install --upgrade pip
```

```powershell
pip install -e ".[dev]"
```

## 4. Configuration

```powershell
Copy-Item .env.example .env
```

```powershell
notepad .env
```

At minimum, set `GIM_USER_AGENT` (your contact URL). The AI section is covered in step 8.

## 5. Check the installation

```powershell
python -m pytest
```

Expected: all tests pass (`111 passed, 1 skipped`; the skipped test waits for real recorded responses).

## 6. See the complete application immediately (demo, synthetic data)

```powershell
python -m gim.demo
```

```powershell
$env:GIM_DB_PATH="data/demo.sqlite3"; streamlit run app/dashboard.py
```

Your browser opens at http://localhost:8501. Every page shows a red **SYNTHETIC DATA** banner, and every headline starts with `[SYNTHETIC]` or `[TEST FIXTURE]`. This lets you explore everything: maps, the country monitor, the event explorer, report generation with PDF export, and alerts. It is not real intelligence.

Stop with `Ctrl+C`. Return to your real database in the same window:

```powershell
Remove-Item Env:GIM_DB_PATH
```

## 7. Real data: validate and collect sources

```powershell
python -m gim.maintenance backup
```

```powershell
python -m gim.validate --record
```

This checks every enabled source against the real internet and writes a report to `reports\validation\`. Details and outcome meanings are in [PHASE2_1_RELIABILITY.md §4](PHASE2_1_RELIABILITY.md#4-windows-testing-real-sources-step-by-step).

```powershell
python -m gim.collect --force
```

```powershell
streamlit run app/dashboard.py
```

## 8. Enable AI analysis (choose one)

### Option A: DeepSeek API (cheap, best quality on your budget)
1. Create an account and API key at https://platform.deepseek.com.
2. Copy the **exact model id** and the **prices** from https://api-docs.deepseek.com (GIM never guesses them).
3. In `.env`:

```
GIM_LLM_PROVIDER=deepseek
GIM_LLM_MODEL=<model id from the docs>
GIM_LLM_API_KEY=<your key>
GIM_LLM_PRICE_INPUT_PER_M=<USD per 1M input tokens>
GIM_LLM_PRICE_OUTPUT_PER_M=<USD per 1M output tokens>
GIM_LLM_MONTHLY_BUDGET_USD=5
```

### Option B: Ollama (local, free, runs on your RTX 4070)
1. Install from https://ollama.com and pull a model that fits 12 GB VRAM (roughly 7–14 B parameters, 4-bit), for example a current Qwen, Gemma, Mistral or Llama model:

```powershell
ollama pull <model-name>
```

```powershell
ollama list
```

2. In `.env`:

```
GIM_LLM_PROVIDER=ollama
GIM_LLM_MODEL=<name exactly as shown by ollama list>
```

Local models of this size follow the strict JSON and citation rules less reliably. GIM's validator rejects their bad citations, so expect more rejected statements than with DeepSeek.

### Test it
Restart the dashboard → **Settings & System** → *Test AI connection*. Or from the command line:

```powershell
python -m gim.analyse --top 3 --days 2
```

## 9. Daily use

| Task | Where |
|---|---|
| World situation, top developments, regional pulse | **Global Overview** |
| One country or region over time, generate its report | **Country & Region Monitor** |
| Search the archive, inspect sources, run AI analysis, set event location | **Event Explorer** |
| Daily brief / weekly assessment / country profile / conflict / diplomatic / custom brief, PDF | **Intelligence Reports** |
| Watchlists and transparent alerts | **Alerts & Watchlists** |
| Source status, verification, errors | **Source Health** |
| AI budget, database, backups | **Settings & System** |

Command-line equivalents:

```powershell
python -m gim.report --type daily_brief --pdf reports\daily.pdf
```

```powershell
python -m gim.report --type country_profile --country Sudan --days 30 --pdf reports\sudan.pdf
```

```powershell
python -m gim.report --type custom_topic --topic "Black Sea shipping" --days 60
```

```powershell
python -m gim.alerts
```

## 10. Automation (Windows Task Scheduler)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
```

This registers collection every 30 min and a daily backup. To add a daily brief PDF at 07:43:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -DailyBrief
```

Tasks run only while the PC is on. For 24/7 operation see [DEPLOYMENT.md](DEPLOYMENT.md).

## 11. Updating later

```powershell
git pull
```

```powershell
pip install -e ".[dev]"
```

```powershell
python -m pytest
```

Database schema upgrades run automatically and write a backup first (`data\backups\*-pre-v*`).

## Where things are

| What | Where |
|---|---|
| Database | `data\gim.sqlite3` · demo: `data\demo.sqlite3` |
| Backups | `data\backups\` (`python -m gim.maintenance list`) |
| Logs | `logs\gim.jsonl` (credentials redacted) |
| Validation reports | `reports\validation\` |
| Source registry | `config\sources.toml` |
| Regions / search terms | `src\gim\regions.py` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Activate.ps1 cannot be loaded` | Run the `Set-ExecutionPolicy` command in step 3 |
| `ModuleNotFoundError: No module named 'gim'` | Venv not active, or step 3 skipped |
| Map shows no countries | The browser loads map outlines from `cdn.plot.ly`; check your internet connection or ad-blocker |
| "AI not configured" | See step 8; Settings & System lists exactly what is missing |
| `monthly AI budget … would be exceeded` | Working as intended. Raise `GIM_LLM_MONTHLY_BUDGET_USD` or wait for next month |
| `Skipped: another collection is already running` | Lock held by the scheduler or the dashboard; it expires after 30 min if that run crashed |
| `GDELT rejected the query` | A term in `regions.py` is invalid (e.g. too short) |
| Red SYNTHETIC banner although you expect real data | `GIM_DB_PATH` still points to the demo DB: `Remove-Item Env:GIM_DB_PATH` |
