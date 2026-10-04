# Agent definition format (version 0, unstable)

An agent is a YAML file. The policy engine enforces it: an agent cannot read or write a record
type, or hold a connector, that its file does not list.

```yaml
name: signal-synthesizer
role: Cluster customer evidence into themes with counts, trend and verbatim quotes
tier: A                      # A acts alone, B drafts for approval, C advises only
reads: [evidence, themes]
writes: [themes]
connectors: []
reads_untrusted: true
prompt: ../prompts/signal-synthesizer.md
budget:
  max_tokens_per_run: 200000
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique agent name, used in the audit log |
| `role` | yes | One line describing the job |
| `tier` | yes | `A` acts alone, `B` drafts for approval, `C` advises only |
| `reads` | no | Record types the agent may read |
| `writes` | no | Record types the agent may write |
| `connectors` | no | Connectors the agent may call. Write scopes are written `name:draft-write` |
| `reads_untrusted` | no | `true` if the agent reads raw customer text. Such an agent may not hold a write connector |
| `prompt` | no | Path to the prompt file, relative to this file |
| `budget.max_tokens_per_run` | no | Hard stop for one run |

Record types: `strategy`, `evidence`, `themes`, `decisions`, `outcomes`, `dissent`, `specs`, `updates`, `research`.

Shipped agents: `signal-synthesizer`, `analyst`, `strategist`, `red-team`, `spec-writer`, `outcome-tracker`, `delivery-coordinator`, `stakeholder-comms`, `researcher`.

## Rules checked when a file is loaded

- `tier` must be A, B or C.
- Tier C agents may write only `dissent`. The Red Team is tier C.
- Unknown record types are rejected.
- A `reads_untrusted` agent with a write connector is rejected.

## Rules the framework adds on top

The framework appends a fixed security preamble to every prompt that tells the model to treat
evidence text as data. A team cannot remove it by editing the prompt file. Install-wide limits,
such as the weekly token cap, live in `policy.yaml` and override anything an agent file says.

## Connectors and access

A connector the agent uses (for example `mixpanel`) must appear in `connectors`, or constructing
the agent fails with a policy violation. Approval is not an agent capability at all: the store handle
an agent receives has no approve or reject method. Gates are separate human commands.

## Output checks the framework applies in code

Independent of the prompt, so editing a prompt cannot switch them off: a quote is kept only if it is
verbatim in its source; cited evidence ids must be ones the model was shown; any figure in a status
update, audience message or research finding must appear in the facts or source it came from. An
agent that fails a check has that output dropped, or replaced by a plain template, and the event is
logged.

## Not implemented yet

`schedule` (use `product-agents run weekly` from your own scheduler), and per-role gate routing.
