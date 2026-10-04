"""Analyst: backs the top themes with product-analytics and behavior numbers.

The model only chooses WHICH dictionary events and pages are relevant to a theme. It never
sees or reports the numbers. Those come straight from the connectors and are stored as
metric Evidence, so they cannot be made up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..dictionary import Dictionary
from ..llm import LLMProvider
from ..models import Evidence, make_id
from ..policy import ScopedStore
from .common import call_json, escape, system_prompt

CAVEAT = "Association only. This does not show cause."


@dataclass
class AnalystResult:
    themes_covered: int = 0
    metrics_added: int = 0
    skipped: list[str] = field(default_factory=list)


class Analyst:
    def __init__(
        self,
        store: ScopedStore,
        llm: LLMProvider,
        dictionary: Dictionary,
        mixpanel=None,
        clarity=None,
        top_n: int = 5,
        window_days: int = 7,
        now: datetime | None = None,
    ):
        self.store, self.llm, self.dictionary = store, llm, dictionary
        self.mixpanel, self.clarity = mixpanel, clarity
        for conn in (mixpanel, clarity):
            if conn is not None:
                store.require_connector(conn.id)
        self.top_n, self.window_days = top_n, window_days
        self.now = now or datetime.now(timezone.utc)
        self.system = system_prompt(store.agent)

    def _choose(self, title: str, summary: str, quotes: list[str]) -> dict:
        prompt = "\n".join(
            [
                f"Theme: {escape(title)}",
                f"Summary: {escape(summary)}",
                "Example customer quotes:",
                *[f"<evidence>{escape(q)}</evidence>" for q in quotes[:3]],
                "",
                self.dictionary.describe(),
                "",
                'Pick up to 2 events and up to 2 pages that would show whether this problem is real '
                'and how big it is. Use ONLY names from the lists above. Return JSON: '
                '{"events": ["..."], "pages": ["..."]}',
            ]
        )
        obj = call_json(self.llm, self.system, prompt, lambda o: True)
        obj = obj or {}
        events = [e for e in obj.get("events") or [] if e in self.dictionary.events][:2]
        pages = [p for p in obj.get("pages") or [] if p in self.dictionary.pages][:2]
        return {"events": events if self.mixpanel else [], "pages": pages if self.clarity else []}

    def run(self) -> AnalystResult:
        result = AnalystResult()
        if self.mixpanel is None and self.clarity is None:
            result.skipped.append("no analytics connector configured")
            return result
        w = self.window_days
        today = self.now.date()
        cur_start, cur_end = today - timedelta(days=w - 1), today
        prev_start, prev_end = cur_start - timedelta(days=w), cur_start - timedelta(days=1)
        stats = [s for s in self.store.theme_stats(self.now, w) if s.current > 0]
        stats.sort(key=lambda s: (s.current, s.trend), reverse=True)
        stamp = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        for s in stats[: self.top_n]:
            theme = s.theme
            quotes = [q for q, _ in self.store.theme_quotes(theme.id)]
            picks = self._choose(theme.title, theme.summary, quotes)
            added_here = 0
            for event in picks["events"]:
                cur = self.mixpanel.event_count(event, cur_start, cur_end)
                prev = self.mixpanel.event_count(event, prev_start, prev_end)
                change = f"{(cur - prev) / prev * 100:+.0f}%" if prev else "no baseline"
                ev = Evidence(
                    id=make_id("mixpanel", event, str(cur_end), theme.id),
                    source_type="mixpanel",
                    source_id=f"{event}:{cur_end}",
                    source_url=f"mixpanel://event/{event}?from={cur_start}&to={cur_end}",
                    timestamp=stamp,
                    segment="",
                    text=(
                        f"Mixpanel event '{event}': {cur} in the last {w} days versus {prev} in the "
                        f"{w} days before ({change}). {CAVEAT}"
                    ),
                    metric={"event": event, "current": cur, "previous": prev, "window_days": w},
                    kind="metric",
                )
                if self.store.add_metric_evidence(ev):
                    self.store.attach_to_theme(ev.id, theme.id)
                    result.metrics_added += 1
                    added_here += 1
            for page in picks["pages"]:
                data = self.clarity.page_friction(page, days=3)
                if not data:
                    result.skipped.append(f"clarity: no data for pages containing '{page}'")
                    continue
                signals = ", ".join(f"{k} {v}% of sessions" for k, v in data.items() if k != "sessions")
                ev = Evidence(
                    id=make_id("clarity", page, str(cur_end), theme.id),
                    source_type="clarity",
                    source_id=f"{page}:{cur_end}",
                    source_url=f"clarity://pages?contains={page}",
                    timestamp=stamp,
                    segment="",
                    text=(
                        f"Clarity, pages containing '{page}', last 3 days ({int(data['sessions'])} "
                        f"sessions): {signals}. {CAVEAT}"
                    ),
                    metric={"page": page, **data},
                    kind="metric",
                )
                if self.store.add_metric_evidence(ev):
                    self.store.attach_to_theme(ev.id, theme.id)
                    result.metrics_added += 1
                    added_here += 1
            if added_here:
                result.themes_covered += 1
            else:
                result.skipped.append(f"{theme.title}: nothing in the dictionary applied")
        return result
