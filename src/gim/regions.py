"""Monitored regions and the keyword queries used to collect them.

A region query is deliberately *broad*: the goal of collection is recall.
Precision comes later (classification, deduplication, analyst review).

Edit these lists freely -- this is your "collection plan" in intelligence
cycle terms. Rules imposed by the GDELT DOC API:
  * Terms combined with OR must be wrapped in parentheses (we do that).
  * Very short words (e.g. "EU", "UN") are rejected -- use full names.
  * Multi-word phrases are quoted automatically.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    terms: tuple[str, ...]


REGIONS: dict[str, Region] = {
    r.key: r
    for r in [
        Region(
            "europe",
            "Europe",
            ("European Union", "NATO", "Ukraine", "Russia", "Kremlin", "OSCE",
             "European Commission", "Moldova", "Belarus", "Serbia", "Kosovo"),
        ),
        Region(
            "middle_east",
            "Middle East",
            ("Iran", "Israel", "Gaza", "Lebanon", "Syria", "Saudi Arabia", "Yemen",
             "Iraq", "Qatar", "United Arab Emirates", "Hezbollah", "Houthi"),
        ),
        Region(
            "africa",
            "Africa",
            ("Sahel", "Sudan", "Mali", "Burkina Faso", "Niger", "Ethiopia", "Somalia",
             "African Union", "ECOWAS", "Congo", "Libya", "Nigeria"),
        ),
        Region(
            "asia_pacific",
            "Asia-Pacific",
            ("China", "Taiwan", "India", "Japan", "North Korea", "South Korea",
             "South China Sea", "ASEAN", "Philippines", "Pakistan", "Myanmar"),
        ),
        Region(
            "americas",
            "Americas",
            ("State Department", "Venezuela", "Brazil", "Mexico", "Colombia", "Cuba",
             "Argentina", "Organization of American States", "Haiti", "Panama Canal"),
        ),
    ]
}


def _quote(term: str) -> str:
    term = term.strip().replace('"', "")
    return f'"{term}"' if " " in term else term


def build_query(region: Region, topic: str | None = None, language: str = "english") -> str:
    """Build a GDELT DOC API query string for a region (+ optional topic words)."""
    parts = ["(" + " OR ".join(_quote(t) for t in region.terms) + ")"]
    if topic:
        topic_terms = [t for t in topic.split(",") if t.strip()]
        if len(topic_terms) == 1:
            parts.append(_quote(topic_terms[0]))
        elif topic_terms:
            parts.append("(" + " OR ".join(_quote(t) for t in topic_terms) + ")")
    if language and language != "any":
        parts.append(f"sourcelang:{language}")
    return " ".join(parts)
