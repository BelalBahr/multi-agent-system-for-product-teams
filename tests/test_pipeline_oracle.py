"""End-to-end run with an oracle standing in for the model.

This proves the plumbing (ingest, redaction, batching, theme creation, quote
verification, digest, eval scoring). It says nothing about how well a real
model clusters tickets: that is what `product-agents eval` measures.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from product_agents.connectors import JsonlConnector
from product_agents.digest import build_digest
from product_agents.evals import load_golden, pairwise_f1
from product_agents.ingest import ingest
from product_agents.llm import FakeProvider
from product_agents.models import make_id
from product_agents.policy import load_agent, load_policy
from product_agents.runner import run_synthesizer
from product_agents.store import Store
from product_agents import cli

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo" / "tickets.jsonl"
TAG = re.compile(r'<evidence id="([^"]+)">\n(.*?)\n</evidence>', re.DOTALL)


def oracle():
    subject_theme = {}
    for line in DEMO.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        subject_theme.setdefault(obj["text"].split("\n")[0], obj["expected_theme"])

    def respond(system, user):
        assignments = []
        for eid, text in TAG.findall(user):
            theme = subject_theme[text.split("\n")[0]]
            body = text.split("\n\n", 1)[1]
            assignments.append(
                {"evidence_id": eid, "theme_id": None, "new_theme_title": theme,
                 "new_theme_summary": f"About {theme}.", "quote": body[:40]}
            )
        return json.dumps({"assignments": assignments})

    return FakeProvider(respond)


def test_full_pipeline_scores_perfectly_and_digest_reads_well(tmp_path):
    ws = tmp_path / "ws"
    cli.main(["init", str(ws)])
    store = Store(ws / "product-agents.db")
    connector = JsonlConnector(DEMO)
    ingest(connector, store)
    agent = load_agent(ws / "agents" / "signal-synthesizer.yaml")
    result = run_synthesizer(store, agent, load_policy(ws / "policy.yaml"), oracle(), batch_size=10)
    assert result.stopped_reason == "complete" and result.assigned == 36
    assert result.quotes_rejected == 0 and result.new_themes == 5

    expected = {make_id(connector.id, sid): t for sid, t in load_golden(DEMO).items()}
    assert pairwise_f1(store.assignments(), expected).f1 == 1.0

    # The demo data is anchored at 2026-10-01 12:00 UTC, so its weekly windows line up there.
    md = build_digest(store, now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    assert md.index("csv-export-fails") < md.index("dashboard-slow")  # 6 this week vs 3
    assert "mobile-app-crash" not in md  # no tickets this week, so it drops out
    assert "invoice-wrong-currency" in md and "down 1" in md  # 2 last week, 1 this week
    assert "sso-setup-confusing" in md and "up 2" in md  # 2 last week, 4 this week
    assert "@example.com" not in md and "415" not in md  # redaction held all the way through
    # the hijack attempt was stored as ordinary evidence, not obeyed
    assert not any(t.title.lower() == "everything is fine" for t in store.list_themes())
