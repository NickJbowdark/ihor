"""International organisations and blocs mentioned in a text (deterministic).

Together with countries (pipeline.geo) these are the *candidate actors* of an
event. Whether an actor actually took a position is established later only
from cited statements (AI analysis with source references, or the analyst).
"""
from __future__ import annotations

import re
from functools import lru_cache

# canonical name -> aliases (short all-caps acronyms are matched case-sensitively)
ORGANIZATIONS: dict[str, tuple[str, ...]] = {
    "United Nations": ("UN", "U.N.", "United Nations"),
    "UN Security Council": ("Security Council", "UNSC"),
    "UN General Assembly": ("General Assembly", "UNGA"),
    "European Union": ("EU", "European Union", "European Commission", "Council of the EU", "European Council"),
    "NATO": ("NATO", "North Atlantic Treaty Organization"),
    "OSCE": ("OSCE",),
    "Council of Europe": ("Council of Europe",),
    "African Union": ("African Union", "AU"),
    "ECOWAS": ("ECOWAS",),
    "ASEAN": ("ASEAN",),
    "Arab League": ("Arab League",),
    "Gulf Cooperation Council": ("Gulf Cooperation Council", "GCC"),
    "Organization of American States": ("Organization of American States", "OAS"),
    "G7": ("G7", "Group of Seven"),
    "G20": ("G20",),
    "BRICS": ("BRICS",),
    "Shanghai Cooperation Organisation": ("Shanghai Cooperation Organisation", "Shanghai Cooperation Organization", "SCO"),
    "IAEA": ("IAEA", "International Atomic Energy Agency"),
    "International Criminal Court": ("International Criminal Court", "ICC"),
    "International Court of Justice": ("International Court of Justice", "ICJ"),
    "IMF": ("IMF", "International Monetary Fund"),
    "World Bank": ("World Bank",),
    "WTO": ("WTO", "World Trade Organization"),
    "WHO": ("World Health Organization",),
    "UNHCR": ("UNHCR",),
    "OCHA": ("OCHA",),
    "World Food Programme": ("World Food Programme", "WFP"),
    "UNICEF": ("UNICEF",),
    "OPEC": ("OPEC",),
}


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[re.Pattern, str]]:
    out = []
    for org, aliases in ORGANIZATIONS.items():
        for a in sorted(set(aliases), key=len, reverse=True):
            flags = 0 if (a.replace(".", "").isupper() and len(a) <= 5) else re.IGNORECASE
            out.append((re.compile(r"(?<![\w.])" + re.escape(a) + r"(?![\w])", flags), org))
    return out


def tag_organizations(text: str) -> list[str]:
    """Organisations mentioned in ``text``, in order of first mention."""
    hits: dict[str, int] = {}
    for pattern, org in _patterns():
        m = pattern.search(text)
        if m and (org not in hits or m.start() < hits[org]):
            hits[org] = m.start()
    # "Security Council" also implies "United Nations" being mentioned -- keep both, no inference beyond text
    return sorted(hits, key=hits.get)
