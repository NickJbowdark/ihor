"""Event Explorer: search the archive, inspect events, sources, history and AI analysis."""
from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st

from gim.analysis.engine import analyse_event, get_analyst
from gim.analysis.llm import BudgetExceeded, LLMError, NotConfigured
from gim.analysis.retrieval import related_history
from gim.pipeline.corroboration import summarise
from gim.pipeline.geo import all_countries
from gim.pipeline.prominence import FORMULA
from gim.regions import REGIONS
from gim.storage import db, intel
from gim.ui import components as ui
from gim.ui import data, nav, theme

CATEGORIES = ["conflict", "diplomatic", "political", "economic", "international_orgs", "humanitarian", "information"]


def event_detail(conn, event_id: int, key: str = "ed") -> None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        st.warning(f"EVT-{event_id} not found.")
        return
    e = dict(row)
    docs = db.event_documents(conn, [event_id])[event_id]
    summary = summarise(docs)

    st.markdown(f"#### EVT-{event_id} · {ui.esc(e['headline'])}")
    st.markdown(
        ui.badge(f"{e['first_published_at'][:16].replace('T', ' ')} → {e['last_published_at'][:16].replace('T', ' ')} UTC",
                 theme.ACCENT)
        + ui.level_badge(e["corroboration_level"]) + ui.category_badges(e["categories"])
        + ui.badge(f"prominence {e['prominence']}", "#5B6B85")
        + (ui.badge("synthetic", theme.RED) if e["data_origin"] != "live" else ""), unsafe_allow_html=True)
    st.caption(f"Prominence = {FORMULA}")

    g1, g2, g3 = st.columns(3)
    g1.markdown(f"**Event location**  \n{ui.esc(e['event_country'] or 'unknown')} "
                f"<span class='gim-muted'>(basis: {ui.esc(e['location_basis'])})</span>", unsafe_allow_html=True)
    g2.markdown(f"**Countries mentioned**  \n{ui.esc((e['countries_mentioned'] or '—').replace(',', ', '))}")
    g3.markdown(f"**Organisations mentioned**  \n{ui.esc((e['organizations'] or '—').replace(',', ', '))}")
    with st.expander("Set event location (analyst)"):
        st.caption("Only set what the sources establish. Mentioned or publisher countries are NOT a location.")
        with st.form(f"{key}_loc_{event_id}"):
            ctry = st.selectbox("Where did it happen?", ["(unknown)"] + all_countries())
            involved = st.multiselect("Countries involved", all_countries())
            note = st.text_input("Basis / note (e.g. 'venue stated in DOC-12 and DOC-15')")
            if st.form_submit_button("Save location"):
                db.set_event_location(conn, event_id, event_country=None if ctry == "(unknown)" else ctry,
                                      countries_involved=involved, basis="analyst", note=note)
                data.clear()
                st.success("Saved with basis 'analyst'.")
                st.rerun()

    tabs = st.tabs(["AI analysis", f"Sources ({len(docs)})", "Corroboration", "History", "Timeline"])
    with tabs[0]:
        _analysis_tab(conn, e, docs, key)
    with tabs[1]:
        st.markdown(ui.doc_rows_html(docs), unsafe_allow_html=True)
    with tabs[2]:
        st.markdown(f"**{ui.esc(summary.label)}**")
        for c in summary.caveats:
            st.caption("• " + c)
        st.dataframe(pd.DataFrame([{"origin": o.label, "kind": o.kind, "documents": len(o.document_ids),
                                    "publishers": ", ".join(o.publishers[:6]),
                                    "DOC ids": ", ".join(f"DOC-{i}" for i in o.document_ids[:8])}
                                   for o in summary.origins]), hide_index=True, width="stretch")
        st.caption("Copies of one wire story count as ONE origin. Official statements document positions, "
                   "not facts. No reliability score is assigned to any organisation.")
    with tabs[3]:
        hist = related_history(conn, e, k=6)
        if not hist:
            st.caption("No related earlier events in the archive (they must share a country or organisation).")
        for h in hist:
            hid = int(h.ref.split("-")[1])
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"**{h.ref}** · {h.first_published_at[:10]} · {ui.esc(h.headline)}")
            if c2.button("Open", key=f"{key}_hist_{event_id}_{hid}"):
                st.session_state["selected_event"] = hid
                st.rerun()
    with tabs[4]:
        tl = pd.DataFrame(docs)
        tl["time"] = pd.to_datetime(tl["timeline_at"], utc=True)
        fig = px.scatter(tl, x="time", y="doc_type", color="doc_type", hover_name="title",
                         hover_data={"publisher": True, "doc_type": False, "time": True}, height=260)
        fig.update_traces(marker={"size": 11})
        fig.update_layout(showlegend=False, yaxis_title="", xaxis_title="")
        st.plotly_chart(fig, width="stretch")


