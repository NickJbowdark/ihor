# Windows setup guide (Phase 1)

Every command below runs in **PowerShell** (Start menu → type "PowerShell"). Run them one at a time.
Lines starting with `#` are explanations; don't type them.

## 1. Install the tools (once)

```powershell
winget install --id Python.Python.3.12 -e
```

```powershell
winget install --id Git.Git -e
```

**Close and reopen PowerShell** so it picks up the new programs, then check them:

```powershell
py -3.12 --version
```

```powershell
git --version
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

## 3. Create a virtual environment

A *virtual environment* is a private Python installation for this project, so its packages don't conflict with SpiderFoot, Maigret or anything else on your PC.

```powershell
py -3.12 -m venv .venv
```

Allow PowerShell to run the activation script (only needed once per Windows user):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Activate it. **You must do this every time you open a new PowerShell window for GIM.** Your prompt then starts with `(.venv)`:

```powershell
.\.venv\Scripts\Activate.ps1
```

## 4. Install GIM and its dependencies

```powershell
python -m pip install --upgrade pip
```

```powershell
pip install -e ".[dev]"
```

`-e` ("editable") means Python uses the code in `src\gim` directly, so your edits take effect without reinstalling.
`[dev]` also installs `pytest` for the tests.

## 5. Configure

```powershell
Copy-Item .env.example .env
```

```powershell
notepad .env
```

Change `GIM_USER_AGENT` so its contact URL points to your GitHub repo. That tells data providers who is calling them.

## 6. Verify the installation

```powershell
python -m pytest
```

Expected result: `17 passed`. If anything fails, stop here and fix it first (see Troubleshooting).

## 7. First real collection

One region first, to see it work:

```powershell
python -m gim.ingest --region europe --timespan 24h
```

You should see a small table, e.g. `europe   250   250   ok`. Then all regions:

```powershell
python -m gim.ingest
```

This takes ~30 s on purpose: GIM waits ≥ 6 s between requests to respect GDELT's rate limit.

Other examples:

```powershell
python -m gim.ingest --region africa --timespan 7d --topic "ceasefire,peace talks"
```

```powershell
python -m gim.ingest --help
```

## 8. Open the dashboard

```powershell
streamlit run app/dashboard.py
```

Your browser opens at http://localhost:8501. Stop the dashboard with `Ctrl+C` in PowerShell.

## 9. (Optional) Automatic collection every 3 hours

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
```

Check it in *Task Scheduler* → "GIM Ingest". Runs happen only while the PC is on, and the overlapping 6 h window means nothing is lost between runs.

## Where things are

| What | Where |
|---|---|
| Database | `data\gim.sqlite3` (back it up by copying it while nothing is running) |
| Logs | `logs\gim.jsonl` (one JSON object per line) |
| Regions / search terms | `src\gim\regions.py` |
| Category keywords | `src\gim\pipeline\classify.py` |

To inspect the database directly, install the free [DB Browser for SQLite](https://sqlitebrowser.org/).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run the `Set-ExecutionPolicy` command in step 3. |
| `ModuleNotFoundError: No module named 'gim'` | The venv isn't active (no `(.venv)` in the prompt), or step 4 was skipped. |
| `GDELT rejected the query: ...` | A search term in `regions.py` is invalid (e.g. a word that is too short, like "EU"). The message says which rule was broken. |
| `GDELT unavailable (HTTP 429)` | Too many requests. Wait a few minutes and don't lower `GIM_GDELT_MIN_INTERVAL_S`. |
| `database is locked` | Two collectors are running at once. Wait for one to finish. |
| `py` not found | Reinstall Python with "Add to PATH" ticked, then reopen PowerShell. |
