# Agent Team architecture

## System purpose

Agent Team is a persistent local orchestration service. It separates user-facing
reasoning, project planning, bounded specialist execution, evidence validation,
and durable state through typed boundaries.

The complete request lifecycle is documented in the
[end-to-end workflow](end-to-end-workflow.md).

## Component map

```text
User / Hermes
      │
      ▼
Hermes Principal and typed delegation plugin
      │                    direct HTTP client
      └──────────┬──────────────────┘
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
- `ProjectBrief` uses stable requirement and acceptance-criterion IDs.
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

The supported model is one supervised Agent Team process per state directory.
Startup acquires a nonblocking operating-system lease at
`<state_dir>/runtime.lock`. A second process fails closed instead of sharing
in-memory registries, process ownership, or cancellation state.

Durable sessions survive restart. Live tasks do not: startup converts stale
`processing` sessions to `blocked` with a restart error. This keeps recovery
honest and avoids fabricated completion.

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
├── knowledge/
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