def _analysis_tab(conn, e: dict, docs: list[dict], key: str) -> None:
    lookup = {f"DOC-{d['id']}": d for d in docs}
    existing = intel.latest_event_analysis(conn, e["id"])
    analyst, problems = get_analyst(conn)
    c1, c2 = st.columns([3, 1])
    if problems:
        c1.caption("AI not configured — the button runs the deterministic baseline (attributed statements only). "
                   + "; ".join(problems[:2]))
    else:
        spend = intel.month_spend(conn)
        c1.caption(f"Provider **{analyst.name} / {analyst.model}** · this month USD {spend['cost_usd']:.4f} of "
                   f"USD {analyst.client.config.monthly_budget_usd:.2f} budget · results are cached")
    if c2.button("Run analysis" if not existing else "Re-run / update", key=f"{key}_run_{e['id']}",
                 type="primary", width="stretch"):
        with st.spinner("Analysing (sources → claims → validation)…"):
            try:
                analyse_event(conn, e["id"], analyst)
            except BudgetExceeded as exc:
                st.error(f"Budget cap: {exc}")
            except (NotConfigured, LLMError) as exc:
                st.error(f"AI error: {exc}")
            except Exception as exc:  # never crash the page
                st.error(f"Analysis failed: {type(exc).__name__}: {exc}")
        existing = intel.latest_event_analysis(conn, e["id"])
    if not existing:
        st.info("No analysis yet.")
        return
    content = json.loads(existing["content"])
    rejected = json.loads(existing["rejected"])
    for ref in content.get("evidence_refs", []):
        if ref not in lookup:
            d = conn.execute("SELECT * FROM documents WHERE id = ?", (int(ref.split('-')[1]),)).fetchone()
            if d:
                lookup[ref] = dict(d)
    st.markdown(f"<span class='gim-muted'>{ui.esc(content['provider'])} / {ui.esc(content['model'])} · "
                f"{ui.esc(content['generated_at'][:16])} UTC · prompts {ui.esc(content['prompt_version'])} · "
                f"DRAFT — analyst review required</span>", unsafe_allow_html=True)
    st.markdown(ui.claims_html(content["claims"], lookup), unsafe_allow_html=True)
    if content.get("open_questions"):
        ui.section("Open questions")
        for q in content["open_questions"]:
            st.markdown(f"- {ui.esc(q)}")
    if rejected:
        with st.expander(f"⚠️ {len(rejected)} statement(s) rejected by the evidence validator"):
            for r in rejected:
                st.markdown(f"- “{ui.esc(r['claim'].get('text', '')[:300])}” — {ui.esc('; '.join(r['problems']))}")


def render() -> None:
    conn = data.conn()
    ui.synthetic_banner(conn)
    ui.hero("Event Explorer", "Search the historical archive, compare sources, run source-grounded analysis")
    days = nav.window_days()
    f1, f2, f3, f4 = st.columns([2.2, 1.2, 1.2, 1.2])
    text = f1.text_input("Search (full text, whole archive within the time window)", placeholder="e.g. ceasefire Sudan")
    regions = f2.multiselect("Regions", list(REGIONS), format_func=lambda r: REGIONS[r].name)
    countries = f3.multiselect("Countries mentioned", all_countries())
    cats = f4.multiselect("Topics", CATEGORIES)
    g1, g2, g3 = st.columns([1.2, 1.2, 2])
    span = g1.selectbox("Period", [1, 7, 30, 90, 365, 3650], index=[1, 7, 30, 90, 365, 3650].index(
        min([1, 7, 30, 90, 365, 3650], key=lambda x: abs(x - days))),
        format_func=lambda d: {1: "24 hours", 7: "7 days", 30: "30 days", 90: "90 days", 365: "1 year",
                               3650: "whole archive"}[d])
    min_origins = g2.slider("Min. origins (independent + official)", 0, 5, 0)
    order = g3.radio("Sort", ["prominence", "recent"], horizontal=True)

    key = data.db_key()
    if text.strip():
        ev = data.search_events(key, text.strip(), span)
    else:
        ev = data.events(key, span, tuple(regions), tuple(countries), tuple(cats), min_origins, order)
    if not ev.empty and text.strip():
        if regions:
            ev = ev[ev["regions"].fillna("").apply(lambda v: bool(set(v.split(",")) & set(regions)))]
        if countries:
            ev = ev[ev["countries_mentioned"].fillna("").apply(lambda v: bool(set(v.split(",")) & set(countries)))]
        if cats:
            ev = ev[ev["categories"].fillna("").apply(lambda v: bool(set(v.split(",")) & set(cats)))]
        ev = ev[(ev["independent_origins"] + ev["official_origins"]) >= min_origins]
    if ev.empty:
        st.info("No events match.")
    else:
        table = pd.DataFrame({
            "ref": "EVT-" + ev["id"].astype(str), "last (UTC)": ev["last_published_at"].str[:16].str.replace("T", " "),
            "headline": ev["headline"], "docs": ev["documents_count"], "indep.": ev["independent_origins"],
            "official": ev["official_origins"],
            "corroboration": ev["corroboration_level"].map(theme.LEVEL_LABELS),
            "topics": ev["categories"].str.replace(",", ", "),
            "countries mentioned": ev["countries_mentioned"].str.replace(",", ", "),
            "location": ev["event_country"].fillna("unknown"), "prominence": ev["prominence"],
        })
        st.caption(f"{len(table)} events · select a row for details")
        sel = st.dataframe(table, hide_index=True, width="stretch", height=380, on_select="rerun",
                           selection_mode="single-row", key="events_table",
                           column_config={"headline": st.column_config.TextColumn(width="large")})
        rows = sel.selection.rows if hasattr(sel, "selection") else []
        if rows:
            st.session_state["selected_event"] = int(ev.iloc[rows[0]]["id"])
    eid = st.session_state.get("selected_event")
    if eid:
        st.divider()
        event_detail(conn, int(eid), key="ex")
