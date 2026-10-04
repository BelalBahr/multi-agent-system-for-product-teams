"""The whole loop through the real CLI and orchestrator, with stand-ins for the model and APIs.

This proves the loop and its gates work together. It does not measure how good a real model's
themes, options or specs are.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import FakeMixpanel, FakeWriter, scripted
from product_agents import cli, config
from product_agents.connectors import JsonlConnector
from product_agents.orchestrator import run_weekly
from product_agents.store import Store
from test_pipeline_oracle import DEMO, oracle

NOW = "2026-10-01T12:00:00Z"
EVENTS = "events:\n  - name: export_completed\n    description: User finished a CSV export\npages: []\n"


class Composite:
    """Oracle for the Synthesizer, scripted answers for every other agent."""

    def __init__(self):
        self.synth, self.rest = oracle(), scripted()
        self.calls = []

    def complete(self, system, user, max_tokens=4096):
        self.calls.append(user)
        return (self.synth if "Existing themes" in user else self.rest).complete(system, user, max_tokens)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    cli.main(["init", str(root)])
    (root / "events.yaml").write_text(EVENTS, encoding="utf-8")
    (root / "strategy.md").write_text("Goal: reliable reporting.", encoding="utf-8")
    llm = Composite()
    monkeypatch.setattr(cli, "_llm", lambda: llm)
    return root, str(root / "product-agents.db"), llm


def run(root, db, *argv):
    return cli.main(["--db", db, "--config", str(root), *argv])


def first_id(capsys, pattern):
    out = capsys.readouterr().out
    m = re.search(pattern, out)
    assert m, out
    return m.group(1)


def test_signal_to_spec_to_draft_to_outcome(ws, monkeypatch, capsys):
    root, db, llm = ws
    assert run(root, db, "strategy", "set", str(root / "strategy.md"), "--by", "sam") == 0
    assert run(root, db, "ingest", "jsonl", str(DEMO)) == 0
    assert run(root, db, "synthesize") == 0

    # the Strategist drafts, the Red Team dissents, and nothing is approved yet
    capsys.readouterr()
    assert run(root, db, "propose", "--now", NOW) == 0
    decision_id = first_id(capsys, r"Drafted decision (\w+)")
    store = Store(db)
    assert all(d["status"] == "draft" for d in store.list_decisions())
    assert run(root, db, "decisions", "approve", decision_id, "--by", "sam") == 4  # no Red Team review yet
    assert "Red Team" in capsys.readouterr().err
    assert run(root, db, "redteam") == 0
    assert run(root, db, "decisions", "show", decision_id) == 0
    page = capsys.readouterr().out
    assert "Red Team dissent" in page and "Strongest objection" in page

    # a person approves; only then can a spec be written
    assert run(root, db, "specwrite") == 0
    assert "No specs drafted" in capsys.readouterr().out
    assert run(root, db, "decisions", "approve", decision_id, "--by", "sam") == 0
    assert run(root, db, "specwrite") == 0
    spec_id = first_id(capsys, r"Drafted spec (\w+)")

    # export is blocked until the spec is approved, and produces only a draft
    writer = FakeWriter()
    monkeypatch.setattr(config, "clickup_from_env", lambda: writer)
    assert run(root, db, "specs", "export", spec_id, "--by", "sam") == 4
    assert writer.created == []
    assert run(root, db, "specs", "approve", spec_id, "--by", "sam") == 0
    assert run(root, db, "specs", "export", spec_id, "--by", "sam") == 0
    assert len(writer.created) == 1 and "agent-draft" not in writer.created[0][1]
    assert "draft" in capsys.readouterr().out.lower()

    # at the review date the outcome is checked against the real number
    today = datetime.now(timezone.utc).date()
    monkeypatch.setattr(config, "mixpanel_from_env", lambda: FakeMixpanel(lambda e, s, d: 50 if d < today else 120))
    assert run(root, db, "outcomes", "--now", "2027-06-01T00:00:00Z") == 0
    assert "met" in capsys.readouterr().out
    assert Store(db).list_outcomes()[0]["verdict"] == "met"

    # every step is attributable
    actors = {r["actor"] for r in Store(db).audit(500)}
    assert {"human:sam", "strategist", "red-team", "spec-writer", "outcome-tracker", "signal-synthesizer"} <= actors


def test_rejecting_requires_a_reason_and_blocks_a_spec(ws, capsys):
    root, db, _ = ws
    run(root, db, "ingest", "jsonl", str(DEMO))
    run(root, db, "synthesize")
    capsys.readouterr()
    run(root, db, "propose", "--now", NOW)
    did = first_id(capsys, r"Drafted decision (\w+)")
    assert run(root, db, "decisions", "reject", did, "--by", "sam") == 4
    assert run(root, db, "decisions", "reject", did, "--by", "sam", "--reason", "Not this quarter") == 0
    run(root, db, "specwrite", "--decision", did)
    assert Store(db).list_specs() == []


def test_weekly_run_does_everything_and_one_failure_does_not_stop_the_rest(ws, tmp_path):
    root, db, llm = ws
    store = Store(db)
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    steps = run_weekly(root, store, lambda: llm, now=now, mixpanel=FakeMixpanel(lambda *a: 10),
                       sources=[JsonlConnector(DEMO)])
    assert [s.name for s in steps] == ["ingest jsonl", "synthesize", "analyst", "outcomes", "digest"]
    assert all(s.ok for s in steps), steps
    digest = (root / "digests" / "2026-10-01.md").read_text(encoding="utf-8")
    assert "Context (mixpanel)" in digest and "csv-export-fails" in digest

    def broken():
        raise RuntimeError("no API key")

    store2 = Store(tmp_path / "second.db")
    steps = run_weekly(root, store2, broken, now=now, sources=[JsonlConnector(DEMO)])
    by = {s.name: s for s in steps}
    assert by["ingest jsonl"].ok and by["digest"].ok
    assert not by["synthesize"].ok and "no API key" in by["synthesize"].detail


def test_a_bad_source_config_is_reported_not_fatal(ws, monkeypatch):
    root, db, llm = ws
    monkeypatch.delenv("ZENDESK_SUBDOMAIN", raising=False)
    steps = run_weekly(root, Store(db), lambda: llm, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert not steps[0].ok and steps[0].name == "sources" and "ZENDESK" in steps[0].detail
    assert steps[-1].name == "digest" and steps[-1].ok


def test_cli_gate_commands_need_ids_and_names(ws, capsys):
    root, db, _ = ws
    assert run(root, db, "decisions", "approve") == 2
    assert run(root, db, "decisions", "approve", "nope", "--by", "") == 4
    assert run(root, db, "specs", "approve", "nope", "--by", "sam") == 4
    assert run(root, db, "strategy", "set", "--by", "sam") == 2
