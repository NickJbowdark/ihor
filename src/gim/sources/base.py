"""Common connector interface.

Every connector turns ONE registry entry into a list of ``Document`` objects.
Connectors only fetch and parse -- they never write to the database. The
collector (``gim.collect``) handles storage, health status and run logging.
That separation makes each connector testable with recorded responses.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from gim.models import Document
from gim.sources.http import PoliteHttp
from gim.sources.registry import SourceSpec


class NeedsCredentials(RuntimeError):
    """Raised when a source cannot run because .env lacks required credentials."""


class ParseError(RuntimeError):
    """The response arrived but is not in the expected format."""


@dataclass
class FetchState:
    """Small per-source memory between runs (stored in source_status)."""

    etag: str | None = None
    last_modified: str | None = None
    last_success_at: datetime | None = None


@dataclass
class FetchResult:
    documents: list[Document] = field(default_factory=list)
    not_modified: bool = False           # HTTP 304: nothing new since last run
    etag: str | None = None
    last_modified: str | None = None
    raw: list[tuple[str, bytes]] = field(default_factory=list)  # (label, body) for --record
    notes: list[str] = field(default_factory=list)              # partial failures etc.


class Connector(ABC):
    def __init__(self, spec: SourceSpec, http: PoliteHttp):
        self.spec = spec
        self.http = http

    def check_credentials(self) -> None:
        missing = self.spec.missing_credentials()
        if missing:
            raise NeedsCredentials(f"set {', '.join(missing)} in .env")

    @abstractmethod
    def fetch(self, state: FetchState) -> FetchResult: ...
