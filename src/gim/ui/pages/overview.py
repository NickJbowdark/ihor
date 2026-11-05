"""Global Overview -- the intelligence centre's front page."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from gim.analysis.llm import load_config
from gim.pipeline.geo import ISO3
from gim.pipeline.prominence import FORMULA
from gim.regions import REGIONS
from gim.storage import db, intel
from gim.ui import components as ui
from gim.ui import data, nav, theme

REGION_LABELS = {**{k: r.name for k, r in REGIONS.items()}, "global": "Global"}


def render() -> None:
    conn = data.conn()
    days = nav.window_days()
    key = data.db_key()
    synthetic = ui.synthetic_banner(conn)

    last = conn.execute("SELECT MAX(last_success_at) FROM source_status").fetchone()[0]
    cfg = load_config()
    ai_chip = ui.badge(f"AI: {cfg.provider} / {cfg.model}" if not cfg.problems() else "AI: not configured (baseline)",
                       theme.GREEN if not cfg.problems() else theme.AMBER)
    ui.hero("Global Overview",
            f"Geopolitical developments in the last {int(days) if days >= 1 else 24} {'days' if days >= 1 else 'hours'} · "
            "source-attributed, corroboration-aware",
            ui.badge(f"last collection {ui.age(last)}", theme.ACCENT) + ai_chip
            + (ui.badge("synthetic data", theme.RED) if synthetic else ui.badge("live data", theme.GREEN)))

    ev = data.events(key, days)
    prev = data.events(key, days, offset_days=days)
    alerts = intel.list_alerts(conn, unacknowledged=True, limit=200)
    if ev.empty:
        st.info("No events in this period yet. Collect data (Operations → Source health) or load the demo "
                "(`python -m gim.demo`).")
        return

    countries = {c for v in ev["countries_mentioned"] for c in (v or "").split(",") if c}
    multi = int((ev["independent_origins"] >= 2).sum())
    official = int((ev["official_origins"] > 0).sum())
    ui.kpis([
        ("Events", f"{len(ev):,}", f"{len(ev) - len(prev):+,} vs previous period"),
        ("Multiple origins", f"{multi:,}", "≥2 apparently independent origins"),
        ("With official statements", f"{official:,}", "position documented"),
        ("Countries in the news", f"{len(countries):,}", "mentioned, not located"),
        ("Documents", f"{int(ev['documents_count'].sum()):,}", "incl. copies"),
        ("Open alerts", f"{len(alerts):,}", "transparent rules"),
    ])

    left, right = st.columns([2.1, 1])
    with left:
        mode = st.radio("Map layer", ["Countries in the news (mentions)", "Established event locations"],
                        horizontal=True, label_visibility="collapsed")
        if mode.startswith("Countries"):
            stats = data.mention_stats(key, days)
            stats = stats[stats["country"].isin(ISO3)] if not stats.empty else stats
            if stats.empty:
                st.info("No country mentions yet.")
            else:
                stats["iso3"] = stats["country"].map(ISO3)
                fig = px.choropleth(stats, locations="iso3", color="events", hover_name="country",
                                    hover_data={"iso3": False, "events": True, "conflict_events": True},
                                    color_continuous_scale=["#16304F", "#2E6DA4", "#5AA9E6", "#F2A541", "#E4572E"],
                                    title="Events MENTIONING each country — media attention, not event locations")
                theme.geo_layout(fig)
                sel = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                                      key="overview_map")
                pts = (sel or {}).get("selection", {}).get("points", []) if isinstance(sel, dict) else \
                    getattr(getattr(sel, "selection", None), "points", [])
                if pts:
                    iso = pts[0].get("location")
                    name = next((c for c, i in ISO3.items() if i == iso), None)
                    if name:
                        nav.go("monitor", monitor_kind="Country", monitor_country=name)
        else:
            located = intel.located_events(conn, *data.window(days))
            unknown = len(ev) - len(located)
            if located:
                df = pd.DataFrame(located)
                df = df[df["event_country"].isin(ISO3)]
                by = df.groupby("event_country").size().reset_index(name="events")
                by["iso3"] = by["event_country"].map(ISO3)
                fig = px.choropleth(by, locations="iso3", color="events", hover_name="event_country",
                                    color_continuous_scale="Reds", title="Events by ESTABLISHED location (country level)")
                theme.geo_layout(fig)
                st.plotly_chart(fig, width="stretch")
            st.caption(f"{len(located)} events have an established location (dataset or analyst); "
                       f"**{unknown} are unknown** and are not drawn. GIM never uses publisher location or mere "
                       "mentions as event location, and never invents coordinates.")
        c1, c2 = st.columns([3, 1])
        pick = c1.selectbox("Open country monitor", sorted(countries), index=None, placeholder="Choose a country…",
                            label_visibility="collapsed")
        if c2.button("Open monitor", disabled=not pick, width="stretch"):
            nav.go("monitor", monitor_kind="Country", monitor_country=pick)

    with right:
        ui.section("Regional pulse")
        reg = (ev.assign(region=ev["regions"].fillna("").str.split(",")).explode("region")
               .query("region != ''").groupby("region").size().rename("now"))
        reg_prev = (prev.assign(region=prev["regions"].fillna("").str.split(",")).explode("region")
                    .query("region != ''").groupby("region").size().rename("previous")) if not prev.empty \
            else pd.Series(dtype=int, name="previous")
        pulse = pd.concat([reg, reg_prev], axis=1).fillna(0).astype(int).reset_index(names="region")
        pulse["label"] = pulse["region"].map(REGION_LABELS).fillna(pulse["region"])
        pulse = pulse.sort_values("now")
        fig = px.bar(pulse, x=["previous", "now"], y="label", orientation="h", barmode="group",
                     color_discrete_sequence=["#2A3A57", theme.ACCENT], height=260)
        fig.update_layout(legend_title_text="", xaxis_title="events", yaxis_title="", margin={"t": 10})
        st.plotly_chart(fig, width="stretch")
        ui.section("Topic mix")
        cats = (ev["categories"].replace("", "unclassified").str.split(",").explode().value_counts()
                .reset_index())
        cats.columns = ["topic", "events"]
        fig = px.pie(cats, names="topic", values="events", hole=0.55, height=250, color="topic",
                     color_discrete_map=theme.CATEGORY_COLOURS)
        fig.update_layout(margin={"t": 10, "b": 10}, legend={"font": {"size": 10}})
        st.plotly_chart(fig, width="stretch")

    st.markdown("### Top developments")
    st.caption(f"Ranked by prominence = {FORMULA}. Prominence measures independent and official attention, "
               "not truth or importance.")
    top = ev.sort_values("prominence", ascending=False).head(10).to_dict("records")
    cols = st.columns(2)
    for i, e in enumerate(top):
        with cols[i % 2]:
            st.markdown(ui.event_card(e), unsafe_allow_html=True)
            if st.button("Open event", key=f"ov_open_{e['id']}"):
                nav.go("events", selected_event=int(e["id"]))

    a, b = st.columns([2, 1])
    with a:
        st.markdown("### Activity")
        act = data.activity(key, days)
        if not act.empty:
            fig = px.area(act, x="period", y="documents", color="topic", height=300,
                          color_discrete_map=theme.CATEGORY_COLOURS)
            fig.update_layout(xaxis_title="", legend_title_text="")
            st.plotly_chart(fig, width="stretch")
            st.caption("Publishing activity (documents per period, by primary topic). Volume reflects attention, "
                       "not event intensity.")
    with b:
        st.markdown("### Latest alerts")
        sev_col = {"warning": theme.RED, "notice": theme.AMBER, "info": theme.ACCENT}
        for al in alerts[:6]:
            st.markdown(f'<div class="gim-card" style="padding:8px 12px">{ui.badge(al["severity"], sev_col.get(al["severity"], "#888"))}'
                        f'<span class="gim-muted">{ui.esc(al["rule"])} · {ui.age(al["created_at"])}</span>'
                        f'<div class="h" style="font-size:.86rem;margin-top:4px">{ui.esc(al["title"])}</div></div>',
                        unsafe_allow_html=True)
        if st.button("All alerts →"):
            nav.go("alerts")
