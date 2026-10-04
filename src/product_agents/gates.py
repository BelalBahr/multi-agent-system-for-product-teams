"""Decision gates. Only a person can pass them, and every action is audited.

These functions are what the CLI calls on a person's behalf. No agent holds a handle that can
reach them: agents only receive a ScopedStore, which has no approve or reject methods.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .policy import Policy
from .store import Store


class GateError(Exception):
    """A gate refused the action. The message says why."""


def _actor(by: str) -> str:
    if not by.strip():
        raise GateError("say who is approving (--by NAME)")
    return f"human:{by.strip()}"


def approve_decision(
    store: Store,
    decision_id: str,
    by: str,
    policy: Policy,
    assumption: bool = False,
    waive_redteam: bool = False,
    now: datetime | None = None,
) -> dict:
    actor = _actor(by)
    d = store.get_decision(decision_id)
    if d is None:
        raise GateError(f"no decision {decision_id}")
    if d["status"] != "draft":
        raise GateError(f"decision is already {d['status']}")
    has_dissent = bool(store.get_dissents(decision_id))
    if policy.require_dissent and not has_dissent and not waive_redteam:
        raise GateError(
            "the Red Team has not reviewed this decision. Run `product-agents redteam`, "
            "or pass --waive-redteam to proceed without it (the waiver is logged)."
        )
    now = now or datetime.now(timezone.utc)
    review_date = (now + timedelta(days=d["review_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        store.decision_set_approved(
            decision_id, by.strip(), review_date, assumption, waive_redteam and not has_dissent
        )
    except sqlite3.DatabaseError as exc:
        raise GateError(
            f"{exc}. This decision cites no evidence. Pass --assumption to approve it as an "
            "explicit assumption."
        ) from exc
    store.log(actor, "approve", "decisions", decision_id, d["title"])
    if waive_redteam and not has_dissent:
        store.log(actor, "waive_redteam", "decisions", decision_id, "approved without Red Team review")
    return store.get_decision(decision_id)  # type: ignore[return-value]


def reject_decision(store: Store, decision_id: str, by: str, reason: str) -> None:
    actor = _actor(by)
    d = store.get_decision(decision_id)
    if d is None:
        raise GateError(f"no decision {decision_id}")
    if d["status"] != "draft":
        raise GateError(f"decision is already {d['status']}")
    if not reason.strip():
        raise GateError("give a reason (--reason TEXT): it is kept with the decision")
    store.decision_set_rejected(decision_id, by.strip(), reason.strip())
    store.log(actor, "reject", "decisions", decision_id, reason.strip())


def approve_spec(store: Store, spec_id: str, by: str) -> None:
    actor = _actor(by)
    spec = store.get_spec(spec_id)
    if spec is None:
        raise GateError(f"no spec {spec_id}")
    if spec["status"] != "draft":
        raise GateError(f"spec is already {spec['status']}")
    store.spec_set_approved(spec_id, by.strip())
    store.log(actor, "approve", "specs", spec_id, spec["title"])


def export_spec(store: Store, spec_id: str, writer, by: str) -> str:
    """Create a draft task in the team's drafts list. A person promotes it from there."""
    actor = _actor(by)
    spec = store.get_spec(spec_id)
    if spec is None:
        raise GateError(f"no spec {spec_id}")
    if spec["status"] != "approved":
        raise GateError(f"only an approved spec can be exported (this one is {spec['status']})")
    url = writer.create_draft(spec["title"], spec["body_md"])
    store.spec_set_exported(spec_id, url)
    store.log(actor, "export", "specs", spec_id, url)
    return url


def render_decision(store: Store, decision_id: str) -> str:
    d = store.get_decision(decision_id)
    if d is None:
        raise GateError(f"no decision {decision_id}")
    theme = store.get_theme(d["theme_id"])
    lines = [f"# Decision {d['id']}: {d['title']}", ""]
    lines.append(f"Status: {d['status']}   Theme: {theme.title if theme else d['theme_id']}")
    if d["assumption"]:
        lines.append("WARNING: this decision cites no evidence and is an assumption.")
    lines += ["", f"## Recommendation: {d['recommended']}", "", d["rationale"], ""]
    if d["strategy_fit"]:
        lines += [f"Strategy fit: {d['strategy_fit']}", ""]
    lines.append("## Options")
    for o in d["options"]:
        mark = " (recommended)" if str(o.get("name", "")).casefold() == d["recommended"].casefold() else ""
        lines.append(f"- **{o.get('name')}**{mark}: {o.get('summary', '')} Effort: {o.get('effort', '?')}")
        for pro in o.get("pros") or []:
            lines.append(f"    + {pro}")
        for con in o.get("cons") or []:
            lines.append(f"    - {con}")
    lines += ["", "## Hypothesis", d["hypothesis"] or "(none stated)"]
    if d["metric_event"]:
        lines.append(
            f"Metric: {d['metric_event']} should {d['metric_direction']} by {d['threshold_pct']}% "
            f"(compared over {d['window_days']} days), reviewed after {d['review_days']} days."
        )
    else:
        lines.append("Metric: none defined, so the outcome cannot be checked automatically.")
    lines += ["", "## Evidence cited"]
    cited = store.get_evidence(d["evidence_ids"])
    if not cited:
        lines.append("(none)")
    for e in cited:
        snippet = " ".join(e.text.split())[:160]
        lines.append(f"- [{e.id}] ({e.source_type}) {snippet}  <{e.source_url}>")
    lines += ["", "## Red Team dissent"]
    dissents = store.get_dissents(decision_id)
    if not dissents:
        lines.append("Not reviewed yet.")
    for ds in dissents:
        b = ds["body"]
        lines += [f"Verdict: {b['verdict']}", f"Strongest objection: {b['strongest_objection']}"]
        for o in b["objections"]:
            ids = f" [{', '.join(o['evidence_ids'])}]" if o["evidence_ids"] else ""
            lines.append(f"- {o['claim']}: {o['why']}{ids}")
        if b["premortem"]:
            lines.append(f"Pre-mortem: {b['premortem']}")
        for a in b["hidden_assumptions"]:
            lines.append(f"- Hidden assumption: {a}")
        if b["ignored_evidence_ids"]:
            lines.append(f"Evidence the decision did not use: {', '.join(b['ignored_evidence_ids'])}")
    if d["status"] == "approved":
        lines += ["", f"Approved by {d['approved_by']} at {d['approved_at']}, review on {d['review_date']}."]
        if d["redteam_waived"]:
            lines.append("The Red Team review was waived.")
    return "\n".join(lines) + "\n"


def approve_update(store: Store, update_id: str, by: str) -> None:
    actor = _actor(by)
    u = store.get_update(update_id)
    if u is None:
        raise GateError(f"no update {update_id}")
    if u["status"] != "draft":
        raise GateError(f"update is already {u['status']}")
    store.update_set_approved(update_id, by.strip())
    store.log(actor, "approve", "updates", update_id, u["title"])


def export_update(store: Store, update_id: str, writer, by: str) -> str:
    """Create a draft task from an approved update. Nothing is sent to any audience."""
    actor = _actor(by)
    u = store.get_update(update_id)
    if u is None:
        raise GateError(f"no update {update_id}")
    if u["status"] != "approved":
        raise GateError(f"only an approved update can be exported (this one is {u['status']})")
    label = f"[{u['audience']}] " if u["audience"] else ""
    url = writer.create_draft(label + u["title"], u["body_md"])
    store.update_set_exported(update_id, url)
    store.log(actor, "export", "updates", update_id, url)
    return url
