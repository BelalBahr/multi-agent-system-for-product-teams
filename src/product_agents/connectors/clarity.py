"""Microsoft Clarity behavior-analytics connector (read-only, query-style).

Uses the Data Export API's live-insights endpoint:
    GET https://www.clarity.ms/export-data/api/v1/project-live-insights?numOfDays=N&dimension1=URL
    Authorization: Bearer <token>

Written from Clarity's documentation. The export API is rate-limited and covers only the last
few days, so check the current limits before scheduling it. Parsing is defensive: if the
response does not look as expected the connector returns None rather than guessing.
"""

from __future__ import annotations

from typing import Iterator

import httpx

from ..models import RawRecord
from .base import BaseConnector

ENDPOINT = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
FRICTION_METRICS = ("DeadClickCount", "RageClickCount", "QuickbackClick", "ExcessiveScroll", "ScriptErrorCount")


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class ClarityConnector(BaseConnector):
    id = "clarity"
    category = "behavior-analytics"
    scopes = ("read",)

    def __init__(self, api_token: str, client: httpx.Client | None = None):
        super().__init__()
        self.client = client or httpx.Client(
            headers={"Authorization": f"Bearer {api_token}"}, timeout=60.0
        )

    def page_friction(self, url_contains: str, days: int = 3) -> dict[str, float] | None:
        """Share of sessions (percent) with each friction signal on pages whose URL contains
        `url_contains`, weighted by session count. None if nothing matched."""
        resp = self.client.get(ENDPOINT, params={"numOfDays": days, "dimension1": "URL"})
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            return None
        out: dict[str, float] = {}
        sessions_total = 0.0
        for block in payload:
            name = block.get("metricName")
            if name not in FRICTION_METRICS:
                continue
            weight = pct_sum = 0.0
            for row in block.get("information") or []:
                if url_contains not in str(row.get("URL", "")):
                    continue
                sessions = _num(row.get("sessionsCount") or row.get("totalSessionCount"))
                pct_sum += _num(row.get("sessionsWithMetricPercentage")) * sessions
                weight += sessions
            if weight:
                out[name] = round(pct_sum / weight, 1)
                sessions_total = max(sessions_total, weight)
        if not out:
            return None
        out["sessions"] = sessions_total
        return out

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        """Analytics are queried, not streamed. Use `page_friction`."""
        return iter(())
