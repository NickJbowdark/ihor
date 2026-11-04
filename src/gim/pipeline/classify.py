"""Transparent keyword-based category tagging (Phase 1 baseline).

This is intentionally simple and explainable: every tag can be traced to a
keyword. In Phase 3 an LLM classifier is added, and this rule set becomes the
*baseline* you measure the AI against. Never replace a baseline you cannot
beat on a labelled test set.
"""
from __future__ import annotations

import re

CATEGORIES: dict[str, tuple[str, ...]] = {
    "diplomatic": (
        "diplomat", "diplomacy", "embassy", "ambassador", "foreign minister",
        "summit", "talks", "negotiat", "treaty", "agreement", "bilateral",
        "envoy", "state visit", "recognition", "accord",
    ),
    "conflict": (
        "war", "attack", "airstrike", "air strike", "missile", "drone", "shelling",
        "ceasefire", "truce", "troops", "offensive", "militant", "insurgent",
        "killed", "clashes", "military", "frontline", "invasion", "artillery",
        "defence", "defense", "drills", "warship", "navy",
    ),
    "political": (
        "election", "vote", "parliament", "president", "prime minister", "coup",
        "protest", "opposition", "referendum", "government", "cabinet", "junta",
        "impeach", "resign",
    ),
    "economic": (
        "sanction", "tariff", "trade", "export", "import", "oil", "gas", "energy",
        "pipeline", "economy", "inflation", "currency", "supply chain", "embargo",
        "investment", "debt",
    ),
    "international_orgs": (
        "united nations", "security council", "general assembly", "nato",
        "european union", "european commission", "osce", "african union", "ecowas",
        "asean", "imf", "world bank", "wto", "g7", "g20", "brics",
    ),
    "information": (
        "disinformation", "misinformation", "propaganda", "fake news", "fact check",
        "censorship", "deepfake", "influence operation", "hack", "cyber",
    ),
    "humanitarian": (
        "refugee", "displaced", "humanitarian", "famine", "food insecurity",
        "aid", "civilians", "unhcr", "unicef", "ocha", "earthquake", "flood",
        "cholera", "evacuat",
    ),
}

# Keywords match at the START of a word, so "negotiat" matches "negotiations"
# but "war" does not match "award". Multi-word keywords are matched as phrases.
_PATTERNS = {
    cat: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")", re.IGNORECASE)
    for cat, kws in CATEGORIES.items()
}
# Words where prefix matching gives false positives, needing a full-word match.
_FULL_WORD = {"war": re.compile(r"\bwars?\b", re.IGNORECASE), "aid": re.compile(r"\baid\b", re.IGNORECASE)}


def classify_title(title: str) -> list[str]:
    found = []
    for cat, pattern in _PATTERNS.items():
        for match in pattern.finditer(title):
            word = match.group(0).lower()
            if word in _FULL_WORD and not _FULL_WORD[word].match(title, match.start()):
                continue
            found.append(cat)
            break
    return found
