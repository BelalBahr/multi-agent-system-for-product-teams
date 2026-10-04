"""The three new agents through the real CLI and the weekly orchestrator."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from helpers import FakeWriter
from product_agents import cli, config
from product_agents.connectors import Task, WebPagesConnector
from product_agents.llm import FakeProvider
from product_agents.orchestrator import run_weekly
from product_agents.store import Store

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
TRANSCRIPT = "Maya said the export screen is confusing and she keeps a spreadsheet as a workaround."
PUBLIC = "93.184.216.34"


class Reader:
    id = "clickup-read"

    def list_tasks(self, list_id):
        return [
            Task("t1", "Ship export fix", "in progress", "custom", NOW - timedelta(days=2), NOW - timedelta(days=1), ("sam",), (), "https://app.clickup.com/t/t1"),
            Task("t2", "Legal review", "blocked", "custom", None, NOW - timedelta(days=1), (), (), "https://app.clickup.com/t/t2"),
        ]


def model(system, user):
    if "Source type:" in user:
        return json.dumps({"summary": "Maya finds exports confusing.", "findings": [
            {"type": "pain", "text": "Finds the export screen confusing", "quote": "the export screen is confusing"},
            {"type": "pain", "text": "Fabricated", "quote": "this text is not in the transcript"}]})
    if "Facts about the team's open work" in user:
        return json.dumps({"headline": "One task is late and one is blocked", "summary_md": "Export work is behind.",
                           "risks": [{"task_id": "t1", "note": "Late."}], "asks": []})
    if "Audience:" in user:
        if "Audience: sales" in user:
            return json.dumps({"subject": "Ask", "body_md": "We will ship in 12 days."})  # invented figure
        return json.dumps({"subject": "Delivery", "body_md": "One task is late and one is blocked."})
    return "{}"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    cli.main(["init", str(root)])
    llm = FakeProvider(model)
    monkeypatch.setattr(cli, "_llm", lambda: llm)
    return root, str(root / "product-agents.db")


def run(root, db, *argv):
    return cli.main(["--db", db, "--config", str(root), *argv])


def test_init_installs_every_agent_and_they_all_load(ws):
    root, _ = ws
    for name in config.AGENT_FILES:
        a = config.agent(root, name)
        assert a.name == name
    assert len(config.AGENT_FILES) == 9 and (root / "audiences.yaml").exists()
    assert not config.agent(root, "researcher").connectors  # the Researcher never fetches


def test_web_ingest_then_research_notes_with_verified_quotes_only(ws, monkeypatch, capsys):
    root, db = ws
    urls = root / "urls.txt"
    urls.write_text("https://acme.test/pricing\nhttp://localhost/admin\n", encoding="utf-8")
    page = f"<html><title>Acme</title><body><p>{TRANSCRIPT}</p></body></html>"

    def factory(url_list):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
        return WebPagesConnector(url_list, client=client,
                                 resolver=lambda h, p: ["127.0.0.1"] if h == "localhost" else [PUBLIC])

    monkeypatch.setattr(cli, "WebPagesConnector", factory)
    assert run(root, db, "ingest", "web", str(urls)) == 0
    captured = capsys.readouterr()
    assert "1 new" in captured.out and "localhost" in captured.err and "non-public" in captured.err

    assert run(root, db, "research", "run") == 0
    assert "kept 1 findings, dropped 1" in capsys.readouterr().out
    assert run(root, db, "research", "list", "--kind", "competitor") == 0
    listing = capsys.readouterr().out
    note_id = listing.split()[0]
    assert run(root, db, "research", "show", note_id) == 0
    shown = capsys.readouterr().out
    assert "the export screen is confusing" in shown and "this text is not in the transcript" not in shown
    assert "1 findings dropped" in shown


def test_delivery_update_then_audience_messages_then_the_human_gate(ws, monkeypatch, capsys):
    root, db = ws
    monkeypatch.setattr(config, "clickup_reader_from_env", lambda: (Reader(), ["L1"]))
    assert run(root, db, "delivery", "--now", "2026-10-08T12:00:00Z") == 0
    out = capsys.readouterr().out
    assert "Read 2 tasks" in out and "(model)" in out
    update_id = out.split("Drafted update ")[1].split()[0]

    assert run(root, db, "updates", "show", update_id) == 0
    body = capsys.readouterr().out
    assert "One task is late and one is blocked" in body and "[Ship export fix](https://app.clickup.com/t/t1)" in body

    # sales message invents "12 days" so it is dropped (exit 3). The other audiences are kept
    assert run(root, db, "comms", "--update", update_id) == 3
    err = capsys.readouterr().err
    assert "Dropped 'sales'" in err
    listing = Store(db).list_updates("audience")
    assert {u["audience"] for u in listing} == {"executives", "support", "engineering"}
    assert all(u["status"] == "draft" for u in listing)

    writer = FakeWriter()
    monkeypatch.setattr(config, "clickup_from_env", lambda: writer)
    target = listing[0]["id"]
    assert run(root, db, "updates", "export", target, "--by", "sam") == 4  # not approved yet
    assert writer.created == []
    assert run(root, db, "updates", "approve", target, "--by", "sam") == 0
    assert "Nothing has been sent" in capsys.readouterr().out
    assert run(root, db, "updates", "export", target, "--by", "sam") == 0
    assert len(writer.created) == 1 and writer.created[0][0].startswith("[")


def test_comms_summarises_recent_activity_when_given_no_source(ws, capsys):
    root, db = ws
    assert run(root, db, "comms", "--recent", "7") == 3  # sales is dropped by the same figure check
    out = capsys.readouterr()
    assert "Source update:" in out.out
    sources = [u for u in Store(db).list_updates("status")]
    assert sources and "No decisions were made" in sources[0]["body_md"] and sources[0]["method"] == "template"


def test_delivery_needs_configuration_and_cli_validates_ids(ws, monkeypatch, capsys):
    root, db = ws
    monkeypatch.setattr(config, "clickup_reader_from_env", lambda: None)
    assert run(root, db, "delivery") == 2
    assert "CLICKUP_BACKLOG_LIST_IDS" in capsys.readouterr().err
    assert run(root, db, "updates", "approve") == 2
    assert run(root, db, "research", "show") == 2
    assert run(root, db, "updates", "approve", "ghost", "--by", "sam") == 4


def test_weekly_run_includes_research_and_delivery_when_there_is_something_to_do(ws, tmp_path):
    root, db = ws
    store = Store(db)
    folder = tmp_path / "interviews"
    folder.mkdir()
    (folder / "maya.txt").write_text(TRANSCRIPT, encoding="utf-8")
    from product_agents.connectors import FolderConnector

    steps = run_weekly(root, store, lambda: FakeProvider(model), now=NOW, sources=[FolderConnector(folder)],
                       tracker=(Reader(), ["L1"]))
    names = [s.name for s in steps]
    assert names == ["ingest folder", "synthesize", "research", "delivery", "digest"]
    by = {s.name: s for s in steps}
    assert by["research"].ok and by["delivery"].ok, steps
    assert "1 notes" in by["research"].detail and "model" in by["delivery"].detail
    assert len(store.list_research_notes()) == 1 and len(store.list_updates("status")) == 1


def test_weekly_run_skips_research_and_delivery_when_not_configured_or_nothing_new(ws):
    root, db = ws
    steps = run_weekly(root, Store(db), lambda: FakeProvider(model), now=NOW, sources=[])
    assert [s.name for s in steps] == ["synthesize", "digest"]
