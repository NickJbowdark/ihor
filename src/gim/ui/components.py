"""Reusable display components. ALL source/model text is HTML-escaped before display."""
from __future__ import annotations

import html
from datetime import datetime, timezone

import streamlit as st

from gim.ui import theme

DOC_TYPE = {
    "original_reporting": ("original reporting", theme.GREEN), "syndicated": ("syndicated copy", "#8A9BB8"),
    "official_statement": ("official statement", theme.VIOLET), "dataset_record": ("dataset record", theme.AMBER),
    "analysis_opinion": ("analysis/opinion", "#4FC1C6"), "unknown": ("origin unknown", "#5B6B85"),
}
CLAIM_TYPE = {"REPORTED_FACT": ("reported", theme.GREEN), "ATTRIBUTED_CLAIM": ("attributed", theme.VIOLET),
              "ASSESSMENT": ("assessment", theme.ACCENT), "CONTEXT": ("background · unverified", theme.AMBER)}
SECTION_TITLES = {
    "significance": "Why it matters", "key_developments": "Key developments",
    "actors": "Actors & documented positions", "background": "Background", "source_comparison": "Source comparison",
    "implications": "Implications", "uncertainty": "Uncertainty", "indicators": "Indicators to watch",
}


def esc(text) -> str:
    """HTML-escape untrusted text; also neutralise '$' so Streamlit does not render it as LaTeX math."""
    return html.escape(str(text if text is not None else "")).replace("$", "&#36;")


def safe_url(url: str) -> str:
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else ""


def badge(text: str, colour: str = "#5B6B85") -> str:
    return (f'<span class="gim-badge" style="background:{colour}22;color:{colour};border-color:{colour}55">'
            f"{esc(text)}</span>")


def age(iso: str | None) -> str:
    if not iso:
        return "never"
    mins = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() // 60)
    if mins < 0:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def hero(title: str, subtitle: str, chips: str = "") -> None:
    st.markdown(f'<div class="gim-hero"><div class="t">{esc(title)}</div><div class="s">{esc(subtitle)}</div>'
                f'<div style="margin-top:8px">{chips}</div></div>', unsafe_allow_html=True)


def kpis(items: list[tuple[str, str, str]]) -> None:
    """items: (label, value, small caption)."""
    cols = st.columns(len(items))
    for col, (label, value, cap) in zip(cols, items):
        col.markdown(f'<div class="gim-kpi"><div class="l">{esc(label)}</div><div class="v">{esc(value)}</div>'
                     f'<div class="d">{esc(cap)}</div></div>', unsafe_allow_html=True)


def section(title: str) -> None:
    st.markdown(f'<div class="gim-section">{esc(title)}</div>', unsafe_allow_html=True)


def synthetic_banner(conn) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM documents WHERE data_origin = 'fixture'").fetchone()[0]
    if row:
        st.markdown(f'<div class="gim-warn">🧪 DEMO / SYNTHETIC DATA — {row} test documents in this database. '
                    "They are NOT real events or intelligence. Unset GIM_DB_PATH to return to live data.</div>",
                    unsafe_allow_html=True)
    return bool(row)


def level_badge(level: str) -> str:
    return badge(theme.LEVEL_LABELS.get(level, level), theme.LEVEL_COLOURS.get(level, "#5B6B85"))


def category_badges(csv: str) -> str:
    return "".join(badge(c, theme.CATEGORY_COLOURS.get(c, "#5B6B85")) for c in (csv or "").split(",") if c)


def event_card(e: dict) -> str:
    countries = ", ".join((e.get("countries_mentioned") or "").split(",")[:4]) or "—"
    origin = badge("synthetic", theme.RED) if e.get("data_origin") != "live" else ""
    return (f'<div class="gim-card"><div class="h">{esc(e["headline"])}</div>'
            f'<div class="m">EVT-{e["id"]} · {esc(e["last_published_at"][:16].replace("T", " "))} UTC · '
            f'{e["documents_count"]} docs · {e["independent_origins"]} independent / {e["official_origins"]} official origins'
            f' · mentions: {esc(countries)}</div>'
            f'<div style="margin-top:6px">{level_badge(e.get("corroboration_level", ""))}'
            f'{category_badges(e.get("categories", ""))}{origin}</div></div>')


def claims_html(claims: list[dict], docs: dict[str, dict]) -> str:
    """Render validated claims grouped by section; citations link to the original URLs."""
    out = []
    by_section: dict[str, list[dict]] = {}
    for c in claims:
        by_section.setdefault(c.get("section", "key_developments"), []).append(c)
    for sec in list(SECTION_TITLES) + [s for s in by_section if s not in SECTION_TITLES]:
        items = by_section.get(sec)
        if not items:
            continue
        out.append(f'<div class="gim-section">{esc(SECTION_TITLES.get(sec, sec))}</div>')
        for c in items:
            label, colour = CLAIM_TYPE.get(c["claim_type"], (c["claim_type"], "#5B6B85"))
            extra = ""
            if c["claim_type"] == "ASSESSMENT" and c.get("confidence"):
                extra = badge(f"{c['confidence']} confidence", theme.ACCENT)
            if c["claim_type"] == "ATTRIBUTED_CLAIM" and c.get("attributed_to"):
                extra = badge(f"according to {c['attributed_to']}", theme.VIOLET)
            cites = []
            for ref in c.get("source_refs") or []:
                base = ref.split("#")[0]
                d = docs.get(base, {})
                url = safe_url(d.get("url", ""))
                title = esc(f"{d.get('publisher', '')}: {d.get('title', '')}")
                cites.append(f'<a href="{esc(url)}" target="_blank" title="{title}">[{esc(base)}]</a>' if url
                             else f"[{esc(base)}]")
            out.append(f'<div class="gim-claim {esc(c["claim_type"])}">{badge(label, colour)}{extra} '
                       f'{esc(c["text"])} {" ".join(cites)}</div>')
    return "".join(out)


def doc_rows_html(docs: list[dict]) -> str:
    out = []
    for d in docs:
        label, colour = DOC_TYPE.get(d["doc_type"], (d["doc_type"], "#5B6B85"))
        url = safe_url(d["url"])
        by = f" · originally {esc(d['original_reporter'])}" if d.get("original_reporter") and d["original_reporter"] != d["publisher"] else ""
        link = f' · <a href="{esc(url)}" target="_blank">open source ↗</a>' if url else ""
        out.append(f'<div class="gim-card" style="padding:8px 12px"><div class="h" style="font-size:.9rem">'
                   f'{esc(d["title"])}</div><div class="m">DOC-{d["id"]} · {esc(d["publisher"])}{by} · '
                   f'{esc((d.get("published_at") or d["timeline_at"])[:16].replace("T", " "))} UTC · via '
                   f'{esc(d.get("delivered_by") or d["source_id"])}{link}</div>'
                   f'<div>{badge(label, colour)}{badge("synthetic", theme.RED) if d["data_origin"] != "live" else ""}'
                   f'</div></div>')
    return "".join(out)
