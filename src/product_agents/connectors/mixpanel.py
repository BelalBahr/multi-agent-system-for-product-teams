"""Mixpanel product-analytics connector (read-only, query-style).

Uses the Query API's segmentation endpoint with a service account:
    GET {base}/api/query/segmentation?project_id=..&event=..&from_date=..&to_date=..&unit=day

Written against Mixpanel's documented Query API. Check the response shape on your project.
"""

from __future__ import annotations

from datetime import date
from typing import Iterator

import httpx

from ..models import RawRecord
from .base import BaseConnector

REGIONS = {
    "us": "https://mixpanel.com",
    "eu": "https://eu.mixpanel.com",
    "in": "https://in.mixpanel.com",
}


class MixpanelConnector(BaseConnector):
    id = "mixpanel"
    category = "product-analytics"
    scopes = ("read",)

    def __init__(
        self,
        project_id: str,
        username: str,
        secret: str,
        region: str = "us",
        client: httpx.Client | None = None,
    ):
        super().__init__()
        if region not in REGIONS:
            raise ValueError(f"region must be one of {sorted(REGIONS)}")
        self.project_id = project_id
        self.base = REGIONS[region]
        self.client = client or httpx.Client(auth=(username, secret), timeout=60.0)

    def event_count(self, event: str, start: date, end: date) -> int:
        """Total occurrences of `event` from `start` to `end` inclusive."""
        resp = self.client.get(
            f"{self.base}/api/query/segmentation",
            params={
                "project_id": self.project_id,
                "event": event,
                "from_date": start.isoformat(),
                "to_date": end.isoformat(),
                "unit": "day",
                "type": "general",
            },
        )
        resp.raise_for_status()
        values = resp.json().get("data", {}).get("values", {})
        return int(sum(sum(per_day.values()) for per_day in values.values()))

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        """Analytics are queried, not streamed. Use `event_count`."""
        return iter(())
