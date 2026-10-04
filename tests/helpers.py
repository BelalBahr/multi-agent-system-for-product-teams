"""Test doubles shared by the decision-chain tests."""

import json
import re
from datetime import date

from product_agents.llm import FakeProvider
from product_agents.models import Evidence
from product_agents.policy import AgentDef
from product_agents.store import Store

TAG_IDS = re.compile(r'<evidence id="([^"]+)"')
TAG_FULL = re.compile(r'<evidence id="([^"]+)"[^>]*>\n(.*?)\n</evidence>', re.DOTALL)


def agent(name, tier="B", reads=(), writes=(), connectors=(), budget=None, untrusted=True):
    return AgentDef(name, "r", tier, tuple(reads), tuple(writes), tuple(connectors), "Be precise.", budget, untrusted)


class FakeMixpanel:
    id = "mixpanel"

    def __init__(self, counts):
        # counts: callable(event, start, end) -> int, or a dict {event: (baseline, current)} unused
        self.counts = counts
        self.calls = []

    def event_count(self, event, start: date, end: date) -> int:
        self.calls.append((event, start, end))
        return self.counts(event, start, end)


class FakeClarity:
    id = "clarity"

    def __init__(self, data):
        self.data = data
        self.calls = []

    def page_friction(self, url_contains, days=3):
        self.calls.append(url_contains)
        return self.data.get(url_contains)


class FakeWriter:
    id = "clickup"

    def __init__(self):
        self.created = []

    def create_draft(self, title, markdown):
        self.created.append((title, markdown))
        return "https://app.clickup.com/t/abc123"


def seed_theme(store: Store, theme_id="t1", title="CSV export fails", n=4, day="2026-10-06"):
    store.create_theme(theme_id, title, "Exports fail on big reports.")
    for i in range(n):
        ev = Evidence(f"{theme_id}e{i}", "zendesk", f"{i}", f"http://z/{i}", f"{day}T0{i}:00:00Z", "smb",
                      f"Export failed again for report number {i}, it just says export failed")
        store.add_evidence(ev)
        store.assign_evidence(ev.id, theme_id)
    store.add_quote(theme_id, f"{theme_id}e0", "Export failed again for report number 0")
    store.set_confidence(theme_id, "medium")


def scripted(overrides=None):
    """A model that answers each agent's prompt with valid JSON, picking from the ids it sees."""
    overrides = overrides or {}

    def respond(system, user):
        ids = TAG_IDS.findall(user)
        if "Pick up to 2 events" in user:
            out = overrides.get("analyst", {"events": ["export_completed"], "pages": ["/reports/export"]})
        elif '"strongest_objection"' in user and "Draft decision to challenge" in user:
            out = overrides.get("redteam", {
                "strongest_objection": "The fix may not move completions if the real cause is timeouts.",
                "objections": [{"claim": "Assumes big reports are the cause", "why": "Only 4 reports", "evidence_ids": ids[:1] + ["made-up"]}],
                "premortem": "We ship the fix and completions do not move.",
                "hidden_assumptions": ["Users retry after failure"],
                "ignored_evidence_ids": ids[-1:] + ["not-an-id"],
                "verdict": "proceed with changes",
            })
        elif '"acceptance_criteria"' in user:
            out = overrides.get("spec", {
                "title": "Fix large CSV exports", "problem": "Exports over 50k rows fail.",
                "goals": ["Exports of 100k rows succeed"], "non_goals": ["New export formats"],
                "requirements": [
                    {"text": "Exports up to 100k rows complete", "evidence_ids": ids[:1], "assumption": False},
                    {"text": "Show progress while exporting", "evidence_ids": ["not-shown"], "assumption": False},
                ],
                "acceptance_criteria": ["A 100k row export finishes"], "open_questions": ["Which format limit?"],
            })
        elif '"recommended"' in user:
            out = overrides.get("strategist", {
                "title": "Fix large CSV exports", "options": [
                    {"name": "Fix exports", "summary": "Stream the export.", "pros": ["Removes the error"], "cons": ["Eng time"], "effort": "M"},
                    {"name": "Do nothing for now", "summary": "Wait.", "pros": [], "cons": ["Customers stay blocked"], "effort": "S"}],
                "recommended": "Fix exports", "rationale": "Four reports in a week, rising.",
                "strategy_fit": "Fits the reliability goal.", "evidence_ids": ids[:2] + ["invented"],
                "hypothesis": "Fixing it raises export completions.", "metric_event": "export_completed",
                "metric_direction": "increase", "threshold_pct": 20, "window_days": 7, "review_days": 14,
            })
        else:
            out = {"assignments": []}
        return json.dumps(out)

    return FakeProvider(respond)
