from datetime import datetime, timezone

import httpx
import pytest

from product_agents.connectors import ClickUpReader, UnsafeUrl, WebPagesConnector, check_public_url
from product_agents.connectors.web import extract_text
from product_agents.ingest import ingest
from product_agents.store import Store

PUBLIC = "93.184.216.34"


def resolver_for(mapping, default=PUBLIC):
    return lambda host, port: [mapping.get(host, default)]


# ---- SSRF guard ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,why",
    [
        ("file:///etc/passwd", "only http"),
        ("ftp://example.com/x", "only http"),
        ("http://user:pw@example.com/", "credentials"),
        ("http://example.com:8080/", "port 8080"),
        ("http://example.com:22/", "port 22"),
        ("http:///nohost", "no host"),
    ],
)
def test_rejects_bad_schemes_credentials_ports_and_missing_hosts(url, why):
    with pytest.raises(UnsafeUrl):
        check_public_url(url, resolver_for({}))


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "0.0.0.0",
     "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "100.64.0.1"],
)
def test_rejects_hosts_that_resolve_to_non_public_addresses(address):
    with pytest.raises(UnsafeUrl, match="non-public"):
        check_public_url("http://internal.example/", lambda h, p: [address])


def test_one_private_address_among_public_ones_is_enough_to_reject():
    with pytest.raises(UnsafeUrl):
        check_public_url("https://rebind.example/", lambda h, p: [PUBLIC, "10.0.0.5"])


def test_unresolvable_host_is_rejected_and_public_host_passes():
    def boom(host, port):
        raise OSError("no such host")

    with pytest.raises(UnsafeUrl, match="resolve"):
        check_public_url("https://nope.example/", boom)
    check_public_url("https://example.com/pricing", resolver_for({}))
    check_public_url("http://example.com:80/", resolver_for({}))


# ---- fetching ------------------------------------------------------------------------------

HTML = """<html><head><title>Acme  Pricing</title><script>var secret = 1;</script></head>
<body><style>.x{}</style><h1>Plans</h1><p>Team plan is $29 per seat. Contact sales@acme.test.</p>
<noscript>enable js</noscript></body></html>"""


def connector(handler, urls, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kw.setdefault("resolver", resolver_for({}))
    kw.setdefault("now", lambda: datetime(2026, 10, 5, 9, tzinfo=timezone.utc))
    return WebPagesConnector(urls, client=client, **kw)


def ok(html=HTML, ctype="text/html; charset=utf-8"):
    return httpx.Response(200, text=html, headers={"content-type": ctype})


def test_extracts_visible_text_and_drops_scripts_and_styles():
    text = extract_text(HTML)
    assert text.startswith("Acme Pricing")
    assert "Team plan is $29 per seat" in text
    assert "secret" not in text and ".x{}" not in text and "enable js" not in text


def test_fetch_makes_dated_snapshots_and_ingest_redacts(tmp_path):
    c = connector(lambda r: ok(), ["https://acme.test/pricing", "# a comment", ""])
    store = Store(tmp_path / "a.db")
    assert ingest(c, store).new == 1
    ev = store.unassigned_evidence(5)[0]
    assert ev.source_type == "web" and ev.source_url == "https://acme.test/pricing"
    assert "[EMAIL]" in ev.text and "sales@acme.test" not in ev.text
    assert ev.source_id == "https://acme.test/pricing#2026-10-05"
    # the same page the next day is a new snapshot, never an overwrite of old evidence
    later = connector(lambda r: ok(HTML.replace("$29", "$39")), ["https://acme.test/pricing"],
                      now=lambda: datetime(2026, 10, 6, 9, tzinfo=timezone.utc))
    assert ingest(later, store).new == 1 and store.count_evidence() == 2


def test_redirect_to_an_internal_host_is_blocked_before_any_request_to_it():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "acme.test":
            return httpx.Response(302, headers={"location": "http://internal.test/admin"})
        return ok("secret")

    c = connector(handler, ["https://acme.test/go"], resolver=resolver_for({"internal.test": "10.0.0.5"}))
    assert list(c.fetch(None)) == []
    assert seen == ["acme.test"] and "non-public" in c.errors[0]


def test_follows_a_safe_redirect_but_not_forever():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return ok()

    assert len(list(connector(handler, ["https://acme.test/old"]).fetch(None))) == 1
    loop = connector(lambda r: httpx.Response(302, headers={"location": "/again"}), ["https://acme.test/a"])
    assert list(loop.fetch(None)) == [] and "too many redirects" in loop.errors[0]


def test_non_text_content_oversized_pages_and_http_errors_are_skipped_and_reported():
    def handler(request):
        path = request.url.path
        if path == "/img":
            return ok("PNG", "image/png")
        if path == "/big":
            return ok("x" * 5000)
        return httpx.Response(404)

    c = connector(handler, ["https://a.test/img", "https://a.test/big", "https://a.test/gone"], max_bytes=1000)
    assert list(c.fetch(None)) == []
    joined = " ".join(c.errors)
    assert "not a text page" in joined and "larger than" in joined and "404" in joined


def test_one_bad_url_does_not_stop_the_rest():
    def handler(request):
        return ok() if request.url.host == "good.test" else httpx.Response(500)

    c = connector(handler, ["https://bad.test/", "https://good.test/"])
    assert [r.source_url for r in c.fetch(None)] == ["https://good.test/"]


def test_only_get_requests_are_made():
    methods = []

    def handler(request):
        methods.append(request.method)
        return ok()

    list(connector(handler, ["https://a.test/"]).fetch(None))
    assert set(methods) == {"GET"}


# ---- ClickUp reader ------------------------------------------------------------------------

def task(i, **over):
    base = {"id": f"t{i}", "name": f"Task {i}", "status": {"status": "in progress", "type": "custom"},
            "due_date": "1759276800000", "date_updated": "1759104000000",
            "assignees": [{"username": "sam"}], "tags": [{"name": "blocked"}],
            "url": f"https://app.clickup.com/t/t{i}"}
    return {**base, **over}


def test_reader_parses_tasks_paginates_and_only_reads():
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        if page == 0:
            return httpx.Response(200, json={"tasks": [task(1), task(2, due_date=None, assignees=[], tags=[])], "last_page": False})
        return httpx.Response(200, json={"tasks": [task(3, status={"status": "done", "type": "closed"})], "last_page": True})

    r = ClickUpReader("tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    tasks = r.list_tasks("42")
    assert [t.id for t in tasks] == ["t1", "t2", "t3"]
    t1, t2 = tasks[0], tasks[1]
    assert t1.due == datetime(2025, 10, 1, tzinfo=timezone.utc) and t1.assignees == ("sam",) and t1.tags == ("blocked",)
    assert t2.due is None and t2.assignees == ()
    assert {q.method for q in seen} == {"GET"}
    assert seen[0].url.path == "/api/v2/list/42/task" and seen[0].url.params["include_closed"] == "false"
    assert r.scopes == ("read",) and r.id == "clickup-read" and list(r.fetch(None)) == []


def test_reader_stops_at_max_pages():
    def handler(request):
        return httpx.Response(200, json={"tasks": [task(1)], "last_page": False})

    r = ClickUpReader("tok", client=httpx.Client(transport=httpx.MockTransport(handler)), max_pages=3)
    assert len(r.list_tasks("1")) == 3
