"""Intelligence Reports: generate, review, export."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from gim.analysis.engine import get_analyst
from gim.analysis.llm import BudgetExceeded, LLMError
from gim.pipeline.geo import all_countries
from gim.regions import REGIONS
from gim.reports.builder import ReportError, generate_report
from gim.reports.pdf import render_pdf
from gim.reports.specs import SPECS
from gim.storage import intel
from gim.ui import components as ui
from gim.ui import data, theme

STATUS_COLOURS = {"draft": theme.AMBER, "reviewed": theme.ACCENT, "final": theme.GREEN}


def _generate(conn, synthetic: bool) -> None:
    pre = st.session_state.pop("report_prefill", None) or {}
    keys = list(SPECS)
    rtype = st.selectbox("Report type", keys, index=keys.index(pre.get("type", "daily_brief")),
                         format_func=lambda k: SPECS[k].name)
    spec = SPECS[rtype]
    st.caption(spec.purpose)
    c1, c2, c3 = st.columns(3)
    countries = all_countries()
    country = c1.selectbox("Country" + (" (required)" if spec.needs == "country" else " (optional)"),
                           ["—"] + countries,
                           index=(countries.index(pre["country"]) + 1) if pre.get("country") in countries else 0)
    region = c2.selectbox("Region" + (" (required)" if spec.needs == "region" else " (optional)"),
                          ["—"] + list(REGIONS), index=(list(REGIONS).index(pre["region"]) + 1)
                          if pre.get("region") in REGIONS else 0,
                          format_func=lambda r: REGIONS[r].name if r in REGIONS else r)
    days = c3.slider("Period (days)", 1, 120, int(pre.get("days") or spec.default_days))
    topic = st.text_input("Topic keywords" + (" (required)" if spec.needs == "topic" else " (optional)"),
                          placeholder="e.g. grain corridor Black Sea")

    analyst, problems = get_analyst(conn)
    if problems:
        st.markdown(f'<div class="gim-card">{ui.badge("baseline", theme.AMBER)} No AI provider ready — the report '
                    f'will contain attributed source statements and statistics only.<div class="m">'
                    f'{ui.esc("; ".join(problems))}</div></div>', unsafe_allow_html=True)
    else:
        spend = intel.month_spend(conn)
        cfg = analyst.client.config
        st.markdown(f'<div class="gim-card">{ui.badge("AI", theme.GREEN)} {ui.esc(cfg.provider)} / '
                    f'{ui.esc(cfg.model)}<div class="m">Month to date USD {spend["cost_usd"]:.4f} of '
                    f'USD {cfg.monthly_budget_usd:.2f}. A report makes ~1 call per event (cached) + 1 synthesis call; '
                    f'each call is capped by the budget.</div></div>', unsafe_allow_html=True)
    use_ai = st.toggle("Use AI analysis", value=not problems, disabled=bool(problems))
    if synthetic:
        st.warning("This database contains synthetic test data; the report will be labelled SYNTHETIC.")
    if st.button("Generate report", type="primary"):
        if not use_ai:
            analyst, _ = get_analyst(conn, prefer_ai=False)
        bar = st.progress(0.0, text="Selecting events…")
        try:
            rid = generate_report(
                conn, rtype, analyst, regions=[region] if region in REGIONS else [],
                countries=[country] if country in countries else [], topic=topic.strip() or None, days=days,
                live_only=not synthetic, progress=lambda i, n, t: bar.progress(min(i / n, 1.0), text=t))
        except (ReportError, BudgetExceeded, LLMError) as exc:
            bar.empty()
            st.error(f"Report not generated: {exc}")
            return
        bar.empty()
        st.session_state["selected_report"] = rid
        st.success(f"Report #{rid} created as DRAFT. Open it in the library below.")


def _library(conn) -> None:
    reports = intel.list_reports(conn)
    if not reports:
        st.info("No reports yet.")
        return
    df = pd.DataFrame(reports)
    table = pd.DataFrame({"#": df["id"], "created (UTC)": df["created_at"].str[:16], "title": df["title"],
                          "type": df["report_type"].map(lambda k: SPECS[k].name if k in SPECS else k),
                          "status": df["status"], "analysis": df["provider"] + " / " + df["model"],
                          "data": df["data_origin"]})
    sel = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                       selection_mode="single-row", key="reports_table", height=230,
                       column_config={"title": st.column_config.TextColumn(width="large")})
    if sel.selection.rows:
        st.session_state["selected_report"] = int(df.iloc[sel.selection.rows[0]]["id"])
    rid = st.session_state.get("selected_report") or int(df.iloc[0]["id"])
    rep = intel.get_report(conn, rid)
    if not rep:
        return
    content = json.loads(rep["content"])
    content["status"] = rep["status"]
    st.divider()
    h1, h2, h3, h4 = st.columns([3, 1, 1, 1])
    h1.markdown(f"{ui.badge(rep['status'].upper(), STATUS_COLOURS.get(rep['status'], '#888'))}"
                f"{ui.badge(rep['data_origin'], theme.RED if rep['data_origin'] != 'live' else theme.GREEN)}"
                f"<span class='gim-muted'> report #{rid}</span>", unsafe_allow_html=True)
    h2.download_button("⬇ PDF", render_pdf(content), f"gim_report_{rid}.pdf", "application/pdf",
                       width="stretch")
    h3.download_button("⬇ Markdown", rep["markdown"].encode("utf-8"), f"gim_report_{rid}.md", "text/markdown",
                       width="stretch")
    with h4.popover("Review", width="stretch"):
        note = st.text_area("Reviewer note", value=rep.get("reviewer_note") or "")
        new = st.radio("Status", ["draft", "reviewed", "final"], index=["draft", "reviewed", "final"].index(rep["status"]))
        if st.button("Save review"):
            intel.set_report_status(conn, rid, new, note)
            st.rerun()
    if rep.get("reviewer_note"):
        st.caption(f"Reviewer note: {rep['reviewer_note']}")
    with st.container(border=True):
        st.markdown(rep["markdown"])


def render() -> None:
    conn = data.conn()
    synthetic = ui.synthetic_banner(conn)
    ui.hero("Intelligence Reports", "Daily briefs, weekly assessments, country profiles, conflict and diplomatic "
            "analyses, custom research briefs — source-grounded drafts for analyst review")
    tab1, tab2 = st.tabs(["Report library", "Generate new report"])
    with tab2:
        _generate(conn, synthetic)
    with tab1:
        _library(conn)
