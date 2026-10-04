"""Weekly digest: rising themes with counts, trend, verified quotes and source links."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Theme
from .store import Store


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass
class ThemeStats:
    theme: Theme
    current: int
    previous: int
    total: int
    first_seen: datetime | None

    @property
    def trend(self) -> int:
        return self.current - self.previous


def theme_stats(store: Store, now: datetime, window_days: int) -> list[ThemeStats]:
    cur_start = now - timedelta(days=window_days)
    prev_start = now - timedelta(days=2 * window_days)
    stats = []
    for theme in store.list_themes():
        evidence = [e for e in store.theme_evidence(theme.id) if e.kind == "text"]
        stamps = [_parse(e.timestamp) for e in evidence]
        current = sum(1 for s in stamps if cur_start < s <= now)
        previous = sum(1 for s in stamps if prev_start < s <= cur_start)
        stats.append(
            ThemeStats(theme, current, previous, len(stamps), min(stamps) if stamps else None)
        )
    return stats


def _trend_label(s: ThemeStats, now: datetime, window_days: int) -> str:
    if s.previous == 0 and s.first_seen and s.first_seen > now - timedelta(days=window_days):
        return "new this period"
    if s.trend > 0:
        return f"up {s.trend}"
    if s.trend < 0:
        return f"down {-s.trend}"
    return "flat"


def build_digest(
    store: Store, now: datetime | None = None, window_days: int = 7, top_n: int = 5
) -> str:
    now = now or datetime.now(timezone.utc)
    stats = [s for s in theme_stats(store, now, window_days) if s.current > 0]
    stats.sort(key=lambda s: (s.current, s.trend, s.total), reverse=True)

    lines = [
        f"# Weekly product signal digest",
        "",
        f"Window: {(now - timedelta(days=window_days)).date()} to {now.date()} "
        f"({window_days} days), compared with the {window_days} days before.",
        "",
    ]
    sources = store.evidence_sources()
    total = store.count_evidence()
    unassigned = store.count_unassigned()
    lines.append(
        "Sources: "
        + (", ".join(f"{k} ({v})" for k, v in sorted(sources.items())) or "none yet")
        + f". Evidence total: {total}. Not yet in a theme: {unassigned}."
    )
    lines.append(
        "Blind spot: this digest only reflects customers who contact support or take part "
        "in research. Quiet users are not represented."
    )
    lines.append("")
    if not stats:
        lines.append("No themes have new evidence in this window.")
        return "\n".join(lines) + "\n"

    lines.append(f"## Top {min(top_n, len(stats))} themes this period")
    lines.append("")
    for i, s in enumerate(stats[:top_n], 1):
        t = s.theme
        lines.append(f"### {i}. {t.title}")
        if t.summary:
            lines.append(t.summary)
        lines.append("")
        lines.append(
            f"- This period: {s.current} (previous: {s.previous}, {_trend_label(s, now, window_days)})"
            f" | All time: {s.total} | Confidence: {t.confidence}"
        )
        quotes = store.theme_quotes(t.id)
        if quotes:
            for quote, ev in quotes[:3]:
                lines.append(f'- "{quote}" ([source]({ev.source_url}))')
        else:
            lines.append("- No verified quotes. Treat this theme as unconfirmed.")
        for ev in [e for e in store.theme_evidence(t.id) if e.kind == "metric"]:
            lines.append(f"- Context ({ev.source_type}): {ev.text}")
        if t.confidence == "low":
            lines.append("- Flag: low confidence, check the underlying evidence before acting.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
