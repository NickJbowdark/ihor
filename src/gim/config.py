"""Central configuration.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Secrets such as API keys must NEVER be hard-coded in source
files -- keep them in ``.env``, which is git-ignored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# src/gim/config.py -> parents[2] is the project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_dir: Path
    log_level: str
    gdelt_min_interval_s: float
    http_timeout_s: float
    user_agent: str
    gdelt_language: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        db_path=_path("GIM_DB_PATH", "data/gim.sqlite3"),
        log_dir=_path("GIM_LOG_DIR", "logs"),
        log_level=os.getenv("GIM_LOG_LEVEL", "INFO").upper(),
        # Never allow faster than GDELT's documented ~1 request / 5 s.
        gdelt_min_interval_s=max(5.0, _float("GIM_GDELT_MIN_INTERVAL_S", 6.0)),
        http_timeout_s=_float("GIM_HTTP_TIMEOUT_S", 30.0),
        user_agent=os.getenv(
            "GIM_USER_AGENT", "GIM-Research-Monitor/0.1 (personal OSINT research project)"
        ),
        gdelt_language=os.getenv("GIM_GDELT_LANGUAGE", "english").strip().lower(),
    )
