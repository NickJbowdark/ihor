"""GIM dashboard (Phase 1).

Run from the project root:   streamlit run app/dashboard.py

Design rule: the dashboard only *reads and displays*. All collection,
processing and storage logic lives in the ``gim`` package, so the UI can be
replaced later (e.g. by a FastAPI + React front end) without touching it.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from gim.ingest import ingest_regions
from gim.logging_setup import setup_logging
from gim.pipeline.classify import CATEGORIES
from gim.regions import REGIONS
from gim.storage import db

setup_logging()
st.set_page_config(page_title="Global Intelligence Monitor", page_icon="🌐", layout="wide")


def get_conn():
    # A fresh connection per script run: SQLite connections are cheap, and this
    # avoids sharing one connection across Streamlit's worker threads.
    if "_conn" not in globals():
        globals()["_conn"] = db.connect()
    return globals()["_conn"]


def load(since, until, regions, categories, text) -> pd.DataFrame:
    rows = db.query_articles(get_conn(), since, until, regions, categories, text)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["seen_at"] = pd.to_datetime(df["seen_at"], utc=True)
    return df


# --- Sidebar: filters -------------------------------------------------------
st.sidebar.title("🌐 GIM")
st.sidebar.caption("Global Intelligence Monitor · Phase 1")

region_labels = {k: r.name for k, r in REGIONS.items()}
sel_regions = st.sidebar.multiselect(
    "Regions", options=list(REGIONS), default=list(REGIONS), format_func=region_labels.get
)
window = st.sidebar.radio("Time window", ["24 hours", "7 days", "30 days", "Custom"], horizontal=True)
now = datetime.now(timezone.utc)
if window == "Custom":
    d_from, d_to = st.sidebar.date_input("Date range (UTC)", (now.date() - timedelta(days=7), now.date()))
    since = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
    until = datetime.combine(d_to, time.max, tzinfo=timezone.utc)
else:
    since = now - {"24 hours": timedelta(hours=24), "7 days": timedelta(days=7), "30 days": timedelta(days=30)}[window]
    until = None
sel_categories = st.sidebar.multiselect("Categories (keyword-based)", list(CATEGORIES))
text = st.sidebar.text_input("Search headlines")
collapse = st.sidebar.toggle("Collapse syndicated duplicates", value=True,
                             help="Group articles with the same headline fingerprint into one story.")

st.sidebar.divider()
st.sidebar.subheader("Collection")
fetch_span = st.sidebar.selectbox("Fetch window", ["1h", "6h", "24h", "7d"], index=2)
if st.sidebar.button("Fetch latest from GDELT", type="primary", disabled=not sel_regions):
    with st.spinner(f"Querying GDELT for {len(sel_regions)} region(s) — ~6 s per region (rate limit)…"):
        results = ingest_regions(sel_regions, timespan=fetch_span, conn=get_conn())
    for r in results:
        if r.error:
            st.sidebar.error(f"{region_labels[r.region]}: {r.error}")
        else:
            st.sidebar.success(f"{region_labels[r.region]}: {r.fetched} fetched, {r.new} new")

# --- Main area ---------------------------------------------------------------
st.title("Global Overview")
if not sel_regions:
    st.info("Select at least one region.")
    st.stop()

df = load(since, until, sel_regions, sel_categories, text.strip() or None)
if df.empty:
    st.info("No articles stored for these filters yet. Click **Fetch latest from GDELT** in the sidebar, "
            "or run `python -m gim.ingest` in a terminal.")
    st.stop()

stories = (
    df.groupby("title_fingerprint")
    .agg(seen_at=("seen_at", "min"), title=("title", "first"), url=("url", "first"),
         outlets=("domain", "nunique"), countries=("source_country", "nunique"),
         regions=("regions", "first"), categories=("categories", "first"),
         domain=("domain", "first"))
    .reset_index()
    .sort_values(["outlets", "seen_at"], ascending=[False, False])
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Articles", f"{len(df):,}")
c2.metric("Distinct stories", f"{len(stories):,}", help="After grouping identical headline fingerprints")
c3.metric("Outlets", f"{df['domain'].nunique():,}")
c4.metric("Outlet countries", f"{df['source_country'].nunique():,}")

tab_feed, tab_trends, tab_sources, tab_runs = st.tabs(["Feed", "Trends", "Source landscape", "Collection log"])

with tab_feed:
    if collapse:
        st.caption("Stories ranked by number of distinct outlets carrying the same headline. "
                   "⚠️ Many outlets ≠ independent corroboration: wire copy is often republished verbatim.")
        view = stories[["seen_at", "title", "outlets", "countries", "regions", "categories", "domain", "url"]]
    else:
        view = df[["seen_at", "title", "domain", "source_country", "language", "regions", "categories", "url"]]
    st.dataframe(
        view,
        hide_index=True,
        width="stretch",
        height=600,
        column_config={
            "seen_at": st.column_config.DatetimeColumn("First seen (UTC)", format="YYYY-MM-DD HH:mm"),
            "title": st.column_config.TextColumn("Headline", width="large"),
            "url": st.column_config.LinkColumn("Source", display_text="open ↗"),
        },
    )
    st.download_button("Download CSV", view.to_csv(index=False).encode("utf-8"), "gim_articles.csv", "text/csv")

with tab_trends:
    freq = "h" if (until or now) - since <= timedelta(days=2) else "D"
    per_region = (
        df.assign(region=df["regions"].str.split(","))
        .explode("region")
        .groupby([pd.Grouper(key="seen_at", freq=freq), "region"])
        .size()
        .reset_index(name="articles")
    )
    per_region["region"] = per_region["region"].map(region_labels).fillna(per_region["region"])
    st.plotly_chart(px.line(per_region, x="seen_at", y="articles", color="region",
                            title="Coverage volume over time (articles first seen per period)"),
                    width="stretch")
    cats = (
        df.assign(category=df["categories"].replace("", "unclassified").str.split(","))
        .explode("category")["category"].value_counts().reset_index()
    )
    cats.columns = ["category", "articles"]
    st.plotly_chart(px.bar(cats, x="articles", y="category", orientation="h",
                           title="Category tags (rule-based baseline)"), width="stretch")
    st.caption("Volume measures media attention, not event intensity. A spike can mean a real development — "
               "or simply that one story is being widely republished.")

with tab_sources:
    by_country = df[df["source_country"] != ""].groupby("source_country").size().reset_index(name="articles")
    fig = px.choropleth(by_country, locations="source_country", locationmode="country names",
                        color="articles", color_continuous_scale="Blues",
                        title="Where the REPORTING comes from (outlet country, not event location)")
    st.plotly_chart(fig, width="stretch")
    top = df.groupby("domain").size().sort_values(ascending=False).head(25).reset_index(name="articles")
    st.dataframe(top, hide_index=True, width="stretch")

with tab_runs:
    runs = pd.DataFrame(db.recent_runs(get_conn(), 50))
    st.dataframe(runs, hide_index=True, width="stretch")
