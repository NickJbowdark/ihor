"""Structured logging: human-readable on the console, JSON lines in a file.

JSON logs are easy to search later (and to ship to a monitoring tool once the
platform runs on a server).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone

from gim.config import get_settings
from gim.security import redact

_CONFIGURED = False
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class RedactingFormatter(logging.Formatter):
    """Console formatter that strips credentials from every message."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D102  (output is redacted below)
        return redact(self._format(record))

    def _format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via logger.info("...", extra={...}) is kept as a field.
        for key, value in vars(record).items():
            if key not in _STD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(settings.log_level)

    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "gim.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    # Keep third-party chatter down.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _CONFIGURED = True
