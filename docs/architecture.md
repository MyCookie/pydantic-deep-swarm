# Agent Team architecture

## System purpose

Agent Team is a persistent local orchestration service. It separates user-facing
reasoning, project planning, bounded specialist execution, evidence validation,
and durable state through typed boundaries.

The complete request lifecycle is documented in the
[end-to-end workflow](end-to-end-workflow.md).

## Component map

```text
        External client
                 │
                 │ message or typed ProjectBrief
                 ▼
        FastAPI HTTP service
                 │
                 ├── durable SessionStore
                 └── AgentTeamEngine
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
      Agent Team Principal       Team Manager
                                     │
                                     ▼
                            bounded worker DAG
                                     │
                 ┌───────────────────┼───────────────────┐
                 ▼                   ▼                   ▼
           artifact store       project store      memory/knowledge
```

The caller is outside this architecture boundary. Its identity, interface, and
user-facing routing are implementation details of the invoking system.

## Model roles

The runtime supports separate OpenAI-compatible model configuration for:

- `principal` — direct answers, intake decisions, and final response rendering;
- `manager` — staffing, task decomposition, and bounded report repair;
- `worker` — specialist execution;
- `curator` — optional post-run durable knowledge extraction.

All roles may point to one local endpoint or to different endpoints. Model names
and base URLs are configuration, not orchestration state.

## Typed boundaries

The contracts in [`src/agent_team/contracts`](../src/agent_team/contracts/__init__.py)
are the architecture boundary:

- `PrincipalDecision` prevents an incomplete delegation action.
- [`ProjectBrief`](project-brief.md) defines the objective and uses stable
  requirement and acceptance-criterion IDs.
- `ManagerPlan` validates task, role, tool, and dependency identities.
- `WorkerOutput` excludes runtime-owned worker identity and process state.
- `WorkerResult` is the compact internal worker handoff.
- `CompletionReport` carries only bounded results, artifacts, findings, risks,
  unresolved items, and recommended next actions.

Raw model reasoning and worker transcripts are not contract fields.

## HTTP boundary

The service is implemented by [`app.py`](../src/agent_team/app.py).

| Method and route | Purpose |
| --- | --- |
| `POST /sessions` | Create or reuse a durable session by client key |
| `POST /sessions/{id}/messages` | Run normal Agent Team Principal intake |
| `POST /sessions/{id}/delegations` | Execute an already-authored typed brief without second Principal intake |
| `POST /sessions/{id}/reset` | Cancel active work and clear durable conversation/project fields |
| `POST /sessions/{id}/cancel` | Cancel the active project tree |
| `GET /sessions/{id}` | Read compact session status |
| `GET /sessions/{id}/report` | Read the latest durable completion report |
| `GET /health` | Process liveness response |
| `GET /ready` | Runtime/configuration/state readiness |
| `GET /models` | Inspect configured role models and endpoints |

The HTTP boundary supports one optional shared Bearer token from
`AGENT_TEAM_API_TOKEN`. When it is empty or unset, existing anonymous local mode
is preserved. When nonempty, middleware protects every API route plus generated
OpenAPI/docs routes and rejects unauthorized requests before runtime
initialization. Shared or non-loopback use requires the token and transport
security from TLS or a trusted terminating proxy. This is service authentication,
not per-session or multi-tenant authorization.

## Runtime process model

The supported model is one Agent Team process per state directory, started
directly or through an optional supervisor.
Startup acquires a nonblocking operating-system lease at
`<state_dir>/runtime.lock`. A second process fails closed instead of sharing
in-memory registries, process ownership, or cancellation state.

Durable sessions survive restart. Live tasks do not: startup converts stale
`processing` sessions to `blocked` with a restart error. This keeps recovery
honest and avoids fabricated completion.

## Optional s6 boundary

Core `init`, `serve`, `doctor`, model discovery, and swarm status use loaded
configuration and HTTP state without requiring s6 assets or environment variables.
`s6-service/` remains the canonical location for optional deployment templates;
installed live copies belong to the deployment, outside the checkout.

The s6 controller and supervised `bootstrap` workflow remain compatibility
surfaces. `AGENT_TEAM_SUPERVISOR=none|s6` selects
the adapter explicitly. Without that setting, a nonempty
`AGENT_TEAM_SERVICE_DIR` selects legacy s6 mode; otherwise supervision is
`not_configured`. Finding s6 executables never selects the adapter. Explicit
`none` overrides inherited service settings.

