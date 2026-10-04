"""The weekly run: ingest, synthesize, analyse, check outcomes, write the digest.

Each step is isolated. If one fails the others still run, and the failure is reported plainly.
This does not schedule itself: run it from cron, Task Scheduler or a CI job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config
from .agents.analyst import Analyst
from .agents.delivery import DeliveryCoordinator
from .agents.researcher import Researcher
from .agents.outcome import OutcomeTracker
from .dictionary import load_dictionary
from .digest import build_digest
from .ingest import ingest
from .llm import LLMProvider
from .runner import run_agent, run_synthesizer
from .store import Store


@dataclass
class Step:
    name: str
    ok: bool
    detail: str


def run_weekly(
    root: Path,
    store: Store,
    llm_factory: Callable[[], LLMProvider],
    now: datetime | None = None,
    mixpanel=None,
    clarity=None,
    sources: list | None = None,
    tracker: tuple | None = None,
) -> list[Step]:
    now = now or datetime.now(timezone.utc)
    pol = config.policy(root)
    steps: list[Step] = []

    def step(name: str, fn: Callable[[], str]) -> None:
        try:
            steps.append(Step(name, True, fn()))
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            steps.append(Step(name, False, f"{type(exc).__name__}: {exc}"))

    if sources is None:
        try:
            sources = config.load_sources(root)
        except Exception as exc:  # noqa: BLE001 - a bad source must not stop the rest of the run
            steps.append(Step("sources", False, f"{type(exc).__name__}: {exc}"))
            sources = []
    for conn in sources:
        def do_ingest(conn=conn) -> str:
            r = ingest(conn, store)
            return f"{r.new} new, {r.duplicates} already stored"
        step(f"ingest {conn.id}", do_ingest)

    llm = None

    def get_llm() -> LLMProvider:
        nonlocal llm
        if llm is None:
            llm = llm_factory()
        return llm

    def do_synth() -> str:
        r = run_synthesizer(store, config.agent(root, "signal-synthesizer"), pol, get_llm(), now=now)
        if r.stopped_reason != "complete":
            raise RuntimeError(r.stopped_reason)
        return f"{r.assigned} assigned, {r.new_themes} new themes, {r.quotes_rejected} quotes rejected"
    step("synthesize", do_synth)

    if mixpanel or clarity:
        def do_analyst() -> str:
            d = load_dictionary(root / "events.yaml")
            r = run_agent(
                store, config.agent(root, "analyst"), pol, get_llm(),
                lambda s, p: Analyst(s, p, d, mixpanel=mixpanel, clarity=clarity, now=now).run, now=now,
            )
            return f"{r.metrics_added} metrics across {r.themes_covered} themes"
        step("analyst", do_analyst)

    if mixpanel:
        def do_outcomes() -> str:
            done = run_agent(
                store, config.agent(root, "outcome-tracker"), pol, None,
                lambda s, p: OutcomeTracker(s, mixpanel, now=now).run, now=now,
            )
            return f"{len(done)} decisions reviewed"
        step("outcomes", do_outcomes)

    if store.unresearched_evidence(("folder", "web"), 1):
        def do_research() -> str:
            r = run_agent(
                store, config.agent(root, "researcher"), pol, get_llm(),
                lambda s, p: Researcher(s, p).run, now=now,
            )
            return f"{r.notes} notes, {r.findings_kept} findings kept, {r.findings_dropped} dropped"
        step("research", do_research)

    if tracker:
        reader, list_ids = tracker

        def do_delivery() -> str:
            r = run_agent(
                store, config.agent(root, "delivery-coordinator"), pol, get_llm(),
                lambda s, p: DeliveryCoordinator(s, p, reader, list_ids, now=now).run, now=now,
            )
            return f"update {r.update_id} from {r.tasks_seen} tasks ({r.method})"
        step("delivery", do_delivery)

    def do_digest() -> str:
        out = root / "digests" / f"{now.date()}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_digest(store, now=now), encoding="utf-8")
        return str(out)
    step("digest", do_digest)
    return steps
