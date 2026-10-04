import json

import httpx

from product_agents.connectors import FolderConnector, JsonlConnector, ZendeskConnector
from product_agents.ingest import ingest
from product_agents.store import Store


def make_zendesk(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ZendeskConnector("acme", "a@b.co", "tok", client=client, sleep=lambda s: None, **kw)


def ticket(i, status="open", subject="Export broken", desc="Please call +1 415 555 0132"):
    return {
        "id": i,
        "status": status,
        "subject": subject,
        "description": desc,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": f"2026-10-01T10:0{i}:00Z",
        "tags": ["export", "smb"],
    }


def test_zendesk_paginates_skips_deleted_and_redacts(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "page2" in str(request.url):
            return httpx.Response(200, json={"tickets": [ticket(3)], "end_of_stream": True})
        return httpx.Response(
            200,
            json={
                "tickets": [ticket(1), ticket(2, status="deleted")],
                "end_of_stream": False,
                "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?page2",
            },
        )

    store = Store(tmp_path / "a.db")
    result = ingest(make_zendesk(handler), store, since="2026-09-30T00:00:00Z")
    assert result.new == 2 and len(calls) == 2
    assert "start_time=" in calls[0]
    stored = store.unassigned_evidence(10)
    assert all("415" not in e.text and "[PHONE]" in e.text for e in stored)
    assert stored[0].source_url == "https://acme.zendesk.com/agent/tickets/1"
    assert stored[0].segment == "export,smb"


def test_zendesk_retries_on_rate_limit():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"tickets": [], "end_of_stream": True})

    assert list(make_zendesk(handler).fetch("2026-10-01T00:00:00Z")) == []
    assert attempts["n"] == 2


def test_zendesk_only_issues_get_requests():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"tickets": [ticket(1)], "end_of_stream": True})

    list(make_zendesk(handler).fetch(None))
    assert set(methods) == {"GET"}


def test_jsonl_connector_and_cursor(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = [
        {"id": "a", "timestamp": "2026-10-01T00:00:00Z", "text": "one"},
        {"id": "b", "timestamp": "2026-10-02T00:00:00Z", "text": "two"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    store = Store(tmp_path / "a.db")
    first = ingest(JsonlConnector(path), store)
    assert first.new == 2
    second = ingest(JsonlConnector(path), store)
    assert second.fetched == 0  # cursor skips what was already ingested
    assert store.get_meta("cursor:jsonl") == "2026-10-02T00:00:00Z"


def test_jsonl_bad_line_reports_location(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"id": "a"}\n', encoding="utf-8")
    try:
        list(JsonlConnector(path).fetch(None))
    except ValueError as exc:
        assert ":1:" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_folder_connector(tmp_path):
    (tmp_path / "call1.txt").write_text("Customer said email me at x@y.com", encoding="utf-8")
    (tmp_path / "skip.bin").write_bytes(b"\x00")
    records = list(FolderConnector(tmp_path).fetch(None))
    assert [r.source_id for r in records] == ["call1.txt"]
    store = Store(tmp_path / "a.db")
    ingest(FolderConnector(tmp_path), store)
    assert "[EMAIL]" in store.unassigned_evidence(1)[0].text
