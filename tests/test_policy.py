import textwrap

import pytest

from product_agents.policy import (
    AgentDef,
    PolicyViolation,
    ScopedStore,
    load_agent,
    load_policy,
)
from product_agents.store import Store


def write_agent(tmp_path, body):
    path = tmp_path / "agent.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_valid_agent_reads_prompt_file(tmp_path):
    (tmp_path / "p.md").write_text("be careful", encoding="utf-8")
    path = write_agent(
        tmp_path,
        """\
        name: a
        role: r
        tier: a
        reads: [evidence]
        writes: [themes]
        prompt: p.md
        budget: {max_tokens_per_run: 5}
        """,
    )
    agent = load_agent(path)
    assert agent.tier == "A" and agent.prompt_text == "be careful"
    assert agent.max_tokens_per_run == 5


def test_tier_c_may_only_write_dissent(tmp_path):
    path = write_agent(tmp_path, "name: a\nrole: r\ntier: C\nwrites: [themes]\n")
    with pytest.raises(ValueError, match="tier C"):
        load_agent(path)


def test_unknown_record_type_rejected(tmp_path):
    path = write_agent(tmp_path, "name: a\nrole: r\ntier: A\nreads: [secrets]\n")
    with pytest.raises(ValueError, match="unknown record type"):
        load_agent(path)


def test_untrusted_reader_cannot_hold_write_connector(tmp_path):
    path = write_agent(
        tmp_path,
        "name: a\nrole: r\ntier: A\nreads_untrusted: true\nconnectors: ['clickup:draft-write']\n",
    )
    with pytest.raises(ValueError, match="untrusted"):
        load_agent(path)


def agent(reads=("evidence",), writes=()):
    return AgentDef("t", "r", "A", tuple(reads), tuple(writes), (), "")


def test_scoped_store_blocks_unlisted_access(tmp_path):
    scoped = ScopedStore(Store(tmp_path / "a.db"), agent())
    scoped.unassigned_evidence(5)  # allowed
    with pytest.raises(PolicyViolation):
        scoped.list_themes()
    with pytest.raises(PolicyViolation):
        scoped.create_theme("t1", "x", "")


def test_scoped_writes_are_audited(tmp_path):
    store = Store(tmp_path / "a.db")
    scoped = ScopedStore(store, agent(reads=("themes",), writes=("themes",)))
    scoped.create_theme("t1", "Title", "")
    rows = store.audit(5)
    assert rows[0]["actor"] == "t" and rows[0]["action"] == "create"
    assert rows[0]["run_id"] == scoped.run_id


def test_load_policy_defaults(tmp_path):
    assert load_policy(None).weekly_token_cap is None
    p = tmp_path / "policy.yaml"
    p.write_text("budgets: {weekly_token_cap: 10}\n", encoding="utf-8")
    assert load_policy(p).weekly_token_cap == 10
