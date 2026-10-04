from datetime import date

import httpx
import pytest

from product_agents.connectors import ClarityConnector, ClickUpDraftWriter, MixpanelConnector


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_mixpanel_sums_segmentation_values_and_only_reads():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"values": {"export_completed": {"2026-10-01": 10, "2026-10-02": 5}}}})

    m = MixpanelConnector("123", "svc", "sec", region="eu", client=client(handler))
    assert m.event_count("export_completed", date(2026, 10, 1), date(2026, 10, 2)) == 15
    req = seen[0]
    assert req.method == "GET" and req.url.host == "eu.mixpanel.com"
    assert req.url.params["event"] == "export_completed" and req.url.params["project_id"] == "123"
    assert req.url.params["from_date"] == "2026-10-01"


def test_mixpanel_rejects_unknown_region_and_has_no_stream():
    with pytest.raises(ValueError):
        MixpanelConnector("1", "u", "s", region="mars")
    assert list(MixpanelConnector("1", "u", "s", client=client(lambda r: httpx.Response(200))).fetch(None)) == []


def clarity_payload():
    return [
        {"metricName": "DeadClickCount", "information": [
            {"URL": "https://app.test/reports/export", "sessionsCount": "100", "sessionsWithMetricPercentage": 6.0},
            {"URL": "https://app.test/reports/export?x=1", "sessionsCount": "300", "sessionsWithMetricPercentage": 2.0},
            {"URL": "https://app.test/home", "sessionsCount": "900", "sessionsWithMetricPercentage": 0.5}]},
        {"metricName": "RageClickCount", "information": [
            {"URL": "https://app.test/reports/export", "sessionsCount": "100", "sessionsWithMetricPercentage": 3.0}]},
        {"metricName": "Traffic", "information": [{"totalSessionCount": "5"}]},
    ]


def test_clarity_weights_by_sessions_and_ignores_other_pages():
    c = ClarityConnector("tok", client=client(lambda r: httpx.Response(200, json=clarity_payload())))
    data = c.page_friction("/reports/export")
    assert data["DeadClickCount"] == 3.0  # (6*100 + 2*300) / 400
    assert data["RageClickCount"] == 3.0
    assert data["sessions"] == 400
    assert "Traffic" not in data


def test_clarity_returns_none_rather_than_guessing():
    c = ClarityConnector("tok", client=client(lambda r: httpx.Response(200, json=clarity_payload())))
    assert c.page_friction("/nonexistent") is None
    odd = ClarityConnector("tok", client=client(lambda r: httpx.Response(200, json={"unexpected": 1})))
    assert odd.page_friction("/x") is None


def test_clickup_can_only_create_a_task_in_the_drafts_list():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "abc", "url": "https://app.clickup.com/t/abc"})

    w = ClickUpDraftWriter("tok", "999", client=client(handler))
    url = w.create_draft("Spec", "# Body")
    assert url == "https://app.clickup.com/t/abc"
    assert [r.method for r in seen] == ["POST"]
    assert str(seen[0].url) == "https://api.clickup.com/api/v2/list/999/task"
    assert b'"markdown_description"' in seen[0].content and b"agent-draft" in seen[0].content
    assert w.scopes == ("draft-write",)
    assert list(w.fetch(None)) == []


def test_clickup_requires_a_designated_list():
    with pytest.raises(ValueError):
        ClickUpDraftWriter("tok", "")
