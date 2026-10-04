"""Zendesk support connector (read-only).

Uses the incremental ticket export, authenticated with an API token:
    GET https://{subdomain}.zendesk.com/api/v2/incremental/tickets.json?start_time=<unix>
This connector never writes to Zendesk.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

import httpx

from ..models import RawRecord
from .base import BaseConnector


def _to_unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


class ZendeskConnector(BaseConnector):
    id = "zendesk"
    category = "support"
    scopes = ("read",)

    def __init__(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        client: httpx.Client | None = None,
        default_lookback_days: int = 30,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ):
        super().__init__()
        self.subdomain = subdomain
        self.base = f"https://{subdomain}.zendesk.com"
        self.default_lookback_days = default_lookback_days
        self.sleep = sleep
        self.max_retries = max_retries
        self.client = client or httpx.Client(
            auth=(f"{email}/token", api_token), timeout=30.0
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            resp = self.client.get(url, params=params)
            if resp.status_code == 429 and attempt < self.max_retries:
                self.sleep(float(resp.headers.get("Retry-After", "5")))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        if since:
            start = _to_unix(since)
        else:
            start = int(
                (datetime.now(timezone.utc) - timedelta(days=self.default_lookback_days)).timestamp()
            )
        url = f"{self.base}/api/v2/incremental/tickets.json"
        params: dict | None = {"start_time": start}
        while True:
            data = self._get(url, params)
            for t in data.get("tickets", []):
                if t.get("status") == "deleted":
                    continue
                subject = (t.get("subject") or "").strip()
                body = (t.get("description") or "").strip()
                text = f"{subject}\n\n{body}".strip()
                if not text:
                    continue
                yield RawRecord(
                    source_id=str(t["id"]),
                    source_url=f"{self.base}/agent/tickets/{t['id']}",
                    timestamp=t.get("updated_at") or t["created_at"],
                    text=text,
                    segment=",".join(t.get("tags") or []),
                )
            if data.get("end_of_stream", True) or not data.get("next_page"):
                return
            url, params = data["next_page"], None
