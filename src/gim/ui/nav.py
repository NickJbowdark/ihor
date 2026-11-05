"""Cross-page navigation helpers (pages are registered by app/dashboard.py on each run)."""
from __future__ import annotations

import streamlit as st

PAGES: dict = {}


def go(page: str, **state) -> None:
    for k, v in state.items():
        st.session_state[k] = v
    if page in PAGES:
        st.switch_page(PAGES[page])


def window_days() -> float:
    return float(st.session_state.get("gim_window_days", 7))
