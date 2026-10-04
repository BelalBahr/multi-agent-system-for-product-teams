"""Command line interface: product-agents <command>."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import __version__, config
from .agents.analyst import Analyst
from .agents.comms import StakeholderComms, load_audiences
from .agents.delivery import DeliveryCoordinator
from .agents.researcher import Researcher
from .agents.outcome import OutcomeTracker
from .agents.redteam import RedTeam
from .agents.specwriter import SpecWriter
from .agents.strategist import Strategist
from .connectors import FolderConnector, JsonlConnector, WebPagesConnector
from .dictionary import load_dictionary
from .digest import build_digest
from .evals import load_golden, pairwise_f1
from .gates import (
    GateError, approve_decision, approve_spec, approve_update, export_spec, export_update,
    reject_decision, render_decision,
)
from .ingest import ingest
from .models import make_id
from .policy import BudgetExceeded
from .runner import run_agent, run_synthesizer
from .store import Store

DEFAULT_DB = "product-agents.db"


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _root(args: argparse.Namespace) -> Path:
    return Path(args.config)


def _llm():
    from .llm import AnthropicProvider

    return AnthropicProvider()


def _now(args: argparse.Namespace) -> datetime:
    value = getattr(args, "now", None)
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)


def _guard(fn):
    """Turn expected failures into a clean message and exit code, not a traceback."""
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except GateError as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 4
        except BudgetExceeded as exc:
            print(f"Stopped: {exc}", file=sys.stderr)
            return 3
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return wrapper


@_guard
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir)
    target.mkdir(parents=True, exist_ok=True)
    template_root = resources.files("product_agents") / "templates"
    created = []
    for rel in config.TEMPLATE_FILES:
        dest = target / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(template_root / rel) as src:
            shutil.copyfile(src, dest)
        created.append(rel)
    Store(target / DEFAULT_DB).close()
    print(f"Initialised {target.resolve()} ({len(created)} files created)")
    print("Next: write strategy.md, then ingest, synthesize, digest. See the README.")
    return 0


@_guard
def cmd_ingest(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.source == "jsonl":
        connector = JsonlConnector(args.path)
    elif args.source == "folder":
        connector = FolderConnector(args.path)
    elif args.source == "web":
        connector = WebPagesConnector(config.read_url_file(Path(args.path)))
    else:
        connector = config.zendesk_from_env()
        if connector is None:
            raise RuntimeError("Set ZENDESK_SUBDOMAIN, ZENDESK_EMAIL and ZENDESK_API_TOKEN.")
    result = ingest(connector, store, since=args.since)
    print(f"Fetched {result.fetched}: {result.new} new, {result.duplicates} already stored.")
    for problem in getattr(connector, "errors", []):
        print(f"  skipped: {problem}", file=sys.stderr)
    return 0


@_guard
def cmd_synthesize(args: argparse.Namespace) -> int:
    root = _root(args)
    result = run_synthesizer(
        _store(args), config.agent(root, "signal-synthesizer"), config.policy(root), _llm(),
        batch_size=args.batch_size,
    )
    print(
        f"Assigned {result.assigned} evidence items, created {result.new_themes} themes, "
        f"kept {result.quotes_kept} quotes, rejected {result.quotes_rejected} unverifiable quotes."
    )
    if result.stopped_reason != "complete":
        print(f"Run stopped early: {result.stopped_reason}", file=sys.stderr)
        return 3
    return 0


@_guard
def cmd_analyst(args: argparse.Namespace) -> int:
    root = _root(args)
    mixpanel, clarity = config.mixpanel_from_env(), config.clarity_from_env()
    if not (mixpanel or clarity):
        raise RuntimeError("Set the MIXPANEL_* and/or CLARITY_API_TOKEN variables first.")
    dictionary = load_dictionary(root / "events.yaml")
    if not dictionary.events and not dictionary.pages:
        raise RuntimeError("events.yaml is empty. List the events and pages the Analyst may use.")
    now = _now(args)
    r = run_agent(
        _store(args), config.agent(root, "analyst"), config.policy(root), _llm(),
        lambda s, p: Analyst(s, p, dictionary, mixpanel=mixpanel, clarity=clarity, now=now).run, now=now,
    )
    print(f"Added {r.metrics_added} metrics across {r.themes_covered} themes.")
    for note in r.skipped:
        print(f"  skipped: {note}")
    return 0


@_guard
def cmd_propose(args: argparse.Namespace) -> int:
    root = _root(args)
    dictionary = load_dictionary(root / "events.yaml")
    now = _now(args)
    ids = run_agent(
        _store(args), config.agent(root, "strategist"), config.policy(root), _llm(),
        lambda s, p: lambda: Strategist(s, p, dictionary, now=now).run(args.theme), now=now,
    )
    for i in ids:
        print(f"Drafted decision {i}. Next: product-agents redteam --decision {i}")
    if not ids:
        print("No decisions drafted (no eligible theme, or the model output could not be used).")
    return 0


@_guard
def cmd_redteam(args: argparse.Namespace) -> int:
    root = _root(args)
    n = run_agent(
        _store(args), config.agent(root, "red-team"), config.policy(root), _llm(),
        lambda s, p: lambda: RedTeam(s, p).run(args.decision),
    )
    print(f"Recorded dissent on {n} decision(s). Review with: product-agents decisions show <id>")
    return 0


@_guard
def cmd_decisions(args: argparse.Namespace) -> int:
    store, root = _store(args), _root(args)
    if args.action == "list":
        for d in store.list_decisions():
            dis = "reviewed" if store.get_dissents(d["id"]) else "not reviewed"
            print(f"{d['id']}  {d['status']:<9} {dis:<13} {d['title']}")
    elif args.action == "show":
        print(render_decision(store, args.id))
    elif args.action == "approve":
        d = approve_decision(
            store, args.id, args.by, config.policy(root), assumption=args.assumption,
            waive_redteam=args.waive_redteam,
        )
        print(f"Approved. Review date: {d['review_date']}")
    elif args.action == "reject":
        reject_decision(store, args.id, args.by, args.reason or "")
        print("Rejected.")
    return 0


@_guard
def cmd_specwrite(args: argparse.Namespace) -> int:
    root = _root(args)
    ids = run_agent(
        _store(args), config.agent(root, "spec-writer"), config.policy(root), _llm(),
        lambda s, p: lambda: SpecWriter(s, p).run(args.decision),
    )
    for i in ids:
        print(f"Drafted spec {i}. Review with: product-agents specs show {i}")
    if not ids:
        print("No specs drafted (no approved decision without a spec, or the output was unusable).")
    return 0


@_guard
def cmd_specs(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.action == "list":
        for s in store.list_specs():
            print(f"{s['id']}  {s['status']:<9} {s['title']}")
    elif args.action == "show":
        spec = store.get_spec(args.id)
        if spec is None:
            raise RuntimeError(f"no spec {args.id}")
        print(spec["body_md"])
    elif args.action == "approve":
        approve_spec(store, args.id, args.by)
        print("Spec approved. Export it to your drafts list with: product-agents specs export <id> --by NAME")
    elif args.action == "export":
        writer = config.clickup_from_env()
        if writer is None:
            raise RuntimeError("Set CLICKUP_API_TOKEN and CLICKUP_DRAFTS_LIST_ID.")
        print(f"Draft created: {export_spec(store, args.id, writer, args.by)}")
        print("It is a draft. Move it out of the drafts list yourself when you want it to become real work.")
    return 0


@_guard
def cmd_delivery(args: argparse.Namespace) -> int:
    root = _root(args)
    configured = config.clickup_reader_from_env()
    if configured is None:
        raise RuntimeError("Set CLICKUP_API_TOKEN and CLICKUP_BACKLOG_LIST_IDS (comma-separated list ids).")
    reader, list_ids = configured
    now = _now(args)
    r = run_agent(
        _store(args), config.agent(root, "delivery-coordinator"), config.policy(root), _llm(),
        lambda s, p: DeliveryCoordinator(s, p, reader, list_ids, stale_days=args.stale_days, now=now).run,
        now=now,
    )
    print(f"Read {r.tasks_seen} tasks. Drafted update {r.update_id} ({r.method}).")
    if r.method == "template":
        print("The model's wording could not be grounded in the facts, so a plain template was used.")
    print(f"Review with: product-agents updates show {r.update_id}")
    return 0


@_guard
def cmd_comms(args: argparse.Namespace) -> int:
    root = _root(args)
    audiences = load_audiences(root / "audiences.yaml")
    now = _now(args)

    def build(scoped, provider):
        agent_ = StakeholderComms(scoped, provider, audiences, now=now)

        def go():
            source = args.update or agent_.summarize_recent(args.recent)
            return agent_.run(source)

        return go

    r = run_agent(_store(args), config.agent(root, "stakeholder-comms"), config.policy(root), _llm(), build, now=now)
    print(f"Source update: {r.source_id}")
    for u in r.created:
        print(f"Drafted {u}")
    for name in r.dropped:
        print(f"Dropped '{name}': the message contained figures that are not in the source.", file=sys.stderr)
    return 0 if not r.dropped else 3


@_guard
def cmd_updates(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.action == "list":
        for u in store.list_updates():
            who = u["audience"] or "-"
            print(f"{u['id']}  {u['status']:<9} {u['kind']:<8} {who:<12} {u['method']:<8} {u['title']}")
    elif args.action == "show":
        u = store.get_update(args.id)
        if u is None:
            raise RuntimeError(f"no update {args.id}")
        print(f"# {u['title']}  [{u['status']}, written by {u['method']}]\n")
        print(u["body_md"])
    elif args.action == "approve":
        approve_update(store, args.id, args.by)
        print("Approved. Nothing has been sent. Copy it out, or export it as a ClickUp draft.")
    elif args.action == "export":
        writer = config.clickup_from_env()
        if writer is None:
            raise RuntimeError("Set CLICKUP_API_TOKEN and CLICKUP_DRAFTS_LIST_ID.")
        print(f"Draft created: {export_update(store, args.id, writer, args.by)}")
    return 0


@_guard
def cmd_research(args: argparse.Namespace) -> int:
    store, root = _store(args), _root(args)
    if args.action == "run":
        r = run_agent(
            store, config.agent(root, "researcher"), config.policy(root), _llm(),
            lambda s, p: Researcher(s, p).run,
        )
        print(
            f"Wrote {r.notes} notes: kept {r.findings_kept} findings, dropped {r.findings_dropped} that "
            "lacked a verbatim quote or contained numbers not in the source."
        )
    elif args.action == "list":
        for n in store.list_research_notes(args.kind):
            print(f"{n['id']}  {n['kind']:<10} {len(n['findings'])} findings  {n['summary'][:80]}")
    else:
        n = store.get_research_note(args.id)
        if n is None:
            raise RuntimeError(f"no research note {args.id}")
        ev = store.get_evidence([n["evidence_id"]])[0]
        print(f"# {n['kind']} note on {ev.source_url}\n\n{n['summary']}\n")
        for f in n["findings"]:
            print(f"- [{f['type']}] {f['text']}\n    \"{f['quote']}\"")
        if n["dropped"]:
            print(f"\n({n['dropped']} findings dropped: no verbatim quote, or figures not in the source)")
    return 0


@_guard
def cmd_outcomes(args: argparse.Namespace) -> int:
    root = _root(args)
    mixpanel = config.mixpanel_from_env()
    if mixpanel is None:
        raise RuntimeError("Set the MIXPANEL_* variables first.")
    now = _now(args)
    done = run_agent(
        _store(args), config.agent(root, "outcome-tracker"), config.policy(root), None,
        lambda s, p: OutcomeTracker(s, mixpanel, now=now).run, now=now,
    )
    for o in done:
        print(f"{o['decision_id']}: {o['verdict']}. {o['detail']}")
    if not done:
        print("No decisions are due for review.")
    return 0


@_guard
def cmd_strategy(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.action == "set":
        if not args.by:
            raise RuntimeError("say who is setting the strategy (--by NAME)")
        store.set_strategy(Path(args.file).read_text(encoding="utf-8"), args.by)
        store.log(f"human:{args.by}", "set", "strategy", None, args.file)
        print("Strategy saved.")
    else:
        print(store.get_strategy() or "(no strategy set)")
    return 0


@_guard
def cmd_digest(args: argparse.Namespace) -> int:
    text = build_digest(_store(args), now=_now(args), window_days=args.days, top_n=args.top)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


@_guard
def cmd_audit(args: argparse.Namespace) -> int:
    for row in _store(args).audit(args.limit):
        print(
            f"{row['seq']:>5} {row['at']} {row['actor']:<24} {row['action']:<14} "
            f"{row['record_type']:<9} {row['record_id'] or '-'} {row['detail'] or ''}"
        )
    return 0


@_guard
def cmd_run(args: argparse.Namespace) -> int:
    from .orchestrator import run_weekly

    store, root = _store(args), _root(args)
    steps = run_weekly(
        root, store, _llm, now=_now(args), mixpanel=config.mixpanel_from_env(),
        clarity=config.clarity_from_env(), tracker=config.clickup_reader_from_env(),
    )
    for s in steps:
        print(f"[{'ok' if s.ok else 'FAILED'}] {s.name}: {s.detail}")
    return 0 if all(s.ok for s in steps) else 3


@_guard
def cmd_eval(args: argparse.Namespace) -> int:
    expected_by_source = load_golden(args.golden)
    if not expected_by_source:
        raise RuntimeError("The golden file has no 'expected_theme' fields.")
    root = _root(args)
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "eval.db")
        connector = JsonlConnector(args.golden)
        ingest(connector, store)
        run_synthesizer(
            store, config.agent(root, "signal-synthesizer"), config.policy(root), _llm(),
            batch_size=args.batch_size,
        )
        expected = {make_id(connector.id, sid): t for sid, t in expected_by_source.items()}
        score = pairwise_f1(store.assignments(), expected)
        store.close()
    print(
        f"Theme accuracy (pairwise): precision {score.precision:.2f}, "
        f"recall {score.recall:.2f}, F1 {score.f1:.2f}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="product-agents", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", default=os.environ.get("PT_DB", DEFAULT_DB), help="SQLite store path")
    p.add_argument("--config", default=".", help="folder holding agents/, prompts/ and policy.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help_, func, now=False):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(func=func)
        if now:
            s.add_argument("--now", help="ISO 8601 time to treat as now (reproducible demos)")
        return s

    s = add("init", "create a workspace with the default agents, prompts and policy", cmd_init)
    s.add_argument("dir", nargs="?", default=".")

    s = add("ingest", "pull records from a source into the store", cmd_ingest)
    s.add_argument("source", choices=["jsonl", "folder", "web", "zendesk"])
    s.add_argument("path", nargs="?", help="file or folder (jsonl, folder), or a file of URLs (web)")
    s.add_argument("--since", help="ISO 8601 start time, overrides the saved cursor")

    s = add("synthesize", "group evidence into themes (Signal Synthesizer)", cmd_synthesize)
    s.add_argument("--batch-size", type=int, default=15)

    add("analyst", "attach Mixpanel and Clarity numbers to top themes", cmd_analyst, now=True)

    s = add("propose", "draft decisions for top themes (Strategist)", cmd_propose, now=True)
    s.add_argument("--theme", help="theme id (default: top themes without a decision)")

    s = add("redteam", "record the Red Team's dissent on draft decisions", cmd_redteam)
    s.add_argument("--decision", help="decision id (default: all unreviewed drafts)")

    s = add("decisions", "list, show, approve or reject decisions (a person's gate)", cmd_decisions)
    s.add_argument("action", choices=["list", "show", "approve", "reject"])
    s.add_argument("id", nargs="?")
    s.add_argument("--by", default="", help="who is acting")
    s.add_argument("--assumption", action="store_true", help="approve a decision with no cited evidence")
    s.add_argument("--waive-redteam", action="store_true", help="approve without Red Team review (logged)")
    s.add_argument("--reason", help="why, when rejecting")

    s = add("specwrite", "draft specs for approved decisions (Spec Writer)", cmd_specwrite)
    s.add_argument("--decision", help="decision id (default: approved decisions without a spec)")

    s = add("specs", "list, show, approve or export specs (a person's gate)", cmd_specs)
    s.add_argument("action", choices=["list", "show", "approve", "export"])
    s.add_argument("id", nargs="?")
    s.add_argument("--by", default="", help="who is acting")

    s = add("delivery", "draft a delivery status update from ClickUp (Delivery Coordinator)", cmd_delivery, now=True)
    s.add_argument("--stale-days", type=int, default=7)

    s = add("comms", "rewrite an update for each audience (Stakeholder Comms)", cmd_comms, now=True)
    s.add_argument("--update", help="update id to rewrite")
    s.add_argument("--recent", type=int, default=14, help="otherwise summarise decisions and outcomes from the last N days")

    s = add("updates", "list, show, approve or export updates (a person's gate)", cmd_updates)
    s.add_argument("action", choices=["list", "show", "approve", "export"])
    s.add_argument("id", nargs="?")
    s.add_argument("--by", default="", help="who is acting")

    s = add("research", "write notes from interviews and competitor pages (Researcher)", cmd_research)
    s.add_argument("action", choices=["run", "list", "show"])
    s.add_argument("id", nargs="?")
    s.add_argument("--kind", choices=["interview", "competitor"])

    add("outcomes", "check decisions that are due for review (Outcome Tracker)", cmd_outcomes, now=True)

    s = add("strategy", "set or show the team strategy", cmd_strategy)
    s.add_argument("action", choices=["set", "show"])
    s.add_argument("file", nargs="?")
    s.add_argument("--by", default="")

    s = add("digest", "write the weekly digest", cmd_digest, now=True)
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--out", help="write to a file instead of printing")

    s = add("audit", "show recent audit log entries", cmd_audit)
    s.add_argument("--limit", type=int, default=30)

    s = add("run", "run the weekly cycle: ingest, synthesize, analyse, outcomes, digest", cmd_run, now=True)
    s.add_argument("cycle", choices=["weekly"])

    s = add("eval", "score theme accuracy against a labeled JSONL file", cmd_eval)
    s.add_argument("golden")
    s.add_argument("--batch-size", type=int, default=15)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest" and args.source in ("jsonl", "folder") and not args.path:
        print("A path is required for this source.", file=sys.stderr)
        return 2
    needs_id = (
        (args.command == "decisions" and args.action in ("show", "approve", "reject"))
        or (args.command == "specs" and args.action in ("show", "approve", "export"))
        or (args.command == "updates" and args.action in ("show", "approve", "export"))
        or (args.command == "research" and args.action == "show")
    )
    if needs_id and not args.id:
        print("An id is required for this action.", file=sys.stderr)
        return 2
    if args.command == "strategy" and args.action == "set" and not args.file:
        print("A file is required.", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
