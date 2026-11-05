"""GIM — Global Intelligence Monitor dashboard.

Run from the project root:   streamlit run app/dashboard.py

This file only sets up the application shell (theme, login, sidebar, navigation).
Each page lives in src/gim/ui/pages/ and only displays data / calls gim.* functions.
"""
from __future__ import annotations

import streamlit as st

from gim.logging_setup import setup_logging
from gim.ui import auth, nav, theme
from gim.ui.pages import alerts, documents, events, monitor, overview, reports, settings, sources

setup_logging()
st.set_page_config(page_title="GIM · Global Intelligence Monitor", page_icon="🌐", layout="wide")
theme.apply()
theme.plotly_template()

if not auth.gate():
    st.stop()

st.sidebar.markdown("## 🌐 GIM")
st.sidebar.caption("Global Intelligence Monitor")
windows = {1: "Last 24 hours", 7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days"}
st.session_state.setdefault("gim_window_days", 7)
st.sidebar.selectbox("Time window", list(windows), format_func=windows.get, key="gim_window_days")

pages = {
    "overview": st.Page(overview.render, title="Global Overview", icon="🗺️", url_path="overview", default=True),
    "monitor": st.Page(monitor.render, title="Country & Region Monitor", icon="🎯", url_path="monitor"),
    "events": st.Page(events.render, title="Event Explorer", icon="🧭", url_path="events"),
    "reports": st.Page(reports.render, title="Intelligence Reports", icon="📝", url_path="reports"),
    "alerts": st.Page(alerts.render, title="Alerts & Watchlists", icon="🚨", url_path="alerts"),
    "documents": st.Page(documents.render, title="Document Feed", icon="📰", url_path="documents"),
    "health": st.Page(sources.health_page, title="Source Health", icon="🩺", url_path="health"),
    "catalogue": st.Page(sources.catalogue_page, title="Source Catalogue", icon="📚", url_path="catalogue"),
    "settings": st.Page(settings.render, title="Settings & System", icon="⚙️", url_path="settings"),
}
nav.PAGES.clear()
nav.PAGES.update(pages)
navigation = st.navigation({
    "Intelligence": [pages[k] for k in ("overview", "monitor", "events", "reports", "alerts")],
    "Operations": [pages[k] for k in ("documents", "health", "catalogue", "settings")],
})
st.sidebar.caption("Every statement is linked to its sources. AI output is a draft for analyst review.")
navigation.run()
