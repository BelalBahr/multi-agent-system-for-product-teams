"""Record types shared by connectors, the store and agents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def make_id(*parts: str) -> str:
    """Deterministic short id from the given parts."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RawRecord:
    """What a connector returns. Text is not yet redacted."""

    source_id: str
    source_url: str
    timestamp: str  # ISO 8601, UTC
    text: str
    segment: str = ""
    metric: dict | None = None


@dataclass(frozen=True)
class Evidence:
    """One verifiable observation. Immutable once stored."""

    id: str
    source_type: str
    source_id: str
    source_url: str
    timestamp: str
    segment: str
    text: str  # always redacted
    metric: dict | None = None
    kind: str = "text"  # "text" (customer words) or "metric" (an analytics observation)


@dataclass(frozen=True)
class Theme:
    id: str
    title: str
    summary: str
    confidence: str  # low | medium | high
    created_at: str
    updated_at: str
