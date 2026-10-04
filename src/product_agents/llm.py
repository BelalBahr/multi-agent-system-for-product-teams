"""Model provider interface. Claude is the default; others can implement `LLMProvider`."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .policy import BudgetExceeded

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResult: ...


class AnthropicProvider:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        import anthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic(api_key=key)
        self.model = model or os.environ.get("PT_MODEL", DEFAULT_MODEL)

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResult:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResult(text, resp.usage.input_tokens, resp.usage.output_tokens)


class FakeProvider:
    """Deterministic provider for tests: `responder(system, user)` returns the text."""

    def __init__(self, responder: Callable[[str, str], str], tokens_per_call: int = 100):
        self.responder = responder
        self.tokens_per_call = tokens_per_call
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResult:
        self.calls.append((system, user))
        return LLMResult(self.responder(system, user), self.tokens_per_call, 0)


class BudgetedProvider:
    """Counts tokens for one run and stops it when the run or weekly budget is spent."""

    def __init__(self, inner: LLMProvider, run_budget: int | None, remaining_weekly: int | None):
        self.inner = inner
        self.run_budget = run_budget
        self.remaining_weekly = remaining_weekly
        self.used = 0

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResult:
        if self.run_budget is not None and self.used >= self.run_budget:
            raise BudgetExceeded(f"run budget of {self.run_budget} tokens spent")
        if self.remaining_weekly is not None and self.used >= self.remaining_weekly:
            raise BudgetExceeded("weekly token cap reached")
        result = self.inner.complete(system, user, max_tokens)
        self.used += result.input_tokens + result.output_tokens
        return result


def extract_json(text: str) -> Any:
    """Parse the first JSON object or array in `text`, tolerating code fences."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    decoder = json.JSONDecoder()
    for i, ch in enumerate(candidate):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(candidate[i:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model output")
