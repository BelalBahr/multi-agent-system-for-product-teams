"""Workspace configuration and connector construction from environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

import yaml

from .connectors import (
    ClarityConnector,
    ClickUpDraftWriter,
    ClickUpReader,
    FolderConnector,
    JsonlConnector,
    MixpanelConnector,
    WebPagesConnector,
    ZendeskConnector,
)
from .policy import AgentDef, load_agent, load_policy

AGENT_FILES = {
    "signal-synthesizer": "signal-synthesizer.yaml",
    "analyst": "analyst.yaml",
    "strategist": "strategist.yaml",
    "red-team": "red-team.yaml",
    "spec-writer": "spec-writer.yaml",
    "outcome-tracker": "outcome-tracker.yaml",
    "delivery-coordinator": "delivery-coordinator.yaml",
    "stakeholder-comms": "stakeholder-comms.yaml",
    "researcher": "researcher.yaml",
}

TEMPLATE_FILES = (
    "policy.yaml",
    "events.yaml",
    "strategy.md",
    "sources.yaml",
    "audiences.yaml",
    *[f"agents/{f}" for f in AGENT_FILES.values()],
    *[f"prompts/{n}.md" for n in AGENT_FILES],
)


def agent(root: Path, name: str) -> AgentDef:
    return load_agent(root / "agents" / AGENT_FILES[name])


def policy(root: Path):
    return load_policy(root / "policy.yaml")


def mixpanel_from_env(env: Mapping[str, str] = os.environ) -> MixpanelConnector | None:
    if not (env.get("MIXPANEL_PROJECT_ID") and env.get("MIXPANEL_SA_USERNAME") and env.get("MIXPANEL_SA_SECRET")):
        return None
    return MixpanelConnector(
        env["MIXPANEL_PROJECT_ID"], env["MIXPANEL_SA_USERNAME"], env["MIXPANEL_SA_SECRET"],
        region=env.get("MIXPANEL_REGION", "us"),
    )


def clarity_from_env(env: Mapping[str, str] = os.environ) -> ClarityConnector | None:
    return ClarityConnector(env["CLARITY_API_TOKEN"]) if env.get("CLARITY_API_TOKEN") else None


def clickup_from_env(env: Mapping[str, str] = os.environ) -> ClickUpDraftWriter | None:
    if not (env.get("CLICKUP_API_TOKEN") and env.get("CLICKUP_DRAFTS_LIST_ID")):
        return None
    return ClickUpDraftWriter(env["CLICKUP_API_TOKEN"], env["CLICKUP_DRAFTS_LIST_ID"])


def clickup_reader_from_env(env: Mapping[str, str] = os.environ) -> tuple[ClickUpReader, list[str]] | None:
    """The read-only tracker connector plus the list ids to watch, or None if not configured."""
    ids = [i.strip() for i in env.get("CLICKUP_BACKLOG_LIST_IDS", "").split(",") if i.strip()]
    if not (env.get("CLICKUP_API_TOKEN") and ids):
        return None
    return ClickUpReader(env["CLICKUP_API_TOKEN"]), ids


def read_url_file(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def zendesk_from_env(env: Mapping[str, str] = os.environ) -> ZendeskConnector | None:
    if not (env.get("ZENDESK_SUBDOMAIN") and env.get("ZENDESK_EMAIL") and env.get("ZENDESK_API_TOKEN")):
        return None
    return ZendeskConnector(env["ZENDESK_SUBDOMAIN"], env["ZENDESK_EMAIL"], env["ZENDESK_API_TOKEN"])


def load_sources(root: Path, env: Mapping[str, str] = os.environ) -> list:
    """Connectors named in sources.yaml. Missing credentials raise a clear error."""
    path = root / "sources.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for entry in raw.get("sources") or []:
        kind = entry.get("type")
        if kind == "zendesk":
            conn = zendesk_from_env(env)
            if conn is None:
                raise RuntimeError("sources.yaml lists zendesk but ZENDESK_* variables are not set")
            out.append(conn)
        elif kind == "jsonl":
            out.append(JsonlConnector(root / entry["path"]))
        elif kind == "folder":
            out.append(FolderConnector(root / entry["path"]))
        elif kind == "web":
            out.append(WebPagesConnector(read_url_file(root / entry["urls_file"])))
        else:
            raise RuntimeError(f"unknown source type '{kind}' in sources.yaml")
    return out
