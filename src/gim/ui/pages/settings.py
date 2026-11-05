"""Settings & system status: AI provider, budget, database, backups, security."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from gim.analysis.llm import LLMClient, LLMError, load_config
from gim.config import get_settings
from gim.maintenance import backup_dir, integrity, make_backup
from gim.storage import db, intel
from gim.ui import components as ui
from gim.ui import data, theme


def render() -> None:
    conn = data.conn()
    ui.synthetic_banner(conn)
    ui.hero("Settings & System", "Configuration is read from .env (edit the file, then restart the dashboard). "
            "Secrets are never displayed.")
    cfg = load_config()
    left, right = st.columns(2)
    with left:
        st.markdown("### AI analysis engine")
        problems = cfg.problems()
        st.markdown(ui.badge("ready" if not problems else "not ready", theme.GREEN if not problems else theme.AMBER),
                    unsafe_allow_html=True)
        st.table(pd.DataFrame([
            ["Provider (GIM_LLM_PROVIDER)", cfg.provider], ["Endpoint (GIM_LLM_BASE_URL)", cfg.base_url or "—"],
            ["Model (GIM_LLM_MODEL)", cfg.model or "—"], ["API key (GIM_LLM_API_KEY)", "set" if cfg.api_key else "not set"],
            ["Price input / output per M tokens (USD)", f"{cfg.price_input_per_m} / {cfg.price_output_per_m}"],
            ["Monthly budget (USD)", f"{cfg.monthly_budget_usd:.2f}"],
            ["Max output tokens / temperature", f"{cfg.max_output_tokens} / {cfg.temperature}"],
        ], columns=["Setting", "Value"]).set_index("Setting"))
        for p in problems:
            st.caption("• " + p)
        spend = intel.month_spend(conn)
        st.progress(min(spend["cost_usd"] / max(cfg.monthly_budget_usd, 1e-9), 1.0),
                    text=f"This month: USD {spend['cost_usd']:.4f} · {spend['calls']} calls · "
                         f"{spend['input_tokens']:,} in / {spend['output_tokens']:,} out tokens")
        if st.button("Test AI connection (one tiny call)", disabled=bool(problems)):
            try:
                out = LLMClient(cfg, conn).chat_json(
                    [{"role": "system", "content": "Reply with a JSON object only."},
                     {"role": "user", "content": 'Return exactly {"ok": true}.'}], "connection_test")
                st.success(f"Provider answered: {out}")
            except LLMError as exc:
                st.error(str(exc))
        with st.expander("How to configure DeepSeek or Ollama"):
            st.markdown("""
**DeepSeek API** (in `.env`):
```
GIM_LLM_PROVIDER=deepseek
GIM_LLM_MODEL=<copy the exact model id from api-docs.deepseek.com>
GIM_LLM_API_KEY=<your key>
GIM_LLM_PRICE_INPUT_PER_M=<USD per million input tokens, from the pricing page>
GIM_LLM_PRICE_OUTPUT_PER_M=<USD per million output tokens>
GIM_LLM_MONTHLY_BUDGET_USD=5
```
**Ollama (local, free)**: install Ollama, `ollama pull <model>`, then
```
GIM_LLM_PROVIDER=ollama
GIM_LLM_MODEL=<name shown by `ollama list`>
```
Restart the dashboard after editing `.env`.""")
        calls = intel.recent_llm_calls(conn, 50)
        if calls:
            with st.expander("Recent AI calls"):
                st.dataframe(pd.DataFrame(calls), hide_index=True, width="stretch")
    with right:
        st.markdown("### Database")
        path = get_settings().db_path
        ok, detail, version = integrity(path) if path.exists() else (False, "missing", 0)
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("documents", "events", "reports", "alerts", "watchlists", "llm_calls")}
        st.table(pd.DataFrame([["File", str(path)], ["Size", f"{path.stat().st_size / 1e6:.1f} MB" if path.exists() else "—"],
                               ["Schema", f"v{version} (code v{db.SCHEMA_VERSION})"], ["Integrity", "ok" if ok else detail]]
                              + [[k, f"{v:,}"] for k, v in counts.items()], columns=["Item", "Value"]).set_index("Item"))
        if st.button("Create backup now"):
            st.success(f"Backup written: {make_backup(conn).name}")
        backups = sorted(backup_dir().glob("*.sqlite3"))
        st.caption(f"{len(backups)} backup(s) in {backup_dir()} · restore with `python -m gim.maintenance restore <file> --yes`")
        st.markdown("### Security")
        pw = bool(os.getenv("GIM_DASHBOARD_PASSWORD_SHA256") or os.getenv("GIM_DASHBOARD_PASSWORD"))
        st.markdown(f"- Dashboard password: **{'enabled' if pw else 'not set'}** "
                    "(set `GIM_DASHBOARD_PASSWORD_SHA256`, see docs/DEPLOYMENT.md)\n"
                    "- Server address: `127.0.0.1` by default (`.streamlit/config.toml`) — not reachable from other "
                    "machines until you deploy behind Tailscale or an authenticating proxy\n"
                    "- Credentials are redacted from logs, errors, reports and recorded fixtures")
        if st.button("Clear dashboard caches"):
            data.clear()
            st.success("Caches cleared.")
