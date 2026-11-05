"""Country & Region Monitor: the situation for one country or region over time."""
from __future__ import annotations

from collections import Counter

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from gim.pipeline.geo import COUNTRY_REGION, ISO3, all_countries
from gim.regions import REGIONS
from gim.storage import intel
from gim.ui import components as ui
from gim.ui import data, nav, theme
from gim.ui.pages.events import event_detail


def _list(series: pd.Series) -> Counter:
    return Counter(x for v in series.fillna("") for x in v.split(",") if x)


def render() -> None:
    conn = data.conn()
    ui.synthetic_banner(conn)
    key = data.db_key()
    days = nav.window_days()

    c1, c2, c3 = st.columns([1, 2, 1])
    kind = c1.radio("Monitor", ["Country", "Region"], horizontal=True,
                    index=0 if st.session_state.get("monitor_kind", "Country") == "Country" else 1)
    if kind == "Country":
        options = all_countries()
        current = st.session_state.get("monitor_country", "Ukraine")
        name = c2.selectbox("Country", options, index=options.index(current) if current in options else 0)
        st.session_state["monitor_country"] = name
        region_key = COUNTRY_REGION.get(name)
        subtitle = f"Country monitor · region: {REGIONS[region_key].name if region_key in REGIONS else '—'}"
    else:
        current = st.session_state.get("monitor_region", "europe")
        region_key = c2.selectbox("Region", list(REGIONS), index=list(REGIONS).index(current) if current in REGIONS else 0,
                                  format_func=lambda r: REGIONS[r].name)
        st.session_state["monitor_region"] = region_key
        name = REGIONS[region_key].name
        subtitle = "Regional monitor"
    span = c3.selectbox("Period", [7, 14, 30, 90], index=2, format_func=lambda d: f"{d} days")
    st.session_state["monitor_kind"] = kind

    filt = {"countries": (name,)} if kind == "Country" else {"regions": (region_key,)}
    ev = data.events(key, span, **filt)
    prev = data.events(key, span, offset_days=span, **filt)
    ui.hero(name, subtitle + f" · last {span} days")

    a1, a2 = st.columns([1, 1])
    if a1.button(f"⭐ Watch {name}", width="stretch"):
        intel.add_watch(conn, "country" if kind == "Country" else "region", name if kind == "Country" else region_key,
                        f"{kind}: {name}")
        st.success(f"Added to watchlist — alerts will flag new events about {name}.")
    if a2.button("📝 Generate report for this scope", width="stretch", type="primary"):
        nav.go("reports", report_prefill={"type": "country_profile" if kind == "Country" else "regional_conflict",
                                          "country": name if kind == "Country" else None,
                                          "region": region_key if kind == "Region" else None, "days": span})

    if ev.empty:
        st.info(f"No events about {name} in the last {span} days.")
        return
    docs_now = int(ev["documents_count"].sum())
    docs_prev = int(prev["documents_count"].sum()) if not prev.empty else 0
    ui.kpis([
        ("Events", f"{len(ev)}", f"{len(ev) - len(prev):+d} vs previous {span} d"),
        ("Documents", f"{docs_now}", f"{docs_now - docs_prev:+d} vs previous"),
        ("Multiple origins", f"{int((ev['independent_origins'] >= 2).sum())}", "events"),
        ("Official statements", f"{int((ev['official_origins'] > 0).sum())}", "events with ≥1"),
        ("Conflict-tagged", f"{int(ev['categories'].fillna('').str.contains('conflict').sum())}", "keyword topic tags"),
    ])

    left, right = st.columns([2, 1])
    with left:
        ui.section("Reporting trend")
        series = data.daily(key, span * 2, country=name if kind == "Country" else None,
                            region=region_key if kind == "Region" else None)
        if not series.empty:
            series["day"] = pd.to_datetime(series["day"])
            cutoff = series["day"].max() - pd.Timedelta(days=span)
            base = series[series["day"] <= cutoff]["documents"].mean() if (series["day"] <= cutoff).any() else None
            fig = go.Figure()
            fig.add_bar(x=series["day"], y=series["documents"], name="documents", marker_color=theme.ACCENT)
            fig.add_scatter(x=series["day"], y=series["events"], name="events", mode="lines+markers",
                            line={"color": theme.AMBER})
            if base is not None:
                fig.add_hline(y=base, line_dash="dot", line_color=theme.MUTED,
                              annotation_text=f"previous-period avg {base:.1f}/day")
            fig.update_layout(height=300, legend={"orientation": "h", "y": 1.12}, xaxis_title="")
            st.plotly_chart(fig, width="stretch")
        ui.section("Development of events")
        tl = ev.copy()
        tl["first"] = pd.to_datetime(tl["first_published_at"], utc=True)
        tl["topic"] = tl["categories"].replace("", "unclassified").str.split(",").str[0]
        tl["corroboration"] = tl["corroboration_level"].map(theme.LEVEL_LABELS)
        fig = px.scatter(tl, x="first", y="topic", size="documents_count", color="corroboration",
                         hover_name="headline", custom_data=["id"], height=330, size_max=28,
                         color_discrete_map={v: theme.LEVEL_COLOURS[k] for k, v in theme.LEVEL_LABELS.items()})
        fig.update_layout(xaxis_title="", yaxis_title="", legend_title_text="")
        sel = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                              key="monitor_timeline")
        pts = getattr(getattr(sel, "selection", None), "points", None) or \
            ((sel or {}).get("selection", {}).get("points", []) if isinstance(sel, dict) else [])
        if pts and pts[0].get("customdata"):
            st.session_state["monitor_event"] = int(pts[0]["customdata"][0])
        st.caption("Each bubble is an event (size = documents, colour = corroboration). Click one to inspect it.")
    with right:
        ui.section("Topics")
        cats = _list(ev["categories"])
        if cats:
            df = pd.DataFrame(cats.most_common(), columns=["topic", "events"])
            fig = px.bar(df, x="events", y="topic", orientation="h", color="topic", height=240,
                         color_discrete_map=theme.CATEGORY_COLOURS)
            fig.update_layout(showlegend=False, yaxis_title="", margin={"t": 5})
            st.plotly_chart(fig, width="stretch")
        ui.section("Countries co-mentioned")
        co = _list(ev["countries_mentioned"])
        co.pop(name, None)
        if co:
            df = pd.DataFrame(co.most_common(10), columns=["country", "events"])
            fig = px.bar(df, x="events", y="country", orientation="h", height=280, color_discrete_sequence=[theme.VIOLET])
            fig.update_layout(yaxis={"autorange": "reversed"}, yaxis_title="", margin={"t": 5})
            st.plotly_chart(fig, width="stretch")
        ui.section("Organisations mentioned")
        orgs = _list(ev["organizations"])
        st.markdown(" ".join(ui.badge(f"{o} · {n}", "#4FC1C6") for o, n in orgs.most_common(12)) or "—",
                    unsafe_allow_html=True)
        if kind == "Region":
            ui.section("Countries in this region (mentions)")
            stats = data.mention_stats(key, span)
            if not stats.empty:
                stats = stats[stats["country"].map(COUNTRY_REGION) == region_key]
                stats["iso3"] = stats["country"].map(ISO3)
                fig = px.choropleth(stats, locations="iso3", color="events", hover_name="country",
                                    color_continuous_scale="Blues")
                theme.geo_layout(fig, height=260)
                fig.update_geos(fitbounds="locations")
                st.plotly_chart(fig, width="stretch")

    st.markdown("### Events")
    top = ev.sort_values("prominence", ascending=False).head(30)
    table = pd.DataFrame({"ref": "EVT-" + top["id"].astype(str), "last": top["last_published_at"].str[:16],
                          "headline": top["headline"], "docs": top["documents_count"],
                          "origins": top["independent_origins"], "official": top["official_origins"],
                          "corroboration": top["corroboration_level"].map(theme.LEVEL_LABELS)})
    sel = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                       selection_mode="single-row", key="monitor_events",
                       column_config={"headline": st.column_config.TextColumn(width="large")})
    if sel.selection.rows:
        st.session_state["monitor_event"] = int(top.iloc[sel.selection.rows[0]]["id"])

    official = data.documents(key, span, doc_types=("official_statement",))
    if not official.empty:
        official = official[official["countries_mentioned"].fillna("").str.contains(name, regex=False)] \
            if kind == "Country" else official[official["regions"].fillna("").str.contains(region_key, regex=False)]
    if not official.empty:
        with st.expander(f"Official statements mentioning {name} ({len(official)})"):
            st.markdown(ui.doc_rows_html(official.head(30).assign(
                timeline_at=official["timeline_at"].astype(str)).to_dict("records")), unsafe_allow_html=True)

    eid = st.session_state.get("monitor_event")
    if eid and eid in set(ev["id"]):
        st.divider()
        event_detail(conn, int(eid), key="mon")
