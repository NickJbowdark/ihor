"""Normalisation and defensive cleaning of untrusted source data.

Everything that arrives from the internet is UNTRUSTED. Titles can contain
control characters, HTML, absurd lengths, or (later, when an AI reads them)
text crafted to manipulate the model ("ignore previous instructions...").
We clean data at the boundary, before it is stored or displayed.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_TITLE_LEN = 500

# Query parameters that only track clicks and never change the article.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref", "ref_src", "cmpid", "ocid", "smid", "partner", "share",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_TAGS = re.compile(r"<[^>]{0,200}>")
_WS = re.compile(r"\s+")
# " - BBC News", " | Reuters" at the end of a headline
_OUTLET_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{2,40}$")
_STOPWORDS = frozenset(
    "a an the of in on at to for from by with and or but is are was were be been as "
    "after over amid says said new about into than its it this that".split()
)


def is_safe_url(url: str) -> bool:
    """Only http(s) links may be stored/shown (blocks javascript:, data:, file: ...)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def canonicalize_url(url: str) -> str:
    """Reduce URL variants of the same article to one canonical form.

    https://WWW.Example.com/a/?utm_source=x#top  ->  https://example.com/a
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    # Treat http and https as the same article.
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def clean_text(text: str | None, max_len: int = MAX_TITLE_LEN) -> str:
    """Strip HTML, control/bidi characters and excess whitespace; cap length."""
    if not text:
        return ""
    text = html.unescape(text)
    text = _TAGS.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _WS.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def key_tokens(title: str) -> list[str]:
    """Lower-cased, accent-free headline words without stopwords or outlet suffix."""
    t = _OUTLET_SUFFIX.sub("", title)
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    tokens = re.findall(r"\w+", t)
    return sorted({tok for tok in tokens if tok not in _STOPWORDS and len(tok) > 1})


def title_fingerprint(title: str) -> str:
    """Fingerprint for spotting the same story syndicated under several URLs.

    Wire stories (AP, Reuters, AFP) are republished by hundreds of outlets with
    near-identical headlines. Two articles with the same fingerprint are very
    likely ONE report copied -- which is NOT independent corroboration.
    """
    return hashlib.sha1(" ".join(key_tokens(title)).encode("utf-8")).hexdigest()[:16]
