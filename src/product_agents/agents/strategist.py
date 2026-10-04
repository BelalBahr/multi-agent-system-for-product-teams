"""Strategist: turns a theme into a draft Decision with options and a recommendation.

It proposes. It never approves: a draft Decision waits for a person and for the Red Team.
Evidence ids the model cites are checked against the ids it was actually shown, and a
decision with no valid evidence is flagged as an assumption rather than passed off as grounded.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..dictionary import Dictionary
from ..llm import LLMProvider
from ..models import Evidence, make_id
from ..policy import ScopedStore
from .common import call_json, clamp_int, escape, evidence_tag, system_prompt

OUTPUT_SPEC = """\
Return JSON in exactly this shape:
{"title": "<the decision, as a short statement>",
 "options": [{"name": "<short>", "summary": "<one or two sentences>",
              "pros": ["..."], "cons": ["..."], "effort": "S|M|L"}],
 "recommended": "<the name of one option>",
 "rationale": "<why, referring to the evidence and the strategy>",
 "strategy_fit": "<how this fits or conflicts with the strategy>",
 "evidence_ids": ["<ids from the evidence list that support the recommendation>"],
 "hypothesis": "<if we do this, we expect ...>",
 "metric_event": "<one event name from the dictionary that would show it worked, or null>",
 "metric_direction": "increase|decrease",
 "threshold_pct": <percent change that would count as success>,
 "window_days": <days of data to compare, 3 to 60>,
 "review_days": <days from approval to review, 7 to 180>}
Give 2 to 4 options, and always include "do nothing for now" as one of them.
"""


def _valid(obj: dict) -> bool:
    options = obj.get("options")
    if not (obj.get("title") and obj.get("rationale") and isinstance(options, list) and len(options) >= 2):
        return False
    names = {str(o.get("name", "")).strip().casefold() for o in options if isinstance(o, dict)}
    return str(obj.get("recommended", "")).strip().casefold() in names


class Strategist:
    def __init__(
        self,
        store: ScopedStore,
        llm: LLMProvider,
        dictionary: Dictionary,
        now: datetime | None = None,
        top_n: int = 3,
        window_days: int = 7,
    ):
        self.store, self.llm, self.dictionary = store, llm, dictionary
        self.now = now or datetime.now(timezone.utc)
        self.top_n, self.window_days = top_n, window_days
        self.system = system_prompt(store.agent)

    def _evidence_for(self, theme_id: str) -> list[Evidence]:
        items = self.store.theme_evidence(theme_id)
        quoted = {ev.id for _, ev in self.store.theme_quotes(theme_id)}
        text = [e for e in items if e.kind == "text"]
        chosen = [e for e in text if e.id in quoted][:6]
        chosen += [e for e in text if e.id not in quoted][: max(0, 8 - len(chosen))]
        return chosen + [e for e in items if e.kind == "metric"]

    def propose(self, theme_id: str) -> str | None:
        theme = self.store.get_theme(theme_id)
        if theme is None:
            raise ValueError(f"unknown theme {theme_id}")
        evidence = self._evidence_for(theme_id)
        shown = {e.id for e in evidence}
        total = len([e for e in self.store.theme_evidence(theme_id) if e.kind == "text"])
        strategy = self.store.get_strategy().strip() or "(no strategy has been written yet)"
        prompt = "\n".join(
            [
                "Strategy:",
                escape(strategy),
                "",
                f"Theme: {escape(theme.title)}",
                f"Summary: {escape(theme.summary)}",
                f"Customer reports on this theme: {total}",
                "",
                self.dictionary.describe(),
                "",
                "Evidence:",
                *[evidence_tag(e) for e in evidence],
                "",
                OUTPUT_SPEC,
            ]
        )
        obj = call_json(self.llm, self.system, prompt, _valid, max_tokens=4096)
        if obj is None:
            self.store.note("parse_failure", f"strategist: theme {theme_id}")
            return None
        cited = [i for i in obj.get("evidence_ids") or [] if i in shown]
        metric_event = obj.get("metric_event")
        direction = obj.get("metric_direction")
        try:
            threshold = float(obj.get("threshold_pct"))
            threshold = threshold if threshold > 0 else None
        except (TypeError, ValueError):
            threshold = None
        decision_id = make_id("decision", theme_id, self.store.run_id)
        recommended = next(
            str(o["name"]) for o in obj["options"]
            if str(o.get("name", "")).strip().casefold() == str(obj["recommended"]).strip().casefold()
        )
        self.store.create_decision(
            {
                "id": decision_id,
                "theme_id": theme_id,
                "title": str(obj["title"]).strip(),
                "options": obj["options"],
                "recommended": recommended,
                "rationale": str(obj["rationale"]).strip(),
                "strategy_fit": str(obj.get("strategy_fit") or "").strip(),
                "evidence_ids": cited,
                "assumption": not cited,
                "hypothesis": str(obj.get("hypothesis") or "").strip(),
                "metric_event": metric_event if metric_event in self.dictionary.events else None,
                "metric_direction": direction if direction in ("increase", "decrease") else None,
                "threshold_pct": threshold,
                "window_days": clamp_int(obj.get("window_days"), 3, 60, 14),
                "review_days": clamp_int(obj.get("review_days"), 7, 180, 30),
            }
        )
        return decision_id

    def run(self, theme_id: str | None = None) -> list[str]:
        if theme_id:
            ids = [theme_id]
        else:
            stats = [s for s in self.store.theme_stats(self.now, self.window_days) if s.current > 0]
            stats.sort(key=lambda s: (s.current, s.trend), reverse=True)
            ids = [
                s.theme.id for s in stats if not self.store.theme_has_open_decision(s.theme.id)
            ][: self.top_n]
        created = []
        for tid in ids:
            decision_id = self.propose(tid)
            if decision_id:
                created.append(decision_id)
        return created
