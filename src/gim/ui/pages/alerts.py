"""Alerts and watchlists."""
from __future__ import annotations

import json

import streamlit as st

from gim.alerts import MIN_SPIKE_DOCS, MULTI_ORIGIN_THRESHOLD, SPIKE_SIGMA, evaluate
from gim.pipeline.actors import ORGANIZATIONS
from gim.pipeline.geo import all_countries
from gim.regions import REGIONS
from gim.sources.registry import get_registry
from gim.storage import intel
from gim.ui import components as ui
from gim.ui import data, nav, theme

SEV = {"warning": theme.RED, "notice": theme.AMBER, "info": theme.ACCENT}


def render() -> None:
    conn = data.conn()
    ui.synthetic_banner(conn)
    ui.hero("Alerts & Watchlists", "Transparent rules — every alert shows the exact criteria that triggered it")
    left, right = st.columns([2, 1])
    with left:
        c1, c2 = st.columns([1, 1])
        only_open = c1.toggle("Unacknowledged only", value=True)
        if c2.button("Evaluate rules now", width="stretch"):
            n = evaluate(conn)
            st.success(f"{n} new alert(s).")
        alerts = intel.list_alerts(conn, unacknowledged=only_open)
        counts = {s: sum(a["severity"] == s for a in alerts) for s in SEV}
        ui.kpis([("Warnings", str(counts["warning"]), "volume spikes ≥3× baseline"),
                 ("Notices", str(counts["notice"]), "spikes / multi-origin"),
                 ("Info", str(counts["info"]), "watchlists / official statements")])
        if not alerts:
            st.info("No alerts.")
        for a in alerts[:150]:
            with st.container(border=True):
                st.markdown(f"{ui.badge(a['severity'], SEV.get(a['severity'], '#888'))}"
                            f"{ui.badge(a['rule'], '#5B6B85')}"
                            f"{ui.badge('synthetic', theme.RED) if a['data_origin'] != 'live' else ''}"
                            f"<span class='gim-muted'>{ui.age(a['created_at'])}</span>"
                            f"<div class='h' style='font-weight:650;margin-top:4px'>{ui.esc(a['title'])}</div>"
                            f"<div class='gim-muted'>{ui.esc(a['detail'])}</div>", unsafe_allow_html=True)
                b1, b2, b3 = st.columns([1, 1, 3])
                if a["event_id"] and b1.button("Open event", key=f"al_ev_{a['id']}"):
                    nav.go("events", selected_event=int(a["event_id"]))
                if not a["acknowledged_at"] and b2.button("Acknowledge", key=f"al_ack_{a['id']}"):
                    intel.acknowledge_alert(conn, a["id"])
                    st.rerun()
                with b3.popover("Criteria"):
                    st.json(json.loads(a["criteria"]))
    with right:
        ui.section("Rules")
        st.markdown(f"""
- **volume_spike** — documents mentioning a country in 24 h ≥ max({MIN_SPIKE_DOCS}, mean + {SPIKE_SIGMA}σ of the
  previous 28 days) and ≥ 2× mean (needs ≥ 7 days of history)
- **multi_origin_event** — an event reaches ≥ {MULTI_ORIGIN_THRESHOLD} independent origins (not a verification)
- **official_statement** — a new official statement on diplomatic / conflict / economic topics
- **watchlist_match** — a new or updated event matches one of your watches
""")
        ui.section("Watchlists")
        for w in intel.list_watches(conn):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"{ui.badge(w['kind'], theme.VIOLET)} {ui.esc(w['value'])}", unsafe_allow_html=True)
            if c2.button("✕", key=f"rm_w_{w['id']}"):
                intel.remove_watch(conn, w["id"])
                st.rerun()
        kind = st.selectbox("Watch type", ["country", "region", "organization", "keyword", "source"])
        with st.form("add_watch"):
            options = {"country": all_countries(), "region": list(REGIONS), "organization": list(ORGANIZATIONS),
                       "source": [s.id for s in get_registry().values() if s.implemented]}
            if kind == "keyword":
                keyword, choice = st.text_input("Keyword(s)"), None
            else:
                keyword, choice = "", st.selectbox("Value", options[kind])
            if st.form_submit_button("Add watch"):
                value = keyword.strip() if kind == "keyword" else choice
                try:
                    intel.add_watch(conn, kind, value)
                    st.success("Added.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
