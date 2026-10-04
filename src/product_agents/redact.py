"""Redaction of personal data before anything reaches the store.

This is pattern-based and deliberately conservative. It removes emails, phone
numbers, payment card numbers and IP addresses. It does NOT detect personal
names or street addresses: teams that need that should pass `extra_patterns`
or add a connector-specific hook. See docs/threat-model.md.
"""

from __future__ import annotations

import re
from typing import Iterable

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_CARD = re.compile(r"(?<![\d])(?:\d[ -]?){13,19}(?![\d])")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().\-]{7,}\d(?![\w])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _sub_cards(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[CARD]"
        return m.group(0)

    return _CARD.sub(repl, text)


def _sub_phones(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        # 9 to 15 digits is a plausible phone number; shorter or longer runs
        # are usually ids, order numbers or timestamps.
        if 9 <= len(digits) <= 15:
            return "[PHONE]"
        return m.group(0)

    return _PHONE.sub(repl, text)


def redact_text(text: str, extra_patterns: Iterable[str] = ()) -> str:
    """Return `text` with personal data replaced by placeholders."""
    out = _EMAIL.sub("[EMAIL]", text)
    out = _sub_cards(out)
    out = _IPV4.sub("[IP]", out)
    out = _sub_phones(out)
    for pattern in extra_patterns:
        out = re.sub(pattern, "[REDACTED]", out)
    return out
