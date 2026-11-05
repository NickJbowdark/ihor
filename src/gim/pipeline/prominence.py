"""Prominence: a TRANSPARENT ranking of events for display order.

It measures how much independent and official attention an event received --
not how important it "really" is, and not whether it is true. The formula is
shown in the UI so the analyst can always see why something ranks high.

    prominence = 2.0 * independent_origins
               + 1.5 * official_origins
               + log2(1 + documents)
               + 1.0 if a conflict / diplomatic / international-organisation category is tagged
"""
from __future__ import annotations

import math

FORMULA = ("2.0 × independent origins + 1.5 × official origins + log2(1 + documents) "
           "+ 1 if tagged conflict/diplomatic/international_orgs")
KEY_CATEGORIES = {"conflict", "diplomatic", "international_orgs"}


def prominence(independent_origins: int, official_origins: int, documents: int, categories: list[str]) -> float:
    score = 2.0 * independent_origins + 1.5 * official_origins + math.log2(1 + max(documents, 0))
    if KEY_CATEGORIES & set(categories):
        score += 1.0
    return round(score, 2)
