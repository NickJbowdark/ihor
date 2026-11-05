"""Operations pages: source health and source catalogue (moved from the Phase 2 dashboard)."""
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


def health_page() -> None:
    conn = get_conn()
    st.title("Source health")
    data_origin_banner(conn)
    lock = db.get_lock(conn, LOCK_NAME)
    if lock:
        st.warning(f"⏳ A collection is running (started {_age(lock['acquired_at'])}, owner `{lock['owner']}`).")
    status = db.get_source_status(conn)
    validations = db.latest_validations(conn)
    rows = []
    for spec in REGISTRY.values():
        row = status.get(spec.id) or {}
        label = health_label(spec, row or None)
        last_result = row.get("last_result")
        rows.append({
            "state": STATUS_ICONS.get(label, label), "id": spec.id, "name": spec.name,
            "verification": verification_label(validations.get(spec.id)),
            "enabled": spec.enabled,
            "last success": _age(row.get("last_success_at")),
            "freshness": _freshness(spec, row),
            "last data": ("cached (HTTP 304)" if last_result == "not_modified"
                          else "live" if last_result else "—"),
            "retrieved": row.get("items_last_run", 0), "new": row.get("new_last_run", 0),
            "duplicates": row.get("duplicates_last_run", 0),
            "duration (s)": round((row.get("last_duration_ms") or 0) / 1000, 1),
            "HTTP": row.get("last_http_status"), "error kind": row.get("last_error_kind") or "",
            "failures in a row": row.get("consecutive_failures", 0),
            "empty runs in a row": row.get("consecutive_empty", 0),
            "backoff until": row.get("backoff_until") or "",
            "poll (min)": spec.poll_minutes,
            "credentials set": ("—" if not spec.auth_env else ("yes" if not spec.missing_credentials() else "NO")),
            "last error": row.get("last_error") or "", "_label": label,
        })
    df = pd.DataFrame(rows)
    counts = df["_label"].value_counts()
    cols = st.columns(6)
    groups = {"🟢 ok": ["ok", "ok_empty"], "🟡 attention": ["partial", "empty_suspicious", "stale"],
              "🔴 failing": ["degraded", "failing", "backoff"], "🔑 credentials": ["needs_credentials"],
              "⚪ never run": ["never_run"], "📝 planned": ["planned"]}
    for col, (title, keys) in zip(cols, groups.items()):
        col.metric(title, int(sum(counts.get(k, 0) for k in keys)))

    show_planned = st.toggle("Show planned / catalogued sources", value=False)
    view = df if show_planned else df[df["_label"] != "planned"]
    st.dataframe(view.drop(columns=["_label"]), hide_index=True, width="stretch")
    st.caption("**ok — 0 items**: the source answered correctly but had nothing new (not a failure). "
               "**empty ≥5 runs**: answered but empty five times in a row — check the feed. "
               "**partial**: part of the source failed (e.g. one GDELT region). **backing off**: waiting after "
               "an error or a server's Retry-After. **verification** comes only from `python -m gim.validate` "
               "(schema + document quality), never from a mere HTTP 200. Credential values are never displayed.")

    with st.expander("Validation history (python -m gim.validate)"):
        hist = conn.execute("SELECT source_id, validated_at, outcome, http_status, documents, valid_documents, "
                            "duration_ms, summary FROM source_validations ORDER BY id DESC LIMIT 100").fetchall()
        st.dataframe(pd.DataFrame([dict(r) for r in hist]), hide_index=True, width="stretch")
    with st.expander("Recent collection runs"):
        st.dataframe(pd.DataFrame(db.recent_runs(conn, 100)), hide_index=True, width="stretch")

    st.subheader("Run collection")
    if conn.execute("SELECT 1 FROM documents WHERE data_origin = 'fixture' LIMIT 1").fetchone():
        st.warning("Collection is disabled while a DEMO database is open, so live and fixture data never mix.")
        return
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Run all DUE enabled sources", type="primary", disabled=bool(lock)):
            with st.spinner("Collecting… (polite rate limits: GDELT alone takes ~30 s)"):
                try:
                    results = collect(conn=conn)
                except CollectorBusy as exc:
                    st.warning(str(exc))
                    results = None
            if results is not None:
                st.write(pd.DataFrame([r.__dict__ for r in results]) if results else "Nothing due.")
    with c2:
        sid = st.selectbox("Single source", [s.id for s in REGISTRY.values() if s.implemented],
                           format_func=lambda i: f"{REGISTRY[i].name} ({i})")
        dry = st.checkbox("Dry run (fetch + parse, store nothing)", value=True)
        if st.button("Run selected source", disabled=bool(lock)):
            with st.spinner(f"Fetching {sid}…"):
                try:
                    r = collect([sid], dry_run=dry, conn=conn)[0]
                except CollectorBusy as exc:
                    st.warning(str(exc))
                    return
            (st.success if r.status in ("ok", "ok_empty", "not_modified") else st.error)(
                f"{r.status}: fetched {r.fetched}, new {r.new}, duplicates {r.duplicates}, "
                f"{r.duration_ms} ms. {r.message}")
        st.caption("For a full diagnostic with schema and quality checks run `python -m gim.validate` in a terminal.")


def _freshness(spec, row: dict) -> str:
    if not row.get("last_success_at"):
        return "—"
    age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_success_at"])).total_seconds() / 60
    ratio = age_min / max(spec.poll_minutes, 1)
    return "fresh" if ratio <= 1.5 else "due" if ratio <= 3 else "stale"


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
                f"**verification:** see Source health (from `python -m gim.validate`)"
            )


