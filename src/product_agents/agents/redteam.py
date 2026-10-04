"""Red Team: argues against a draft Decision before it reaches a person.

Tier C: it can only attach dissent. It cannot edit the decision or approve anything.
The dissent is stored separately, so the Strategist cannot overwrite or soften it.
"""

from __future__ import annotations

from ..llm import LLMProvider
from ..models import Evidence, make_id
from ..policy import ScopedStore
from .common import call_json, escape, evidence_tag, system_prompt

OUTPUT_SPEC = """\
Return JSON in exactly this shape:
{"strongest_objection": "<the single best reason not to do this, or to do it differently>",
 "objections": [{"claim": "<what the decision assumes or gets wrong>",
                 "why": "<why that is a problem>",
                 "evidence_ids": ["<ids from the evidence list, if any>"]}],
 "premortem": "<imagine this shipped and failed: the most likely reason>",
 "hidden_assumptions": ["..."],
 "ignored_evidence_ids": ["<ids of evidence the decision did not use that point the other way>"],
 "verdict": "proceed|proceed with changes|reconsider"}
Be specific and fair. Do not invent evidence. If the case is strong, say so, and still name the
best remaining risk.
"""


def _valid(obj: dict) -> bool:
    return bool(obj.get("strongest_objection")) and obj.get("verdict") in (
        "proceed", "proceed with changes", "reconsider",
    )


class RedTeam:
    def __init__(self, store: ScopedStore, llm: LLMProvider):
        self.store, self.llm = store, llm
        self.system = system_prompt(store.agent)

    def _context(self, decision: dict) -> list[Evidence]:
        items = self.store.theme_evidence(decision["theme_id"])
        cited = set(decision["evidence_ids"])
        cited_ev = [e for e in items if e.id in cited]
        other = [e for e in items if e.id not in cited]
        return cited_ev + other[:10]

    def review(self, decision_id: str) -> bool:
        d = self.store.get_decision(decision_id)
        if d is None:
            raise ValueError(f"unknown decision {decision_id}")
        evidence = self._context(d)
        shown = {e.id for e in evidence}
        prompt = "\n".join(
            [
                "Strategy:",
                escape(self.store.get_strategy().strip() or "(none written)"),
                "",
                "Draft decision to challenge:",
                f"Title: {escape(d['title'])}",
                f"Recommended option: {escape(d['recommended'])}",
                f"Rationale: {escape(d['rationale'])}",
                f"Hypothesis: {escape(d['hypothesis'])}",
                f"Marked as an assumption (no supporting evidence): {bool(d['assumption'])}",
                "Options considered: " + "; ".join(escape(str(o.get("name", ""))) for o in d["options"]),
                "",
                "Evidence available (cited ones first):",
                *[evidence_tag(e) for e in evidence],
                "",
                OUTPUT_SPEC,
            ]
        )
        obj = call_json(self.llm, self.system, prompt, _valid)
        if obj is None:
            self.store.note("parse_failure", f"red team: decision {decision_id}")
            return False
        objections = []
        for o in obj.get("objections") or []:
            if isinstance(o, dict) and o.get("claim"):
                objections.append(
                    {
                        "claim": str(o["claim"]),
                        "why": str(o.get("why") or ""),
                        "evidence_ids": [i for i in o.get("evidence_ids") or [] if i in shown],
                    }
                )
        body = {
            "strongest_objection": str(obj["strongest_objection"]),
            "objections": objections,
            "premortem": str(obj.get("premortem") or ""),
            "hidden_assumptions": [str(a) for a in obj.get("hidden_assumptions") or []],
            "ignored_evidence_ids": [i for i in obj.get("ignored_evidence_ids") or [] if i in shown],
            "verdict": obj["verdict"],
        }
        self.store.add_dissent(make_id("dissent", decision_id, self.store.run_id), decision_id, body)
        return True

    def run(self, decision_id: str | None = None) -> int:
        targets = (
            [decision_id]
            if decision_id
            else [d["id"] for d in self.store.list_decisions("draft") if not self.store.get_dissents(d["id"])]
        )
        return sum(1 for t in targets if self.review(t))
