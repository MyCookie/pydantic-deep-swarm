# Agent Team documentation

This directory is the authoritative reader-facing documentation for the Agent
Team repository. The root [README](../README.md) is a short entry point; detailed
architecture, runtime behavior, operations, and testing guidance live here.

## Start here

- [End-to-end workflow](end-to-end-workflow.md) — follows a request from the
  Hermes Principal or HTTP API through planning, workers, verification,
  persistence, and the final response.
- [Architecture](architecture.md) — components, typed boundaries, trust model,
  process ownership, and durable state.
- [Operations](operations.md) — configuration, startup, bootstrap, health,
  control-plane commands, and API use.
- [Testing](testing.md) — deterministic suites, live E2E opt-in, HTTP test
  transports, and release checks.
- [GitOps runbook](gitops-runbook.md) — exact-revision promotion, Pi asset
  reconciliation, rollback, and operational recovery.

## Colocated specifications

Some documentation remains beside the schema or package it governs so it can be
validated and distributed with that component:

- [Pi package](../pi/README.md)
- [Agent Team delegation skill](../pi/skills/agent-team-delegation/SKILL.md)
- [Principal handoff template](../pi/prompts/principal-handoff.md)
- [Hermes Principal plugin](../integrations/hermes/principal-agent-team/README.md)
- [Git provenance schema and policy](../provenance/README.md)

Those files are component specifications, not competing top-level architecture
or status documents.

## Source-of-truth order

When prose and behavior disagree, use this order:

1. Typed contracts and runtime code under `src/agent_team/`.
2. Executable acceptance tests under `tests/`.
3. The documents in this directory.
4. Historical provenance records, which describe only the candidate captured by
   each record and are not evidence for the current worktree.

Point-in-time “complete,” “progress,” and “final status” files are intentionally
not maintained. Current status must be established from an exact revision and
fresh verification output.

## License

The Agent Team runtime and repository documentation are distributed under
[GNU AGPLv3](../LICENSE), version 3 only. The separately scoped first-party Pi
assets remain available under the [MIT License](../pi/LICENSE).
