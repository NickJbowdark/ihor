"""Secret redaction for logs, error messages, reports and recorded fixtures.

Why this exists: some APIs take credentials as URL parameters (ReliefWeb's
``appname``), and libraries like ``requests`` put the full URL into exception
messages. Without redaction, a network error would copy your credential into
the log file, the database (``last_error``) and the validation report.

Two layers:
  1. exact values of every credential environment variable are replaced;
  2. generic patterns (``token=...``, ``Authorization: Bearer ...``) catch the rest.
"""
from __future__ import annotations

import os
import re

REDACTED = "***REDACTED***"
_SECRET_NAME = re.compile(r"^GIM_.*(KEY|TOKEN|SECRET|PASSWORD|APPNAME|APP_IDENTIFIER|EMAIL|CLIENT_ID)", re.I)
_PATTERNS = [
    re.compile(r"(?i)\b(appname|app_identifier|api[_-]?key|access[_-]?token|token|key|password|secret|client_secret)"
               r"(=|%3D)([^&\s'\"]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+|bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    re.compile(r"(?i)(x-ucdp-access-token['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)"),
]


def secret_values() -> list[str]:
    """Values of credential-like GIM_* environment variables (never logged themselves)."""
    from gim.sources.registry import get_registry  # local import avoids a cycle at startup

    names = {n for n in os.environ if _SECRET_NAME.match(n)}
    try:
        names |= {n for s in get_registry().values() for n in s.auth_env}
    except Exception:  # registry broken -> still redact what we can
        pass
    values = [os.environ.get(n, "").strip() for n in names]
    return sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    for value in secret_values():
        text = text.replace(value, REDACTED)
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _PATTERNS[2].sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return text


def redact_bytes(data: bytes) -> bytes:
    """Redact exact secret values inside raw response bodies before saving them."""
    for value in secret_values():
        data = data.replace(value.encode("utf-8"), REDACTED.encode("utf-8"))
    return data
