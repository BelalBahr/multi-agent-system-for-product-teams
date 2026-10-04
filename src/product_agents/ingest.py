"""Pull records from a connector, redact them, and store them as Evidence."""

from __future__ import annotations

from dataclasses import dataclass

from .connectors.base import Connector
from .models import Evidence, make_id
from .store import Store


@dataclass
class IngestResult:
    fetched: int = 0
    new: int = 0
    duplicates: int = 0


def ingest(connector: Connector, store: Store, since: str | None = None) -> IngestResult:
    cursor_key = f"cursor:{connector.id}"
    start = since if since is not None else store.get_meta(cursor_key)
    result = IngestResult()
    latest = start
    for rec in connector.fetch(start):
        result.fetched += 1
        evidence = Evidence(
            id=make_id(connector.id, rec.source_id),
            source_type=connector.id,
            source_id=rec.source_id,
            source_url=rec.source_url,
            timestamp=rec.timestamp,
            segment=rec.segment,
            text=connector.redact(rec.text),
            metric=rec.metric,
        )
        if store.add_evidence(evidence):
            result.new += 1
            store.log(f"connector:{connector.id}", "ingest", "evidence", evidence.id)
        else:
            result.duplicates += 1
        if latest is None or rec.timestamp > latest:
            latest = rec.timestamp
    if latest is not None:
        store.set_meta(cursor_key, latest)
    return result