Foreground serve freezes effective configuration before startup. Its lifespan
acquires the sole lease before endpoint validation, canonical knowledge
preparation, recovery, retention, and engine construction. App, engine, and
retention share the prepared knowledge store. The owner drains and closes these
resources before releasing ownership; HTTP requests never initialize a missing
or stopping runtime. Originating-generation guards protect managed commits.
Knowledge preparation reports its selected source, verification, preserved
history count and digest, recovery/upgrade status, and archive or journal guidance
before retention. These preparation counts are separate from records later removed
by retention. Disabled knowledge reports that selection without accessing databases.

Missing optional supervision is not drift. Selected but unavailable supervision
is a visible failure. Core status compares effective role models and the API;
source defaults and runfiles are not model authority. `OwnedSwarmManager`
reconciles the selected external YAML in core mode: it locks, rereads, saves a
backup, and atomically commits the whole configuration before optional activation.
An injected restart adapter may activate that committed configuration. Missing or
failed requested activation remains pending; it does not undo persistence or claim
verification. Dry runs create no locks or files and never restart. Verification
requires observed runtime convergence; skipping restart or verification leaves the
corresponding activation outcome pending.

## Scheduling and lifecycle

The engine owns:

- a task and worker registry;
- a tracked subprocess manager;
- hard cancellation;
- worker and project deadlines;
- a concurrency limiter;
- per-worker model/tool turn budgets;
- active project and root-task maps.

A `ManagerPlan` is executed as dependency-ready layers. Independent tasks in one
layer run concurrently; dependent tasks receive compact summaries from completed
prerequisites. Cleanup is idempotent and project-scoped.

## Worker capability boundary

Workers do not inherit Hermes terminal, skills, MCP, connectors, or gateway
access. Their complete builtin surface is `list_files`, `read_file`, and
`write_file`, filtered again per assignment.

Path operations stay beneath the configured workspace. Local file artifacts are
atomically written and verified by path, size, and SHA-256. Workers cannot spawn
commands, Python, Git, shells, or arbitrary subprocesses. Command execution may
return only through a future separately authorized sandbox bridge; it is not a
configurable worker capability in this runtime.

Pi and bootstrap probe subprocesses receive credential-scrubbed environments.
Credential-shaped content is also redacted before prompts, tool evidence,
reports, logs, sessions, project checkpoints, artifact manifests, memory, and
shared knowledge are persisted or returned.

## Durable state

Mutable state is external to the checkout:

```text
AGENT_TEAM_STATE_DIR/
├── config/
├── sessions/
├── checkpoints/
├── memory/
├── knowledge/knowledge.db
├── projects/
├── artifacts/
└── logs/
```

The runtime rejects state or workspace paths that resolve inside the source
checkout. Session and project JSON writes use atomic replacement. Knowledge uses
SQLite. Artifact manifests record runtime-observed integrity data. Optional
retention controls bound session messages and can expire terminal sessions,
project/checkpoint families, artifact manifests, memory scopes, and knowledge;
workspace deliverables are never removed by the retention sweep. Log rotation
is separately bounded by byte and backup-count settings.

Knowledge migration preserves full historical records through verified snapshots
and a versioned roll-forward journal under `knowledge/`. Archives are excluded
from ordinary retention. Recovery verifies the staged or published candidate's
complete SQL history against its recorded count and digest, including fresh
journals with no source snapshots, before accepting or publishing that evidence.
Doctor guards an existing lock without acquiring runtime
ownership and validates complete private database copies outside state; it never
opens source databases or creates their sidecars. See the
[knowledge contract](knowledge-database-migration.md).

## Trust and evidence invariants

- A typed brief, not a raw transcript, enters the Manager boundary.
- Workers receive only assignment-scoped context and tools.
- Worker identity, process ownership, and cleanup remain runtime-owned.
- A model's artifact claim is unverified until the runtime reads the local path.
- Required requirements and acceptance criteria need evidence for `complete`.
- Cancellation and timeout produce honest non-complete terminal states.
- Completion reports and compact summaries cross back to the Principal; internal
  worker chatter does not.
- Git revision, deployment revision, and worktree fingerprints are separate
  provenance concepts. See the [GitOps runbook](gitops-runbook.md).
