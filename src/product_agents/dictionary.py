"""The team's event and page dictionary.

The Analyst and Strategist may only use events and pages listed here, so a model can never
make up a metric. The PM maintains this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Dictionary:
    events: dict[str, str]  # name -> description
    pages: dict[str, str]  # url fragment -> description

    def describe(self) -> str:
        lines = ["Events you may reference:"]
        lines += [f"- {k}: {v}" for k, v in self.events.items()] or ["- (none)"]
        lines.append("Pages you may reference (URL contains):")
        lines += [f"- {k}: {v}" for k, v in self.pages.items()] or ["- (none)"]
        return "\n".join(lines)


def load_dictionary(path: str | Path | None) -> Dictionary:
    if path is None or not Path(path).exists():
        return Dictionary({}, {})
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    events = {str(e["name"]): str(e.get("description", "")) for e in raw.get("events") or []}
    pages = {str(p["url_contains"]): str(p.get("description", "")) for p in raw.get("pages") or []}
    return Dictionary(events, pages)
