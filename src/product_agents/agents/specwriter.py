"""Spec Writer: drafts a spec from an APPROVED Decision.

Every requirement cites evidence ids it was shown, or is labeled an assumption in the output.
The model cannot hide an unsupported requirement: if none of its cited ids is valid, the
requirement is forced to an assumption.
"""

from __future__ import annotations

from ..llm import LLMProvider
from ..models import Evidence, make_id
from ..policy import ScopedStore
from .common import call_json, escape, evidence_tag, system_prompt

OUTPUT_SPEC = """\
Return JSON in exactly this shape:
{"title": "<spec title>",
 "problem": "<the problem, in the customers' terms>",
 "goals": ["..."],
 "non_goals": ["..."],
 "requirements": [{"text": "<one testable requirement>",
                   "evidence_ids": ["<ids that justify it>"],
                   "assumption": <true if you have no evidence for it>}],
 "acceptance_criteria": ["<observable, testable>"],
 "open_questions": ["..."]}
Stay within the approved decision. Do not add scope. Mark anything you cannot ground in the
evidence as an assumption.
"""


def _valid(obj: dict) -> bool:
    return bool(obj.get("title") and obj.get("problem") and isinstance(obj.get("requirements"), list)
                and obj["requirements"])


def render_spec(body: dict, evidence: dict[str, Evidence], decision: dict) -> str:
    lines = [f"# {body['title']}", "", f"Source decision: {decision['title']} (approved by {decision['approved_by']})", ""]
    lines += ["## Problem", body["problem"], ""]
    for heading, key in (("Goals", "goals"), ("Non-goals", "non_goals")):
        if body.get(key):
            lines += [f"## {heading}", *[f"- {x}" for x in body[key]], ""]
    lines.append("## Requirements")
    used: list[str] = []
    for i, r in enumerate(body["requirements"], 1):
        if r["assumption"]:
            lines.append(f"{i}. {r['text']} *(assumption: no supporting evidence)*")
        else:
            refs = ", ".join(f"[{e}]" for e in r["evidence_ids"])
            lines.append(f"{i}. {r['text']} {refs}")
            used += [e for e in r["evidence_ids"] if e not in used]
    lines.append("")
    if body.get("acceptance_criteria"):
        lines += ["## Acceptance criteria", *[f"- {x}" for x in body["acceptance_criteria"]], ""]
    if body.get("open_questions"):
        lines += ["## Open questions", *[f"- {x}" for x in body["open_questions"]], ""]
    if used:
        lines.append("## Evidence")
        for e in used:
            ev = evidence[e]
            snippet = " ".join(ev.text.split())[:140]
            lines.append(f"- [{e}] {snippet} <{ev.source_url}>")
    return "\n".join(lines).rstrip() + "\n"


class SpecWriter:
    def __init__(self, store: ScopedStore, llm: LLMProvider):
        self.store, self.llm = store, llm
        self.system = system_prompt(store.agent)

    def write(self, decision_id: str) -> str | None:
        d = self.store.get_decision(decision_id)
        if d is None:
            raise ValueError(f"unknown decision {decision_id}")
        if d["status"] != "approved":
            raise ValueError("a spec can only be written for an approved decision")
        theme_items = self.store.theme_evidence(d["theme_id"])
        by_id = {e.id: e for e in theme_items}
        cited = [by_id[i] for i in d["evidence_ids"] if i in by_id]
        extra = [e for e in theme_items if e.kind == "text" and e.id not in d["evidence_ids"]][:6]
        shown = cited + extra
        shown_ids = {e.id for e in shown}
        prompt = "\n".join(
            [
                "Strategy:",
                escape(self.store.get_strategy().strip() or "(none written)"),
                "",
                "Approved decision:",
                f"Title: {escape(d['title'])}",
                f"Chosen option: {escape(d['recommended'])}",
                f"Rationale: {escape(d['rationale'])}",
                f"Hypothesis: {escape(d['hypothesis'])}",
                "",
                "Evidence:",
                *[evidence_tag(e) for e in shown],
                "",
                OUTPUT_SPEC,
            ]
        )
        obj = call_json(self.llm, self.system, prompt, _valid, max_tokens=6000)
        if obj is None:
            self.store.note("parse_failure", f"spec writer: decision {decision_id}")
            return None
        reqs = []
        for r in obj["requirements"]:
            if not isinstance(r, dict) or not r.get("text"):
                continue
            ids = [i for i in r.get("evidence_ids") or [] if i in shown_ids]
            reqs.append(
                {"text": str(r["text"]), "evidence_ids": ids, "assumption": bool(r.get("assumption")) or not ids}
            )
        if not reqs:
            self.store.note("parse_failure", f"spec writer: no usable requirements for {decision_id}")
            return None
        body = {
            "title": str(obj["title"]).strip(),
            "problem": str(obj["problem"]).strip(),
            "goals": [str(x) for x in obj.get("goals") or []],
            "non_goals": [str(x) for x in obj.get("non_goals") or []],
            "requirements": reqs,
            "acceptance_criteria": [str(x) for x in obj.get("acceptance_criteria") or []],
            "open_questions": [str(x) for x in obj.get("open_questions") or []],
        }
        md = render_spec(body, by_id | {e.id: e for e in shown}, d)
        spec_id = make_id("spec", decision_id, self.store.run_id)
        self.store.create_spec(spec_id, decision_id, body["title"], body, md)
        return spec_id

    def run(self, decision_id: str | None = None) -> list[str]:
        if decision_id:
            targets = [decision_id]
        else:
            targets = [
                d["id"] for d in self.store.list_decisions("approved")
                if self.store.spec_for_decision(d["id"]) is None
            ]
        return [s for s in (self.write(t) for t in targets) if s]
