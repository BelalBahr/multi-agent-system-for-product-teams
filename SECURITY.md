# Security policy

This project handles customer data and calls a language model, so security reports matter.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability. Use GitHub's private vulnerability
reporting on this repository (Security tab, "Report a vulnerability").

Helpful reports include what you found, the steps to reproduce it, and the impact you expect. Please give us
a reasonable time to respond before disclosing publicly.

## Scope

In scope: the policy engine, redaction, connectors, evidence immutability, prompt-injection
handling, and anything that could leak or alter stored data.

Known limitations are listed in [docs/threat-model.md](docs/threat-model.md). Reports about
unredacted personal names, for example, are already known.

## Supported versions

Only the latest pre-release receives fixes.
