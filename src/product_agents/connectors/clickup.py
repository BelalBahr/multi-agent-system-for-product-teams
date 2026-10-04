"""ClickUp work-tracker connectors.

Two deliberately separate classes:
  * ClickUpDraftWriter (scope draft-write): can only create a task in one designated drafts list.
  * ClickUpReader (scope read): reads open tasks from the lists the team names.
Neither can edit, move, assign or delete anything. A person promotes a draft to real work.

    POST https://api.clickup.com/api/v2/list/{list_id}/task      (writer)
    GET  https://api.clickup.com/api/v2/list/{list_id}/task      (reader)
    Authorization: <personal API token>

Written from ClickUp's API documentation. Verify against your workspace before relying on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import httpx

from ..models import RawRecord
from .base import BaseConnector

API = "https://api.clickup.com/api/v2"


class ClickUpDraftWriter(BaseConnector):
    id = "clickup"
    category = "work-tracker"
    scopes = ("draft-write",)

    def __init__(self, token: str, drafts_list_id: str, client: httpx.Client | None = None):
        super().__init__()
        if not drafts_list_id:
            raise ValueError("a designated drafts list id is required")
        self.drafts_list_id = drafts_list_id
        self.client = client or httpx.Client(headers={"Authorization": token}, timeout=30.0)

    def create_draft(self, title: str, markdown: str) -> str:
        """Create a draft task and return its URL."""
        resp = self.client.post(
            f"{API}/list/{self.drafts_list_id}/task",
            json={"name": title, "markdown_description": markdown, "tags": ["agent-draft"]},
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("url") or f"https://app.clickup.com/t/{data['id']}")

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        """This connector writes drafts only."""
        return iter(())


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    status: str
    status_type: str  # open | custom | done | closed
    due: datetime | None
    updated: datetime | None
    assignees: tuple[str, ...]
    tags: tuple[str, ...]
    url: str


def _ms(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class ClickUpReader(BaseConnector):
    id = "clickup-read"
    category = "work-tracker"
    scopes = ("read",)

    def __init__(self, token: str, client: httpx.Client | None = None, max_pages: int = 20):
        super().__init__()
        self.client = client or httpx.Client(headers={"Authorization": token}, timeout=30.0)
        self.max_pages = max_pages

    def list_tasks(self, list_id: str) -> list[Task]:
        tasks: list[Task] = []
        for page in range(self.max_pages):
            resp = self.client.get(
                f"{API}/list/{list_id}/task",
                params={"include_closed": "false", "subtasks": "true", "page": page},
            )
            resp.raise_for_status()
            data = resp.json()
            for t in data.get("tasks") or []:
                status = t.get("status") or {}
                tasks.append(
                    Task(
                        id=str(t["id"]),
                        name=str(t.get("name") or ""),
                        status=str(status.get("status") or ""),
                        status_type=str(status.get("type") or ""),
                        due=_ms(t.get("due_date")),
                        updated=_ms(t.get("date_updated")),
                        assignees=tuple(str(a.get("username") or "") for a in t.get("assignees") or []),
                        tags=tuple(str(g.get("name") or "") for g in t.get("tags") or []),
                        url=str(t.get("url") or f"https://app.clickup.com/t/{t['id']}"),
                    )
                )
            if data.get("last_page", True) or not data.get("tasks"):
                break
        return tasks

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        """Tasks are read with `list_tasks`, not streamed as evidence."""
        return iter(())
