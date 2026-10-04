# Connector specification (version 0, unstable)

A connector is the only place a vendor's API appears. It belongs to a category, declares what it
can do, and returns normalized records. Agents never see vendor-specific payloads.

## Interface

```python
class Connector(Protocol):
    id: str                    # stored as the evidence source type, and used for the cursor
    category: str              # support | product-analytics | behavior-analytics | voice | ...
    scopes: tuple[str, ...]    # "read", or "read" plus "draft-write"

    def fetch(self, since: str | None) -> Iterator[RawRecord]: ...
    def redact(self, text: str) -> str: ...
```

`fetch` yields records newer than `since` (ISO 8601, UTC), oldest first. `redact` runs on every
record before it reaches the store. `BaseConnector` in `product_agents.connectors.base` supplies a
default `redact` and validates scopes.

## RawRecord

| Field | Meaning |
| --- | --- |
| `source_id` | The record's id in the source system |
| `source_url` | A link a person can open to verify the original |
| `timestamp` | ISO 8601, UTC |
| `text` | The content, not yet redacted |
| `segment` | Free-text grouping such as plan or tags |
| `metric` | Optional dict for analytics connectors |

The pipeline turns a `RawRecord` into an `Evidence` record with id `hash(connector id, source id)`.
Evidence is immutable, so a source record that changes later is not re-imported in this version.

## Rules

- **No scope exists for modifying existing items.** A connector may only read, or create drafts in
  a space the team designates. The ClickUp connector uses `draft-write`: it can create one task in
  one designated list and nothing else.
- **Read-only credentials wherever the vendor allows.**
- **Be polite to the API.** Honour rate limits and `Retry-After`. The Zendesk connector shows the pattern.
- **Test against recorded fixtures,** not the live API. See `tests/test_connectors.py`.

## Query connectors and draft writers

Analytics sources are queried rather than streamed, so those connectors expose a query method
instead of a useful `fetch` (which returns nothing). The Analyst and Outcome Tracker call them
directly, and the numbers are stored as `metric` evidence, never typed by a model.

| Method | Connector |
| --- | --- |
| `event_count(event, start, end) -> int` | Mixpanel |
| `page_friction(url_contains, days) -> dict or None` | Clarity |
| `create_draft(title, markdown) -> url` | ClickUp writer (scope `draft-write`) |
| `list_tasks(list_id) -> list[Task]` | ClickUp reader (scope `read`, id `clickup-read`) |

A draft writer can create one kind of item in one designated place and nothing else. The ClickUp
connector's tests assert that it only ever sends a single POST to the configured list.

## Shipped connectors

| Connector | Category | Notes |
| --- | --- | --- |
| `zendesk` | support | Incremental ticket export, API token auth, GET only, skips deleted tickets |
| `jsonl` | support | One JSON object per line with `id`, `timestamp`, `text`, optional `segment`, `url` |
| `folder` | voice | Each `.txt` or `.md` file is one record, such as an interview transcript |
| `mixpanel` | product-analytics | Query API segmentation, service-account auth. Confirm the response shape on your project |
| `clarity` | behavior-analytics | Data Export API live insights. Parsing follows the documented shape and returns nothing rather than guessing, so confirm it on your project. Rate-limited, recent days only |
| `clickup` | work-tracker | Creates a draft task in one list |
| `clickup-read` | work-tracker | Lists open tasks in named lists. Read-only |
| `web` | research | Fetches pages from a list of URLs you supply, as dated snapshots. http/https only, ports 80 and 443, refuses non-public addresses at every redirect hop, size and time limits. **A DNS-rebinding race remains: see the threat model** |

Check each vendor's current API limits and response shapes before relying on them.
