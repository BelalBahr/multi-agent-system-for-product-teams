# Contributing

Thanks for your interest. This is an early, design-led project, so the most useful contributions
right now are design feedback, threat-model review and connectors.

## Ground rules

- **Safety defaults are not negotiable in a pull request.** Changes that widen what an agent can
  read or write, or that let source text act as instructions, need a written justification and a test.
- **Connectors never modify existing items in a source system.** Read, or create drafts in a space
  the team designates.
- **Every behavior change needs a test.** Connectors are tested against recorded or mocked API
  responses, never live APIs.
- **No real customer data** in tests, examples or issues. Use the synthetic data in `examples/demo`.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
pytest
```

## Writing a connector

Read [docs/connector-spec.md](docs/connector-spec.md). Subclass `BaseConnector`, declare `id`,
`category` and `scopes`, implement `fetch`, and add a test file that uses `httpx.MockTransport` or
fixtures. Document any API limits you know of, and say how you checked them.

## Pull requests

Keep them small and focused, explain the why, and note anything you could not verify. Be kind in
review. This project follows the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
