---
status: accepted
---

# One canonical knowledge database with recoverable automatic migration

Agent Team's runtime opens a root-level knowledge database while its s6 initializer
creates a nested one, so existing installations can have two histories. Use
`<state_dir>/knowledge/knowledge.db` as the canonical database and automatically
resolve valid legacy-only, empty-peer, or identical-history cases under the runtime
lease, preserving displaced databases in durable archives. Divergent histories or
unprovable recovery stop startup: silently selecting a path or merging duplicate
record IDs could hide historical knowledge, while requiring manual migration for
every installation would penalize states produced by our own initialization code.

The [migration contract](../knowledge-database-migration.md) specifies selection,
preservation, recovery, diagnostics, and the implementation acceptance gate.
This records a design decision; runtime implementation remains a subsequent effort.

Sources: [issue #3](https://github.com/MyCookie/pydantic-deep-swarm/issues/3) and
the prerequisite [path-authority decision #2](https://github.com/MyCookie/pydantic-deep-swarm/issues/2).
Number 0001 is reserved by that prerequisite's referenced configuration ADR, which
is absent from this checkout.
