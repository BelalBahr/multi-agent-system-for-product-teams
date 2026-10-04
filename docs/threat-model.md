# Threat model (pre-alpha)

This tool reads customer tickets and transcripts and sends text to a language model. That is real
attack surface and real privacy exposure. This page says what is defended, how, and what is not.

## Assets

- Customer personal data inside tickets and transcripts
- API tokens for source systems and the model provider
- The integrity of themes, decisions and the audit trail that people rely on

## What the design defends against

| Threat | Defence | Where |
| --- | --- | --- |
| Prompt injection through ticket text | Evidence is passed as quoted data, closing-tag breakouts are neutralised, a fixed security preamble is appended to every prompt, and the agent has no write connector | `agents/synthesizer.py`, `policy.py` |
| Fabricated quotes | A quote is stored only if it appears verbatim in the evidence it cites. Rejections are logged | `agents/synthesizer.py` |
| Agent exceeding its remit | A scoped store checks every read and write against the agent's file | `policy.py` |
| Rewriting history | Database triggers reject updates and deletes on evidence | `store.py` |
| Untraceable changes | Every write records agent, run id and time | `store.py`, `policy.py` |
| Runaway cost | Per-run and rolling weekly token limits stop a run | `llm.py`, `runner.py` |
| An agent approving its own work | Agents receive a scoped store that has no approve or reject method. Gates are separate commands that record the human approver | `policy.py`, `gates.py` |
| Decisions without grounds | The database refuses to approve a decision with no evidence unless a person flags it as an explicit assumption. Cited ids are checked against ids the model was shown | `store.py`, `agents/strategist.py` |
| Dissent being softened | Red Team dissent is stored separately and the Strategist cannot write to it | `store.py`, `policy.py` |
| Invented metrics | The model only picks from a dictionary of events and pages. Numbers come from connectors | `dictionary.py`, `agents/analyst.py` |
| Fetching internal services through the web connector (SSRF) | Only public http/https on ports 80 and 443, every resolved address must be public (loopback, private, link-local and cloud-metadata ranges are refused), redirects are re-checked each hop, size and time limits. The Researcher agent itself holds no connector | `connectors/web.py` |
| A model inventing a figure in a status update, audience message or research finding | Any number must appear in the facts or source. Otherwise the output is dropped, or a plain template is used | `agents/common.py` and the three agents |
| Source system damage | Connectors have no modify scope. The Zendesk connector only sends GET requests, and a test asserts it | `connectors/` |

## What is NOT defended, and what to do about it

- **Redaction is pattern-based.** It removes emails, phone numbers, valid card numbers and IPv4
  addresses. It does **not** detect personal names, postal addresses, or identifiers specific to
  your product. Use `extra_patterns` in a connector subclass for known strings, and review a
  sample of stored evidence before enabling a live source.
- **The model provider receives redacted text.** Check that against your data policy. The
  framework sends no telemetry of its own.
- **Prompt injection is mitigated, not solved.** A hostile ticket can still try to skew which theme
  an item lands in. The effect is limited by design (no write connector, verified quotes, and a
  human gate before anything is decided), but the Synthesizer's grouping is model judgment.
- **The local database is not encrypted.** Protect the machine and the file. Anyone who can read
  it can read the redacted evidence.
- **Secrets.** The CLI reads tokens from environment variables. Use your operating system's secret
  store or a secret manager to set them, and do not commit them.
- **The web connector has a DNS-rebinding window.** It checks the addresses a host resolves to and
  then makes the request, so a hostile DNS server could answer differently the second time. Run the
  `ingest web` command on a machine or network that cannot reach internal services if that matters,
  and only list URLs you trust.
- **Fetched pages are untrusted and can be wrong.** Competitor pages are marketing. The Researcher
  records what a page claims, verbatim, and does not verify it. Pages may also change between
  scans: each fetch is stored as a new dated snapshot.
- **The figure check is numeric only.** It stops invented numbers, not invented words. A status
  update or audience message can still overstate or mis-emphasise, which is why a person approves
  it before anyone sees it.
- **Vendor connectors follow each vendor's documented API.** Confirm the response shapes on a
  throwaway project before giving them real credentials, and use the narrowest token each vendor offers.
- **A human can still approve a bad decision.** The gates make approval deliberate, attributable
  and informed (dissent and evidence on one page). They do not make the decision correct.
- **Metrics show association, not cause.** The Analyst and Outcome Tracker say so in their output,
  and a "met" verdict does not prove the decision caused the change.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
