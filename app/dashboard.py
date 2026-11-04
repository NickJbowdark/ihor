"""GIM dashboard.

Run from the project root:   streamlit run app/dashboard.py

Design rule: the dashboard only *reads and displays* (plus buttons that call
``gim`` functions). All collection, processing and storage logic lives in the
``gim`` package, so the UI can be replaced later without touching it.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from gim.collect import collect, health_label
from gim.config import get_settings
from gim.logging_setup import setup_logging
from gim.pipeline.classify import CATEGORIES
from gim.regions import REGIONS
from gim.sources.registry import DOC_TYPES, SOURCE_CATEGORIES, get_registry
from gim.storage import db

setup_logging()
st.set_page_config(page_title="Global Intelligence Monitor", page_icon="🌐", layout="wide")

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
    "ok": "🟢 ok", "ok (disabled)": "🟢 ok (disabled)", "stale": "🟠 stale", "degraded": "🟠 degraded",
    "failing": "🔴 failing", "needs_credentials": "🔑 needs credentials", "never_run": "⚪ never run",
    "disabled": "⚫ disabled", "planned": "📝 planned",
}
REGISTRY = get_registry()


def get_conn():
    # One connection per script run (Streamlit re-executes this file on every interaction).
    if "_conn" not in globals():
        globals()["_conn"] = db.connect()
    return globals()["_conn"]


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
    if origins.get("fixture"):
        st.error(f"🧪 **DEMO DATA** — this database (`{db_name}`) contains {origins['fixture']} synthetic test-fixture "
                 "documents. They are NOT real events. Unset `GIM_DB_PATH` to return to live data.")
    last = conn.execute("SELECT MAX(last_success_at) FROM source_status").fetchone()[0]
    if origins.get("live"):
        st.caption(f"📡 Live-collected data, stored locally (`{db_name}`). Most recent successful collection: "
                   f"**{_age(last)}**. Everything shown is the stored copy from that time — open the source link "
                   "for the current version.")


# ---------------------------------------------------------------------------
# Page 1: overview
# ---------------------------------------------------------------------------
def overview_page() -> None:
    conn = get_conn()
    st.sidebar.subheader("Filters")
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

    st.title("Global Overview")
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

    tabs = st.tabs(["Feed", "Story groups", "Trends", "Source landscape", "Document detail", "Collection log"])

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
        st.caption("Documents with an identical headline fingerprint are grouped. **Copies are not confirmations:** "
                   "the 'distinct origins' column counts different original reporters/publishers, and is an upper "
                   "bound — true independence must be checked by reading the sources (Phase 3 adds claim-level evidence).")
        groups = (df.groupby("title_fingerprint")
                  .agg(first_published=("timeline_at", "min"), title=("title", "first"),
                       copies=("id", "count"), publishers=("publisher", "nunique"),
                       distinct_origins=("origin_key", "nunique"),
                       syndicated=("doc_type", lambda s: int((s == "syndicated").sum())),
                       types=("type", lambda s: ", ".join(sorted(set(s)))), url=("url", "first"))
                  .reset_index(drop=True)
                  .sort_values(["publishers", "first_published"], ascending=[False, False]))
        st.dataframe(groups, hide_index=True, width="stretch", height=550, column_config={
            "first_published": st.column_config.DatetimeColumn("First published", format="YYYY-MM-DD HH:mm"),
            "title": st.column_config.TextColumn("Headline", width="large"),
            "url": st.column_config.LinkColumn("Example", display_text="open ↗"),
        })

    with tabs[2]:
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

    with tabs[3]:
        by_country = df[df["source_country"] != ""].groupby("source_country").size().reset_index(name="documents")
        if not by_country.empty:
            st.plotly_chart(px.choropleth(by_country, locations="source_country", locationmode="country names",
                                          color="documents", color_continuous_scale="Blues",
                                          title="Where the REPORTING comes from (publisher country, GDELT only)"),
                            width="stretch")
        top = df.groupby(["publisher", "doc_type"]).size().sort_values(ascending=False).head(30).reset_index(name="documents")
        st.dataframe(top, hide_index=True, width="stretch")

    with tabs[4]:
        options = df.head(500)
        choice = st.selectbox("Document", options.index, format_func=lambda i: options.loc[i, "title"][:120])
        d = options.loc[choice]
        st.markdown(f"### {d['title']}")
        st.markdown(f"[Open original ↗]({d['url']})" if d["url"].startswith(("http://", "https://")) else "")
        if d["summary"]:
            st.markdown("**Summary (as provided by the source):**")
            st.text(d["summary"])
        meta = {k: d[k] for k in ["doc_type", "publisher", "original_reporter", "source_id", "delivered_by",
                                  "source_category", "language", "countries", "regions", "categories",
                                  "published_at", "updated_at", "event_date", "collected_at", "last_seen_at",
                                  "data_origin", "canonical_url", "external_id", "revisions"]}
        meta["provenance"] = json.loads(d["provenance"] or "{}")
        st.json(meta, expanded=True)
        if not d["event_date"]:
            st.caption("Event date not yet extracted — the publication time is not the event date.")

    with tabs[5]:
        st.dataframe(pd.DataFrame(db.recent_runs(conn, 100)), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Page 2: source health
# ---------------------------------------------------------------------------
def health_page() -> None:
    conn = get_conn()
    st.title("Source health")
    data_origin_banner(conn)
    status = db.get_source_status(conn)
    rows = []
    for spec in REGISTRY.values():
        row = status.get(spec.id) or {}
        label = health_label(spec, row or None)
        rows.append({
            "state": STATUS_ICONS.get(label, label), "id": spec.id, "name": spec.name, "category": spec.category,
            "enabled": spec.enabled, "verified": spec.verified,
            "last success": _age(row.get("last_success_at")), "last attempt": _age(row.get("last_attempt_at")),
            "items (last run)": row.get("items_last_run", 0), "new": row.get("new_last_run", 0),
            "failures in a row": row.get("consecutive_failures", 0),
            "poll (min)": spec.poll_minutes, "auth": spec.auth,
            "credentials set": ("—" if not spec.auth_env else ("yes" if not spec.missing_credentials() else "NO")),
            "last error": row.get("last_error") or "", "_label": label,
        })
    df = pd.DataFrame(rows)
    counts = df["_label"].value_counts()
    cols = st.columns(5)
    for col, key in zip(cols, ["ok", "stale", "failing", "needs_credentials", "planned"]):
        col.metric(STATUS_ICONS[key], int(counts.get(key, 0)) + (int(counts.get("degraded", 0)) if key == "failing" else 0))

    show_planned = st.toggle("Show planned / catalogued sources", value=False)
    view = df if show_planned else df[df["_label"] != "planned"]
    st.dataframe(view.drop(columns=["_label"]), hide_index=True, width="stretch")
    st.caption("ok = last attempt succeeded · stale = no success for 3× the poll interval · degraded = recent "
               "failure · failing = ≥3 failures in a row · needs credentials = add them to `.env` (values are never shown).")

    st.subheader("Run collection")
    if conn.execute("SELECT 1 FROM documents WHERE data_origin = 'fixture' LIMIT 1").fetchone():
        st.warning("Collection is disabled while a DEMO database is open, so live and fixture data never mix.")
        return
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Run all DUE enabled sources", type="primary"):
            with st.spinner("Collecting… (polite rate limits: GDELT alone takes ~30 s)"):
                results = collect(conn=conn)
            st.write(pd.DataFrame([r.__dict__ for r in results]) if results else "Nothing due.")
    with c2:
        sid = st.selectbox("Single source", [s.id for s in REGISTRY.values() if s.implemented],
                           format_func=lambda i: f"{REGISTRY[i].name} ({i})")
        dry = st.checkbox("Dry run (fetch + parse, store nothing) — use this to verify a new feed", value=True)
        if st.button("Run selected source"):
            with st.spinner(f"Fetching {sid}…"):
                results = collect([sid], dry_run=dry, conn=conn)
            r = results[0]
            (st.success if r.status in ("ok", "not_modified") else st.error)(
                f"{r.status}: fetched {r.fetched}, new {r.new}. {r.message}")


# ---------------------------------------------------------------------------
# Page 3: source catalogue
# ---------------------------------------------------------------------------
def catalogue_page() -> None:
    st.title("Source catalogue")
    st.caption("Static description of every source, from `config/sources.toml`. Licence notes are our reading of "
               "each source's terms — always check the source itself. There is deliberately no per-organisation "
               "reliability score: evidence quality is assessed per claim.")
    cat = st.selectbox("Category", ["all", *sorted(SOURCE_CATEGORIES)])
    for spec in REGISTRY.values():
        if cat != "all" and spec.category != cat:
            continue
        with st.expander(f"{spec.name} — {spec.organization}  ·  `{spec.connector}`"):
            st.markdown(
                f"- **Category:** {spec.category} · **default document type:** {DOC_TYPE_LABELS[spec.doc_type]}\n"
                f"- **Coverage:** {spec.coverage or '—'} · **languages:** {', '.join(spec.languages) or '—'}\n"
                f"- **Endpoint:** `{spec.url or 'not configured'}` · **homepage:** {spec.homepage or '—'}\n"
                f"- **Authentication:** {spec.auth}" + (f" (`{'`, `'.join(spec.auth_env)}` in .env)" if spec.auth_env else "") + "\n"
                f"- **Licence / terms:** {spec.licence}\n"
                f"- **Usage notes:** {spec.usage_notes or '—'}\n"
                f"- **Poll interval:** {spec.poll_minutes} min · **enabled:** {spec.enabled} · "
                f"**verified against a real response:** {spec.verified}"
            )


st.sidebar.title("🌐 GIM")
st.sidebar.caption("Global Intelligence Monitor · Phase 2")
nav = st.navigation([
    st.Page(overview_page, title="Overview", icon="🗺️", default=True),
    st.Page(health_page, title="Source health", icon="🩺", url_path="health"),
    st.Page(catalogue_page, title="Source catalogue", icon="📚", url_path="catalogue"),
])
nav.run()
