"""Source registry: loads and validates ``config/sources.toml``.

The registry is the *static* description of every source (what, who, how,
how often, under which licence). Operational state lives in the database.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from gim.config import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "config" / "sources.toml"

SOURCE_CATEGORIES = {
    "news_discovery", "international_media", "official_eu", "official_un", "official_nato",
    "official_national", "conflict_dataset", "humanitarian", "economic", "sanctions", "geospatial",
}
DOC_TYPES = {
    "original_reporting",   # an outlet's own journalism
    "syndicated",           # reprint of another organisation's report (e.g. wire copy)
    "official_statement",   # communication by a government / international organisation
    "dataset_record",       # row from a structured dataset (ACLED, UCDP, World Bank...)
    "analysis_opinion",     # think-tank analysis, op-eds, assessments
    "unknown",
}
CONNECTORS = {"gdelt", "rss", "reliefweb", "planned"}
AUTH_TYPES = {"none", "appname", "token", "oauth"}
_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    organization: str
    category: str
    doc_type: str
    connector: str
    url: str
    homepage: str
    coverage: str
    languages: tuple[str, ...]
    default_regions: tuple[str, ...]
    auth: str
    auth_env: tuple[str, ...]
    licence: str
    usage_notes: str
    poll_minutes: int
    enabled: bool
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def implemented(self) -> bool:
        return self.connector != "planned"

    def missing_credentials(self) -> list[str]:
        """Names of required .env variables that are not set (values are never returned)."""
        return [name for name in self.auth_env if not os.getenv(name, "").strip()]


class RegistryError(ValueError):
    pass


_KNOWN_KEYS = {f for f in SourceSpec.__dataclass_fields__ if f != "extra"}


def _validate(raw: dict, index: int) -> SourceSpec:
    where = f"source #{index + 1} ({raw.get('id', '?')})"
    for key in ("id", "name", "organization", "category", "doc_type", "connector"):
        if not raw.get(key):
            raise RegistryError(f"{where}: missing '{key}'")
    if not _ID_RE.match(raw["id"]):
        raise RegistryError(f"{where}: id must be lowercase letters/digits/underscore")
    checks = [("category", SOURCE_CATEGORIES), ("doc_type", DOC_TYPES),
              ("connector", CONNECTORS), ("auth", AUTH_TYPES)]
    for key, allowed in checks:
        value = raw.get(key, "none")
        if value not in allowed:
            raise RegistryError(f"{where}: {key}={value!r} not in {sorted(allowed)}")
    url = raw.get("url", "")
    if raw["connector"] != "planned" and not url.startswith("https://"):
        raise RegistryError(f"{where}: implemented connectors need an https:// url")
    poll = int(raw.get("poll_minutes", 60))
    if raw["connector"] != "planned" and poll < 5:
        raise RegistryError(f"{where}: poll_minutes must be >= 5 (be polite to sources)")
    return SourceSpec(
        id=raw["id"], name=raw["name"], organization=raw["organization"],
        category=raw["category"], doc_type=raw["doc_type"], connector=raw["connector"],
        url=url, homepage=raw.get("homepage", ""), coverage=raw.get("coverage", ""),
        languages=tuple(raw.get("languages", [])), default_regions=tuple(raw.get("default_regions", [])),
        auth=raw.get("auth", "none"), auth_env=tuple(raw.get("auth_env", [])),
        licence=raw.get("licence", ""), usage_notes=raw.get("usage_notes", ""),
        poll_minutes=poll, enabled=bool(raw.get("enabled", False)),
        extra={k: v for k, v in raw.items() if k not in _KNOWN_KEYS},
    )


def load_registry(path: Path | str = REGISTRY_PATH) -> dict[str, SourceSpec]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    specs: dict[str, SourceSpec] = {}
    for i, raw in enumerate(data.get("source", [])):
        spec = _validate(raw, i)
        if spec.id in specs:
            raise RegistryError(f"duplicate source id {spec.id!r}")
        specs[spec.id] = spec
    return specs


@lru_cache(maxsize=1)
def get_registry() -> dict[str, SourceSpec]:
    return load_registry()
