"""Agent definitions and the policy engine.

An agent is a YAML file. The policy engine enforces it: an agent cannot read or
write a record type, or call a connector, that its definition does not list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Evidence, Theme
from .store import Store

TIERS = ("A", "B", "C")
RECORD_TYPES = {"strategy", "evidence", "themes", "decisions", "outcomes", "dissent", "specs", "updates", "research"}


class PolicyViolation(Exception):
    """An agent tried something its definition does not allow."""


class BudgetExceeded(Exception):
    """A run or the weekly cap ran out of tokens."""


@dataclass(frozen=True)
class AgentDef:
    name: str
    role: str
    tier: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    connectors: tuple[str, ...]
    prompt_text: str
    max_tokens_per_run: int | None = None
    # Capability split from the design: an agent that reads untrusted source
    # text must not hold a connector that writes outside the drafts space.
    reads_untrusted: bool = False


@dataclass(frozen=True)
class Policy:
    weekly_token_cap: int | None = None
    # Gate rules. `require_dissent`: a decision cannot be approved until the Red Team
    # has recorded its dissent (a person can waive this explicitly, and the waiver is logged).
    require_dissent: bool = True


def _tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"'{field_name}' must be a list of strings")
    return tuple(value)


def load_agent(path: str | Path) -> AgentDef:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    for key in ("name", "role", "tier"):
        if key not in raw:
            raise ValueError(f"{path}: missing '{key}'")
    tier = str(raw["tier"]).strip().upper()
    if tier not in TIERS:
        raise ValueError(f"{path}: tier must be one of {TIERS}")
    reads = _tuple(raw.get("reads"), "reads")
    writes = _tuple(raw.get("writes"), "writes")
    for rec in (*reads, *writes):
        if rec not in RECORD_TYPES:
            raise ValueError(f"{path}: unknown record type '{rec}'")
    if tier == "C" and not set(writes) <= {"dissent"}:
        raise ValueError(f"{path}: tier C agents may only write 'dissent'")
    connectors = _tuple(raw.get("connectors"), "connectors")
    reads_untrusted = bool(raw.get("reads_untrusted", False))
    if reads_untrusted and any(c.endswith(":write") or c.endswith(":draft-write") for c in connectors):
        raise ValueError(
            f"{path}: an agent that reads untrusted text cannot hold a write connector"
        )
    prompt_text = ""
    if raw.get("prompt"):
        prompt_file = (path.parent / str(raw["prompt"])).resolve()
        prompt_text = prompt_file.read_text(encoding="utf-8")
    budget = raw.get("budget") or {}
    return AgentDef(
        name=str(raw["name"]),
        role=str(raw["role"]),
        tier=tier,
        reads=reads,
        writes=writes,
        connectors=connectors,
        prompt_text=prompt_text,
        max_tokens_per_run=budget.get("max_tokens_per_run"),
        reads_untrusted=reads_untrusted,
    )


def load_policy(path: str | Path | None) -> Policy:
    if path is None or not Path(path).exists():
        return Policy()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    budgets = raw.get("budgets") or {}
    gates = raw.get("gates") or {}
    prioritize = gates.get("prioritize") or {}
    return Policy(
        weekly_token_cap=budgets.get("weekly_token_cap"),
        require_dissent=bool(prioritize.get("require_dissent", True)),
    )


@dataclass
class ScopedStore:
    """The only store handle an agent receives. Enforces its definition and audits writes."""

    store: Store
    agent: AgentDef
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def _read(self, record_type: str) -> None:
        if record_type not in self.agent.reads:
            raise PolicyViolation(f"{self.agent.name} may not read '{record_type}'")

    def _write(self, record_type: str) -> None:
        if record_type not in self.agent.writes:
            raise PolicyViolation(f"{self.agent.name} may not write '{record_type}'")

    def _log(self, action: str, record_type: str, record_id: str | None, detail: str = "") -> None:
        self.store.log(self.agent.name, action, record_type, record_id, detail, self.run_id)

    # reads
    def unassigned_evidence(self, limit: int) -> list[Evidence]:
        self._read("evidence")
        return self.store.unassigned_evidence(limit)

    def get_evidence(self, ids: list[str]) -> list[Evidence]:
        self._read("evidence")
        return self.store.get_evidence(ids)

    def list_themes(self) -> list[Theme]:
        self._read("themes")
        return self.store.list_themes()

    def get_theme(self, theme_id: str) -> Theme | None:
        self._read("themes")
        return self.store.get_theme(theme_id)

    def find_theme_by_title(self, title: str) -> Theme | None:
        self._read("themes")
        return self.store.find_theme_by_title(title)

    def theme_evidence(self, theme_id: str) -> list[Evidence]:
        self._read("themes")
        self._read("evidence")
        return self.store.theme_evidence(theme_id)

    def theme_quote_count(self, theme_id: str) -> int:
        self._read("themes")
        return len(self.store.theme_quotes(theme_id))

    # writes
    def create_theme(self, theme_id: str, title: str, summary: str) -> Theme:
        self._write("themes")
        theme = self.store.create_theme(theme_id, title, summary)
        self._log("create", "themes", theme_id, title)
        return theme

    def assign_evidence(self, evidence_id: str, theme_id: str) -> bool:
        self._write("themes")
        done = self.store.assign_evidence(evidence_id, theme_id)
        if done:
            self._log("assign", "themes", theme_id, evidence_id)
        return done

    def add_quote(self, theme_id: str, evidence_id: str, quote: str) -> None:
        self._write("themes")
        self.store.add_quote(theme_id, evidence_id, quote)
        self._log("quote", "themes", theme_id, evidence_id)

    def set_confidence(self, theme_id: str, confidence: str) -> None:
        self._write("themes")
        self.store.set_confidence(theme_id, confidence)
        self._log("confidence", "themes", theme_id, confidence)

    # strategy, decisions, dissent, specs, outcomes, metric evidence
    def require_connector(self, connector_id: str) -> None:
        if connector_id not in self.agent.connectors:
            raise PolicyViolation(f"{self.agent.name} may not use connector '{connector_id}'")

    def get_strategy(self) -> str:
        self._read("strategy")
        return self.store.get_strategy()

    def theme_stats(self, now, window_days: int):
        self._read("themes")
        self._read("evidence")
        from .digest import theme_stats

        return theme_stats(self.store, now, window_days)

    def theme_quotes(self, theme_id: str):
        self._read("themes")
        self._read("evidence")
        return self.store.theme_quotes(theme_id)

    def theme_has_open_decision(self, theme_id: str) -> bool:
        self._read("decisions")
        return self.store.theme_has_open_decision(theme_id)

    def list_decisions(self, status: str | None = None) -> list[dict]:
        self._read("decisions")
        return self.store.list_decisions(status)

    def get_decision(self, decision_id: str) -> dict | None:
        self._read("decisions")
        return self.store.get_decision(decision_id)

    def get_dissents(self, decision_id: str) -> list[dict]:
        self._read("decisions")
        return self.store.get_dissents(decision_id)

    def evidence_exists(self, ids: list[str]) -> set[str]:
        self._read("evidence")
        return {e.id for e in self.store.get_evidence(ids)}

    def add_metric_evidence(self, ev: Evidence) -> bool:
        self._write("evidence")
        added = self.store.add_evidence(ev)
        if added:
            self._log("add", "evidence", ev.id, ev.source_type)
        return added

    def attach_to_theme(self, evidence_id: str, theme_id: str) -> None:
        self._write("evidence")
        self.store.assign_evidence(evidence_id, theme_id)

    def create_decision(self, d: dict) -> None:
        self._write("decisions")
        self.store.create_decision(d)
        self._log("create", "decisions", d["id"], d["title"])

    def add_dissent(self, dissent_id: str, decision_id: str, body: dict) -> None:
        self._write("dissent")
        self.store.add_dissent(dissent_id, decision_id, self.run_id, body)
        self._log("create", "dissent", dissent_id, decision_id)

    def spec_for_decision(self, decision_id: str) -> dict | None:
        self._read("specs")
        return self.store.spec_for_decision(decision_id)

    def create_spec(self, spec_id: str, decision_id: str, title: str, body: dict, md: str) -> None:
        self._write("specs")
        self.store.create_spec(spec_id, decision_id, title, body, md)
        self._log("create", "specs", spec_id, decision_id)

    def exported_specs(self) -> list[dict]:
        self._read("specs")
        return [x for x in self.store.list_specs() if x["exported_url"]]

    def unresearched_evidence(self, source_types: tuple[str, ...], limit: int = 50):
        self._read("evidence")
        return self.store.unresearched_evidence(source_types, limit)

    def create_research_note(self, n: dict) -> None:
        self._write("research")
        self.store.create_research_note(n)
        self._log("create", "research", n["id"], n["kind"])

    def create_update(self, u: dict) -> None:
        self._write("updates")
        self.store.create_update(u)
        self._log("create", "updates", u["id"], u["title"])

    def get_update(self, update_id: str) -> dict | None:
        self._read("updates")
        return self.store.get_update(update_id)

    def list_updates(self, kind: str | None = None) -> list[dict]:
        self._read("updates")
        return self.store.list_updates(kind)

    def list_outcomes(self) -> list[dict]:
        self._read("outcomes")
        return self.store.list_outcomes()

    def outcome_for_decision(self, decision_id: str) -> dict | None:
        self._read("outcomes")
        return self.store.outcome_for_decision(decision_id)

    def add_outcome(self, o: dict) -> None:
        self._write("outcomes")
        self.store.add_outcome(o)
        self._log("create", "outcomes", o["id"], o["verdict"])

    def note(self, action: str, detail: str) -> None:
        """Audit an event that is not a record write (for example a rejected quote)."""
        self._log(action, "run", None, detail)
