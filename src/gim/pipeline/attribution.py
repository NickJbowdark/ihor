"""Who ORIGINALLY reported this? Detecting wire / agency copy.

When 150 sites print the same Reuters dispatch, that is ONE source, not 150.
We look for the standard attribution markers news agencies require
("(Reuters) -", "(AP) -", "AFP", "-- Reuters") in titles and summaries, and
compare the agency with the publisher.

This is a heuristic: absence of a marker does not prove original reporting.
"""
from __future__ import annotations

import re

# agency name -> (marker regexes, domains that ARE the agency)
AGENCIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Reuters": ((r"\(Reuters\)", r"[-–—]\s*Reuters\b", r"\bReuters reported\b"), ("reuters.com",)),
    "Associated Press": ((r"\(AP\)", r"\bAssociated Press\b"), ("apnews.com", "ap.org")),
    "AFP": ((r"\(AFP\)", r"\bAFP\b", r"Agence France-Presse"), ("afp.com",)),
    "dpa": ((r"\(dpa\)", r"\bdpa\b"), ("dpa.com",)),
    "EFE": ((r"\(EFE\)", r"\bEFE\b"), ("efe.com",)),
    "ANSA": ((r"\(ANSA\)",), ("ansa.it",)),
    "Xinhua": ((r"\(Xinhua\)", r"\bXinhua\b"), ("xinhuanet.com", "news.cn")),
    "TASS": ((r"\bTASS\b",), ("tass.com", "tass.ru")),
    "Anadolu Agency": ((r"\bAnadolu Agency\b", r"\(AA\)"), ("aa.com.tr",)),
    "Kyodo": ((r"\bKyodo\b",), ("english.kyodonews.net", "kyodonews.net")),
    "Yonhap": ((r"\bYonhap\b",), ("en.yna.co.kr", "yna.co.kr")),
    "PTI": ((r"\(PTI\)",), ("ptinews.com",)),
    "Bloomberg": ((r"\(Bloomberg\)",), ("bloomberg.com",)),
}
_COMPILED = {
    name: ([re.compile(p) for p in pats], domains) for name, (pats, domains) in AGENCIES.items()
}


def agency_for_domain(domain: str) -> str | None:
    d = domain.lower().removeprefix("www.")
    for name, (_, domains) in _COMPILED.items():
        if any(d == x or d.endswith("." + x) for x in domains):
            return name
    return None


def detect_original_reporter(text: str) -> str | None:
    for name, (patterns, _) in _COMPILED.items():
        if any(p.search(text) for p in patterns):
            return name
    return None


def classify_reporting(publisher_domain: str, text: str, default_doc_type: str) -> tuple[str, str]:
    """Return (doc_type, original_reporter) for a NEWS item.

    * publisher is itself an agency          -> original_reporting by that agency
    * another agency's marker in the text    -> syndicated, original_reporter = agency
    * otherwise                              -> the registry default (unchanged)
    """
    own = agency_for_domain(publisher_domain)
    if own:
        return "original_reporting", own
    agency = detect_original_reporter(text)
    if agency:
        return "syndicated", agency
    return default_doc_type, ""
