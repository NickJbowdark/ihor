"""Render every dashboard page headless (Streamlit AppTest) on the synthetic demo database."""
import pytest
from streamlit.testing.v1 import AppTest

from gim import demo
from gim.config import get_settings

PAGES = ["overview", "monitor", "events", "reports", "alerts", "documents", "settings"]


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("ui") / "demo.sqlite3"
    demo.build_demo(path)
    return path


def _page_script(page: str) -> str:
    return f"""
import streamlit as st
from gim.ui import theme
from gim.ui.pages import {page}
theme.plotly_template()
st.session_state.setdefault("gim_window_days", 7)
{page}.render()
"""


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exceptions(page, demo_db, monkeypatch):
    monkeypatch.setenv("GIM_DB_PATH", str(demo_db))
    monkeypatch.setenv("GIM_LLM_PROVIDER", "none")
    get_settings.cache_clear()
    try:
        at = AppTest.from_string(_page_script(page), default_timeout=60)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        text = " ".join([m.value for m in at.markdown] + [e.value for e in at.error])
        assert "SYNTHETIC" in text                               # demo data is always labelled
    finally:
        get_settings.cache_clear()


def test_sources_pages_render(demo_db, monkeypatch):
    monkeypatch.setenv("GIM_DB_PATH", str(demo_db))
    get_settings.cache_clear()
    try:
        for fn in ("health_page", "catalogue_page"):
            at = AppTest.from_string(f"from gim.ui.pages import sources\nsources.{fn}()", default_timeout=60)
            at.run()
            assert not at.exception, [e.value for e in at.exception]
    finally:
        get_settings.cache_clear()
