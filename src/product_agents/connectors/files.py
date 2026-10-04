"""File-based connectors: JSONL records and a folder of transcripts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..models import RawRecord
from .base import BaseConnector


def _after(ts: str, since: str | None) -> bool:
    return since is None or ts > since


class JsonlConnector(BaseConnector):
    """One JSON object per line: id, timestamp, text, and optionally segment and url."""

    category = "support"

    def __init__(self, path: str | Path, source_type: str = "jsonl"):
        super().__init__()
        self.path = Path(path)
        self.id = source_type

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        rows = []
        with self.path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rows.append(
                        RawRecord(
                            source_id=str(obj["id"]),
                            source_url=str(obj.get("url") or f"{self.path.name}#{obj['id']}"),
                            timestamp=str(obj["timestamp"]),
                            text=str(obj["text"]),
                            segment=str(obj.get("segment", "")),
                        )
                    )
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ValueError(f"{self.path}:{line_no}: bad record ({exc})") from exc
        for rec in sorted(rows, key=lambda r: r.timestamp):
            if _after(rec.timestamp, since):
                yield rec


class FolderConnector(BaseConnector):
    """Each .txt or .md file in a folder is one record, such as an interview transcript."""

    category = "voice"

    def __init__(self, path: str | Path, source_type: str = "folder"):
        super().__init__()
        self.path = Path(path)
        self.id = source_type

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        records = []
        for file in sorted(self.path.iterdir()):
            if file.suffix.lower() not in (".txt", ".md") or not file.is_file():
                continue
            ts = datetime.fromtimestamp(file.stat().st_mtime, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            records.append(
                RawRecord(
                    source_id=file.name,
                    source_url=file.resolve().as_uri(),
                    timestamp=ts,
                    text=file.read_text(encoding="utf-8"),
                    segment="voice",
                )
            )
        for rec in sorted(records, key=lambda r: r.timestamp):
            if _after(rec.timestamp, since):
                yield rec
