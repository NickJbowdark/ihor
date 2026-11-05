"""Cached, read-only data access for the UI (one short-lived connection per query)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from gim.config import get_settings
from gim.storage import db, intel

TTL = 60


def conn():
    """A fresh connection for the current script run (SQLite connections are cheap)."""
    return db.connect()


def db_key() -> str:
    return str(get_settings().db_path)


def window(days: float) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


@st.cache_data(ttl=TTL, show_spinner=False)
def events(db_path: str, days: float, regions: tuple = (), countries: tuple = (), categories: tuple = (),
           min_origins: int = 0, order: str = "prominence", limit: int = 2000, offset_days: float = 0) -> pd.DataFrame:
    c = db.connect(db_path)
    until = datetime.now(timezone.utc) - timedelta(days=offset_days)
    rows = db.query_events(c, until - timedelta(days=days), until, regions=list(regions) or None,
                           countries=list(countries) or None, categories=list(categories) or None,
                           min_origins=min_origins, order=order, limit=limit)
    c.close()
    return pd.DataFrame(rows)


@st.cache_data(ttl=TTL, show_spinner=False)
def search_events(db_path: str, text: str, days: float, limit: int = 200) -> pd.DataFrame:
    c = db.connect(db_path)
    rows = intel.search_events(c, text, after=datetime.now(timezone.utc) - timedelta(days=days), limit=limit)
    c.close()
    return pd.DataFrame(rows)


@st.cache_data(ttl=TTL, show_spinner=False)
def documents(db_path: str, days: float, regions: tuple = (), doc_types: tuple = (), limit: int = 20000) -> pd.DataFrame:
    c = db.connect(db_path)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.query_documents(c, since, regions=list(regions) or None, doc_types=list(doc_types) or None, limit=limit)
    c.close()
    df = pd.DataFrame(rows)
    if not df.empty:
        df["timeline_at"] = pd.to_datetime(df["timeline_at"], utc=True)
    return df


@st.cache_data(ttl=TTL, show_spinner=False)
def mention_stats(db_path: str, days: float) -> pd.DataFrame:
    c = db.connect(db_path)
    rows = intel.country_mention_stats(c, datetime.now(timezone.utc) - timedelta(days=days))
    c.close()
    return pd.DataFrame(rows)


@st.cache_data(ttl=TTL, show_spinner=False)
def daily(db_path: str, days: float, country: str | None = None, region: str | None = None) -> pd.DataFrame:
    c = db.connect(db_path)
    rows = intel.daily_counts(c, datetime.now(timezone.utc) - timedelta(days=days), country=country, region=region)
    c.close()
    return pd.DataFrame(rows)


def clear() -> None:
    st.cache_data.clear()
