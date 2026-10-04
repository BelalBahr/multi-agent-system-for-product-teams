"""Outcome Tracker: checks each decision's hypothesis against the real number.

Deterministic on purpose. It uses no model: it compares one dictionary event, over the same
number of days, before the decision was approved and around its review date, and states a
verdict plainly. When the data cannot support a verdict, it says so instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models import make_id
from ..policy import ScopedStore

MIN_BASELINE = 30  # fewer events than this in the baseline window is too noisy to judge


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class OutcomeTracker:
    def __init__(self, store: ScopedStore, mixpanel, now: datetime | None = None):
        store.require_connector(mixpanel.id)
        self.store, self.mixpanel = store, mixpanel
        self.now = now or datetime.now(timezone.utc)

    def evaluate(self, d: dict) -> dict:
        base = {"id": make_id("outcome", d["id"]), "decision_id": d["id"]}
        if not d["metric_event"] or not d["metric_direction"] or not d["threshold_pct"]:
            return {**base, "verdict": "no metric", "detail": "The decision did not define a metric, direction and threshold."}
        w = d["window_days"]
        approved = _dt(d["approved_at"]).date()
        review = _dt(d["review_date"]).date()
        b_end = approved - timedelta(days=1)
        b_start = b_end - timedelta(days=w - 1)
        c_end = min(review, self.now.date())
        c_start = c_end - timedelta(days=w - 1)
        baseline = self.mixpanel.event_count(d["metric_event"], b_start, b_end)
        current = self.mixpanel.event_count(d["metric_event"], c_start, c_end)
        window = f"baseline {b_start} to {b_end}, current {c_start} to {c_end}"
        if baseline < MIN_BASELINE:
            return {**base, "baseline": baseline, "current": current, "verdict": "inconclusive",
                    "detail": f"Baseline of {baseline} events is below the {MIN_BASELINE} needed to judge. {window}."}
        change = (current - baseline) / baseline * 100
        met = change >= d["threshold_pct"] if d["metric_direction"] == "increase" else change <= -d["threshold_pct"]
        return {**base, "baseline": baseline, "current": current, "change_pct": round(change, 1),
                "verdict": "met" if met else "not met",
                "detail": (f"'{d['metric_event']}' went from {baseline} to {current} ({change:+.1f}%). "
                           f"Target: {d['metric_direction']} by {d['threshold_pct']}%. {window}. "
                           "This is an association with the decision, not proof it caused the change.")}

    def run(self) -> list[dict]:
        done = []
        for d in self.store.list_decisions("approved"):
            if not d["review_date"] or _dt(d["review_date"]) > self.now:
                continue
            if self.store.outcome_for_decision(d["id"]):
                continue
            outcome = self.evaluate(d)
            self.store.add_outcome(outcome)
            done.append(outcome)
        return done
