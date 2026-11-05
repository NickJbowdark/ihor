"""Visual identity: colours, CSS, Plotly template."""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

BG = "#0B1220"
PANEL = "#131C2E"
BORDER = "#22304A"
TEXT = "#E6EDF7"
MUTED = "#8A9BB8"
ACCENT = "#5AA9E6"
RED = "#E4572E"
AMBER = "#F2A541"
GREEN = "#3BB273"
VIOLET = "#9B8DE8"

CATEGORY_COLOURS = {
    "conflict": RED, "diplomatic": ACCENT, "political": VIOLET, "economic": AMBER,
    "international_orgs": "#4FC1C6", "humanitarian": "#E58FB4", "information": "#B8C4D9", "unclassified": "#5B6B85",
}
LEVEL_COLOURS = {"multiple_origins": GREEN, "official_plus_reporting": ACCENT, "official_only": VIOLET,
                 "single_origin": "#8A9BB8", "": "#5B6B85"}
LEVEL_LABELS = {"multiple_origins": "multiple origins", "official_plus_reporting": "official + 1 origin",
                "official_only": "official only", "single_origin": "single origin", "": "—"}

CSS = f"""
<style>
.block-container {{ padding-top: 1.6rem; max-width: 1500px; }}
h1, h2, h3 {{ letter-spacing: -0.01em; }}
.gim-hero {{ background: linear-gradient(120deg, #10203D 0%, #0B1220 70%); border: 1px solid {BORDER};
  border-radius: 14px; padding: 18px 22px; margin-bottom: 14px; }}
.gim-hero .t {{ font-size: 1.55rem; font-weight: 700; color: {TEXT}; }}
.gim-hero .s {{ color: {MUTED}; font-size: 0.92rem; margin-top: 2px; }}
.gim-kpi {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; padding: 12px 14px; height: 100%; }}
.gim-kpi .l {{ color: {MUTED}; font-size: 0.76rem; text-transform: uppercase; letter-spacing: .06em; }}
.gim-kpi .v {{ color: {TEXT}; font-size: 1.65rem; font-weight: 700; line-height: 1.25; }}
.gim-kpi .d {{ font-size: 0.78rem; color: {MUTED}; }}
.gim-card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; padding: 12px 14px; margin-bottom: 8px; }}
.gim-card .h {{ font-weight: 650; color: {TEXT}; font-size: 0.98rem; line-height: 1.35; }}
.gim-card .m {{ color: {MUTED}; font-size: 0.78rem; margin-top: 4px; }}
.gim-badge {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 600;
  margin: 2px 4px 2px 0; border: 1px solid rgba(255,255,255,0.08); white-space: nowrap; }}
.gim-claim {{ padding: 6px 0 6px 10px; border-left: 3px solid {BORDER}; margin: 6px 0; font-size: 0.93rem; }}
.gim-claim.ASSESSMENT {{ border-left-color: {ACCENT}; }}
.gim-claim.CONTEXT {{ border-left-color: {AMBER}; }}
.gim-claim.ATTRIBUTED_CLAIM {{ border-left-color: {VIOLET}; }}
.gim-claim.REPORTED_FACT {{ border-left-color: {GREEN}; }}
.gim-claim a {{ color: {ACCENT}; text-decoration: none; font-size: 0.8rem; }}
.gim-warn {{ background: rgba(228,87,46,0.12); border: 1px solid rgba(228,87,46,0.45); color: #FFB59E;
  border-radius: 10px; padding: 8px 12px; font-weight: 600; margin-bottom: 10px; }}
.gim-muted {{ color: {MUTED}; font-size: 0.82rem; }}
.gim-section {{ color: {MUTED}; text-transform: uppercase; font-size: 0.74rem; letter-spacing: .08em;
  margin: 14px 0 4px 0; font-weight: 700; }}
div[data-testid="stSidebarNav"] span {{ font-size: 0.93rem; }}
</style>
"""


def apply() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def plotly_template() -> None:
    t = go.layout.Template(pio.templates["plotly_dark"])
    t.layout.paper_bgcolor = "rgba(0,0,0,0)"
    t.layout.plot_bgcolor = "rgba(0,0,0,0)"
    t.layout.font = {"color": TEXT, "size": 12}
    t.layout.margin = {"l": 10, "r": 10, "t": 40, "b": 10}
    t.layout.colorway = [ACCENT, RED, AMBER, GREEN, VIOLET, "#4FC1C6", "#E58FB4", "#B8C4D9"]
    t.layout.xaxis = {"gridcolor": "#1C2840", "zerolinecolor": "#1C2840"}
    t.layout.yaxis = {"gridcolor": "#1C2840", "zerolinecolor": "#1C2840"}
    pio.templates["gim"] = t
    pio.templates.default = "gim"


def geo_layout(fig: go.Figure, height: int = 470) -> go.Figure:
    fig.update_geos(showframe=False, showcoastlines=True, coastlinecolor="#2A3A57", showland=True,
                    landcolor="#18233A", showocean=True, oceancolor="#0B1220", showcountries=True,
                    countrycolor="#2A3A57", projection_type="natural earth", bgcolor="rgba(0,0,0,0)")
    fig.update_layout(height=height, margin={"l": 0, "r": 0, "t": 30, "b": 0})
    return fig
