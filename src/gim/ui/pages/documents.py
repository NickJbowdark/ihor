"""Document feed: every collected document with provenance (the Phase 2 overview page)."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from gim.config import get_settings
from gim.collect import CollectorBusy, LOCK_NAME, collect, health_label
from gim.pipeline.classify import CATEGORIES
from gim.pipeline.corroboration import summarise
from gim.regions import REGIONS
from gim.sources.registry import DOC_TYPES, SOURCE_CATEGORIES, get_registry
from gim.storage import db
from gim.reports.render import md_escape
from gim.ui import data
from gim.ui.components import synthetic_banner
from gim.validate import verification_label

REGION_LABELS = {**{k: r.name for k, r in REGIONS.items()}, "global": "Global / unspecified"}
DOC_TYPE_LABELS = {
    "original_reporting": "📰 Original reporting",
    "syndicated": "🔁 Syndicated / reprint",
    "official_statement": "🏛️ Official statement",
    "dataset_record": "📊 Dataset record",
    "analysis_opinion": "🧠 Analysis / opinion",
    "unknown": "❔ Unknown (discovery)",
}
STATUS_ICONS = {
    "ok": "🟢 ok", "ok (disabled)": "🟢 ok (disabled)", "ok_empty": "🟢 ok — 0 items",
    "empty_suspicious": "🟡 empty ≥5 runs", "partial": "🟡 partial", "stale": "🟠 stale",
    "degraded": "🟠 degraded", "backoff": "⏸️ backing off", "failing": "🔴 failing",
    "needs_credentials": "🔑 needs credentials", "never_run": "⚪ never run",
    "disabled": "⚫ disabled", "planned": "📝 planned",
}
LEVEL_ICONS = {"official_only": "🏛️ official only", "single_origin": "1️⃣ single origin",
               "official_plus_reporting": "🏛️+1 official + 1 origin", "multiple_origins": "🔀 multiple origins"}
REGISTRY = get_registry()


REGISTRY = get_registry()


def get_conn():
    return data.conn()


def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def data_origin_banner(conn) -> None:
    """Always tell the analyst WHAT they are looking at: live collection or demo fixtures."""
    origins = dict(conn.execute("SELECT data_origin, COUNT(*) FROM documents GROUP BY data_origin").fetchall())
    db_name = get_settings().db_path.name
    synthetic_banner(conn)
    last = conn.execute("SELECT MAX(last_success_at) FROM source_status").fetchone()[0]
    if origins.get("live"):
        st.caption(f"📡 Live-collected data, stored locally (`{db_name}`). Most recent successful collection: "
                   f"**{_age(last)}**. Everything shown is the stored copy from that time — open the source link "
                   "for the current version.")


def render() -> None:
    conn = get_conn()
    st.caption("Every collected document with full provenance. For events, analysis and maps use the "
               "Intelligence pages.")
    st.sidebar.subheader("Document filters")
    sel_regions = st.sidebar.multiselect("Regions", list(REGION_LABELS), default=list(REGION_LABELS),
                                         format_func=REGION_LABELS.get)
    window = st.sidebar.radio("Time window", ["24 hours", "7 days", "30 days", "Custom"], horizontal=True, index=1)
    now = datetime.now(timezone.utc)
    if window == "Custom":
        d_from, d_to = st.sidebar.date_input("Date range (UTC)", (now.date() - timedelta(days=7), now.date()))
        since = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
        until = datetime.combine(d_to, time.max, tzinfo=timezone.utc)
    else:
        since = now - {"24 hours": timedelta(hours=24), "7 days": timedelta(days=7), "30 days": timedelta(days=30)}[window]
        until = None
    sel_doc_types = st.sidebar.multiselect("Document type", sorted(DOC_TYPES), format_func=DOC_TYPE_LABELS.get)
    sel_src_cats = st.sidebar.multiselect("Source category", sorted(SOURCE_CATEGORIES))
    implemented = [s for s in REGISTRY.values() if s.implemented]
    sel_sources = st.sidebar.multiselect("Sources", [s.id for s in implemented],
                                         format_func=lambda i: REGISTRY[i].name)
    sel_categories = st.sidebar.multiselect("Topics (keyword-based)", list(CATEGORIES))
    text = st.sidebar.text_input("Search title + summary")

    st.title("Document feed")
    data_origin_banner(conn)
    if not sel_regions:
        st.info("Select at least one region.")
        return
    rows = db.query_documents(conn, since, until, sel_regions, sel_categories, text.strip() or None,
                              sel_sources, sel_doc_types, sel_src_cats)
    if not rows:
        st.info("No documents for these filters. Collect data on the **Source health** page, "
                "or run `python -m gim.collect` in a terminal.")
        return
    df = pd.DataFrame(rows)
    df["timeline_at"] = pd.to_datetime(df["timeline_at"], utc=True)
    df["type"] = df["doc_type"].map(DOC_TYPE_LABELS)
    df["origin_key"] = df["original_reporter"].where(df["original_reporter"] != "", df["publisher"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Documents", f"{len(df):,}")
    c2.metric("Distinct headlines", f"{df['title_fingerprint'].nunique():,}")
    c3.metric("Official statements", f"{(df['doc_type'] == 'official_statement').sum():,}")
    c4.metric("Publishers", f"{df['publisher'].nunique():,}")

    tabs = st.tabs(["Feed", "Trends", "Media landscape", "Document detail", "Collection log"])

    with tabs[0]:
        view = df[["timeline_at", "title", "type", "publisher", "original_reporter", "regions",
                   "delivered_by", "data_origin", "url"]]
        st.dataframe(view, hide_index=True, width="stretch", height=600, column_config={
            "timeline_at": st.column_config.DatetimeColumn("Published (UTC)", format="YYYY-MM-DD HH:mm",
                                                           help="Publication time stated by the source (GDELT: first seen; collection time if the source gives none). NOT the event date."),
            "title": st.column_config.TextColumn("Title", width="large"),
            "original_reporter": st.column_config.TextColumn("Originally by", help="Wire agency / authoring organisation, when identifiable"),
            "delivered_by": st.column_config.TextColumn("Collected via"),
            "data_origin": st.column_config.TextColumn("Data"),
            "url": st.column_config.LinkColumn("Source", display_text="open ↗"),
        })
        st.download_button("Download CSV", view.to_csv(index=False).encode("utf-8"), "gim_documents.csv", "text/csv")

    with tabs[1]:
        freq = "h" if (until or now) - since <= timedelta(days=2) else "D"
        per_region = (df.assign(region=df["regions"].fillna("global").str.split(",")).explode("region")
                      .groupby([pd.Grouper(key="timeline_at", freq=freq), "region"]).size().reset_index(name="documents"))
        per_region["region"] = per_region["region"].map(REGION_LABELS).fillna(per_region["region"])
        st.plotly_chart(px.line(per_region, x="timeline_at", y="documents", color="region",
                                title="Documents per period and region"), width="stretch")
        by_type = df.groupby([pd.Grouper(key="timeline_at", freq=freq), "type"]).size().reset_index(name="documents")
        st.plotly_chart(px.bar(by_type, x="timeline_at", y="documents", color="type",
                               title="Documents by type"), width="stretch")
        st.caption("Volume measures attention and publishing activity, not event intensity.")

    with tabs[2]:
        st.caption("⚠️ **Publisher country** = where the outlet is based (GDELT metadata). It says nothing about "
                   "where events happen, and is therefore deliberately NOT drawn on a map.")
        by_pub = (df[df["publisher_country"] != ""].groupby("publisher_country").size()
                  .sort_values(ascending=False).head(25).reset_index(name="documents"))
        if not by_pub.empty:
            st.plotly_chart(px.bar(by_pub, x="documents", y="publisher_country", orientation="h",
                                   title="Documents by publisher country (GDELT only)"), width="stretch")
        top = df.groupby(["publisher", "doc_type"]).size().sort_values(ascending=False).head(30).reset_index(name="documents")
        st.dataframe(top, hide_index=True, width="stretch")

    with tabs[3]:
        options = df.head(500)
        choice = st.selectbox("Document", options.index, format_func=lambda i: options.loc[i, "title"][:120])
        d = options.loc[choice]
        st.markdown(f"### {md_escape(d['title'])}")
        if d["url"].startswith(("http://", "https://")):
            st.link_button("Open original ↗", d["url"])
        if d["summary"]:
            st.markdown("**Summary (as provided by the source):**")
            st.text(d["summary"])
        meta = {k: d[k] for k in ["doc_type", "publisher", "original_reporter", "source_id", "delivered_by",
                                  "source_category", "language", "publisher_country", "countries_mentioned",
                                  "regions", "categories",
                                  "published_at", "updated_at", "event_date", "collected_at", "last_seen_at",
                                  "data_origin", "canonical_url", "external_id", "revisions"]}
        meta["provenance"] = json.loads(d["provenance"] or "{}")
        st.json(meta, expanded=True)
        if not d["event_date"]:
            st.caption("Event date not yet extracted — the publication time is not the event date.")

    with tabs[4]:
        st.dataframe(pd.DataFrame(db.recent_runs(conn, 100)), hide_index=True, width="stretch")
