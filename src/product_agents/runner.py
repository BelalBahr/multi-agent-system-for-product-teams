"""Runs an agent under its definition: scoped store, token budget, run record."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .agents.synthesizer import SignalSynthesizer, SynthesisResult
from .llm import BudgetedProvider, LLMProvider
from .policy import AgentDef, BudgetExceeded, Policy, ScopedStore
from .store import Store


def run_agent(
    store: Store,
    agent: AgentDef,
    policy: Policy,
    llm: LLMProvider | None,
    build: Callable[[ScopedStore, LLMProvider | None], Callable[[], Any]],
    now: datetime | None = None,
) -> Any:
    """Run one agent. `build(scoped, llm)` returns the zero-argument callable that does the work.

    The agent only ever receives a ScopedStore and a budget-limited model handle. The run is
    recorded with its token use and final status, even if it fails.
    """
    now = now or datetime.now(timezone.utc)
    remaining = None
    if policy.weekly_token_cap is not None:
        week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        remaining = policy.weekly_token_cap - store.tokens_used_since(week_ago)
        if remaining <= 0:
            raise BudgetExceeded("weekly token cap already reached")
    budgeted = BudgetedProvider(llm, agent.max_tokens_per_run, remaining) if llm is not None else None
    run_id = uuid.uuid4().hex[:12]
    store.start_run(run_id, agent.name)
    scoped = ScopedStore(store, agent, run_id)
    status = "ok"
    try:
        result = build(scoped, budgeted)()
        stopped = getattr(result, "stopped_reason", "complete")
        if stopped != "complete":
            status = stopped
        return result
    except BudgetExceeded as exc:
        status = f"budget: {exc}"
        raise
    except Exception as exc:
        status = f"error: {exc}"
        raise
    finally:
        store.finish_run(run_id, budgeted.used if budgeted else 0, status)


def run_synthesizer(
    store: Store,
    agent: AgentDef,
    policy: Policy,
    llm: LLMProvider,
    batch_size: int = 15,
    now: datetime | None = None,
) -> SynthesisResult:
    return run_agent(
        store, agent, policy, llm,
        lambda scoped, provider: SignalSynthesizer(scoped, provider, batch_size=batch_size).run,
        now=now,
    )
