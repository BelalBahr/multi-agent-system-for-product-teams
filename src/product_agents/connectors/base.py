"""The connector interface.

A connector declares an id, a category and its scopes, fetches normalized
records since a point in time, and redacts text before it is stored. There is
deliberately no scope for modifying existing items in a source system.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from ..models import RawRecord
from ..redact import redact_text

VALID_SCOPES = ("read", "draft-write")


class Connector(Protocol):
    id: str
    category: str  # support | product-analytics | behavior-analytics | voice | ...
    scopes: tuple[str, ...]

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        """Yield records newer than `since` (ISO 8601 UTC), oldest first."""
        ...

    def redact(self, text: str) -> str:
        """Strip personal data. Runs before anything reaches the store."""
        ...


class BaseConnector:
    """Convenience base: default redaction, scope validation."""

    id = "base"
    category = "unknown"
    scopes: tuple[str, ...] = ("read",)

    def __init__(self) -> None:
        for scope in self.scopes:
            if scope not in VALID_SCOPES:
                raise ValueError(f"unknown scope '{scope}'")

    def redact(self, text: str) -> str:
        return redact_text(text)

    def fetch(self, since: str | None) -> Iterator[RawRecord]:  # pragma: no cover
        raise NotImplementedError
