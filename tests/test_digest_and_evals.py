import json
from datetime import datetime, timezone
from pathlib import Path

from product_agents import cli
from product_agents.digest import build_digest
from product_agents.evals import load_golden, pairwise_f1, traceability
from product_agents.models import Evidence
from product_agents.store import Store

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def add(store, i, ts, theme):
    store.add_evidence(Evidence(i, "zendesk", i, f"http://t/{i}", ts, "", f"text {i} broken"))
    store.assign_evidence(i, theme)


def test_digest_ranks_by_current_volume_and_reports_trend(tmp_path):
    store = Store(tmp_path / "a.db")
    store.create_theme("t1", "Export fails", "Exports break.")
    store.create_theme("t2", "Slow dashboard", "Slow.")
    store.create_theme("t3", "Old issue", "Gone.")
    for i in range(4):
        add(store, f"a{i}", "2026-10-06T00:00:00Z", "t1")  # 4 this week
    add(store, "b0", "2026-10-06T00:00:00Z", "t2")
    for i in range(3):
        add(store, f"b{i + 1}", "2026-09-30T00:00:00Z", "t2")  # 3 previous week
    add(store, "c0", "2026-09-01T00:00:00Z", "t3")  # outside both windows
    store.add_quote("t1", "a0", "text a0 broken")
    store.set_confidence("t1", "medium")

    md = build_digest(store, now=NOW)
    assert md.index("Export fails") < md.index("Slow dashboard")
    assert "Old issue" not in md
    assert "new this period" in md  # t1 had nothing the week before
    assert "down 2" in md  # t2: 1 now vs 3 before
    assert '"text a0 broken" ([source](http://t/a0))' in md
    assert "No verified quotes" in md  # t2 has none
    assert "Blind spot" in md


def test_digest_with_no_activity(tmp_path):
    md = build_digest(Store(tmp_path / "a.db"), now=NOW)
    assert "No themes have new evidence" in md


def test_pairwise_f1_perfect_and_imperfect():
    expected = {"a": "x", "b": "x", "c": "y", "d": "y"}
    assert pairwise_f1({"a": "1", "b": "1", "c": "2", "d": "2"}, expected).f1 == 1.0
    merged = pairwise_f1({"a": "1", "b": "1", "c": "1", "d": "1"}, expected)
    assert merged.recall == 1.0 and merged.precision < 1.0
    split = pairwise_f1({"a": "1", "b": "2", "c": "3", "d": "4"}, expected)
    assert split.recall == 0.0 and split.f1 == 0.0


def test_traceability():
    assert traceability([]) == 1.0
    assert traceability([("export failed", "Our export failed"), ("made up quote text", "other")]) == 0.5


def test_demo_dataset_is_labeled_and_contains_pii_and_an_injection_attempt():
    path = Path(__file__).resolve().parent.parent / "examples" / "demo" / "tickets.jsonl"
    expected = load_golden(path)
    assert len(expected) >= 30 and len(set(expected.values())) == 5
    raw = path.read_text(encoding="utf-8")
    assert "@example.com" in raw and "IGNORE ALL PREVIOUS INSTRUCTIONS" in raw


def test_cli_init_ingest_digest_end_to_end(tmp_path, capsys):
    ws = tmp_path / "ws"
    assert cli.main(["init", str(ws)]) == 0
    assert (ws / "agents" / "signal-synthesizer.yaml").exists() and (ws / "policy.yaml").exists()
    demo = Path(__file__).resolve().parent.parent / "examples" / "demo" / "tickets.jsonl"
    db = str(ws / "product-agents.db")
    assert cli.main(["--db", db, "ingest", "jsonl", str(demo)]) == 0
    capsys.readouterr()
    store = Store(db)
    assert store.count_evidence() == 36
    texts = " ".join(e.text for e in store.unassigned_evidence(100))
    assert "@example.com" not in texts and "[EMAIL]" in texts and "+1 415" not in texts
    store.close()
    assert cli.main(["--db", db, "digest", "--now", "2026-10-02T00:00:00Z"]) == 0
    assert "No themes have new evidence" in capsys.readouterr().out  # nothing synthesized yet
    assert cli.main(["--db", db, "audit"]) == 0


def test_default_agent_template_is_valid(tmp_path):
    from product_agents.policy import load_agent

    ws = tmp_path / "ws"
    cli.main(["init", str(ws)])
    agent = load_agent(ws / "agents" / "signal-synthesizer.yaml")
    assert agent.tier == "A" and agent.reads_untrusted and "themes" in agent.writes
    assert "specific problem" in agent.prompt_text
