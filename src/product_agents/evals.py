"""Evaluation helpers: how well do the agent's themes match a human labeling?"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from .agents.synthesizer import quote_is_verbatim


@dataclass(frozen=True)
class PairwiseScore:
    precision: float
    recall: float
    f1: float


def pairwise_f1(predicted: dict[str, str], expected: dict[str, str]) -> PairwiseScore:
    """Compare two clusterings by the item pairs they put together.

    Unassigned items (missing from `predicted`) count as singletons. Only ids
    present in `expected` are scored.
    """
    ids = sorted(expected)
    tp = pred_pairs = exp_pairs = 0
    for a, b in combinations(ids, 2):
        same_pred = a in predicted and b in predicted and predicted[a] == predicted[b]
        same_exp = expected[a] == expected[b]
        pred_pairs += same_pred
        exp_pairs += same_exp
        tp += same_pred and same_exp
    precision = tp / pred_pairs if pred_pairs else (1.0 if not exp_pairs else 0.0)
    recall = tp / exp_pairs if exp_pairs else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return PairwiseScore(precision, recall, f1)


def load_golden(path: str | Path) -> dict[str, str]:
    """Return {source_id: expected_theme} from a JSONL file with an `expected_theme` field."""
    expected = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            if "expected_theme" in obj:
                expected[str(obj["id"])] = str(obj["expected_theme"])
    return expected


def traceability(quotes: list[tuple[str, str]]) -> float:
    """Share of (quote, evidence_text) pairs whose quote is verbatim. 1.0 when empty."""
    if not quotes:
        return 1.0
    ok = sum(1 for q, text in quotes if quote_is_verbatim(q, text))
    return ok / len(quotes)
