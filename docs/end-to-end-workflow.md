# Agent Team end-to-end workflow

This document follows one request across every supported Agent Team boundary.
External clients enter through the public HTTP message or typed-delegation
routes; the identity and user-facing behavior of the caller are implementation
details outside the Agent Team runtime.

## Participants

- **External client** — creates or reuses a session, then submits either a user
  message or an already-authored typed brief through the public HTTP contract.
- **Agent Team HTTP service** — owns durable sessions and exposes the public
  request, delegation, report, reset, cancellation, health, readiness, and model
  inspection routes.
- **Agent Team Principal** — used only for direct `/messages` intake. It returns
  a typed `PrincipalDecision`: `answer`, `clarify`, or `delegate`.
- **Team Manager** — converts a validated `ProjectBrief` into a bounded
  `ManagerPlan` of worker assignments and dependencies.
- **Workers** — short-lived specialist agents with only explicitly granted
  builtin tools.
- **Reviewer** — an optional worker commissioned by the runtime when the plan
  requires review.
- **Runtime stores** — persist sessions, project checkpoints, artifact manifests,
  memory, and shared knowledge outside the source checkout.

## Typed handoffs

The workflow crosses components through models defined in
[`contracts/__init__.py`](../src/agent_team/contracts/__init__.py):

| Boundary | Contract | Purpose |
| --- | --- | --- |
| Principal → Team Manager | [`ProjectBrief`](project-brief.md) | Objective, requirements, constraints, acceptance criteria, permissions, context references, and desired output |
| Team Manager → runtime | `ManagerPlan` | Bounded tasks, roles, dependencies, tool grants, and review policy |
| Worker → runtime | `WorkerOutput` | Compact status, evidence, acceptance results, artifacts, findings, and unresolved items |
| Runtime → Principal | `CompletionReport` | Aggregated requirement results, verified acceptance results, artifacts, risks, findings, and final status |

Raw Principal or worker reasoning transcripts do not cross these boundaries.

## Two supported request shapes

### 1. Typed brief submission

An external client may submit an already-authored `ProjectBrief` without asking
Agent Team to repeat intake or delegation decisions.

The client:

1. Calls `POST /sessions` with its own stable client key or metadata; the durable
   session store creates or reuses the associated Agent Team session.
2. Calls `POST /sessions/{agent_team_session_id}/delegations` with the typed
   brief.
3. The service calls `AgentTeamEngine.run_delegated_brief()` directly. It does
   not send the already-authored brief through a second Principal intake.
4. The response exposes only status, project ID, concise summary, verified
   artifacts, and bounded unresolved items.

Repository-side handoff guidance is specified by the
[delegation skill](../pi/skills/agent-team-delegation/SKILL.md) and
[handoff template](../pi/prompts/principal-handoff.md). Optional client adapters
use the same HTTP contract without becoming part of the runtime architecture.

### 2. Direct HTTP conversation

A direct client:

1. Calls `POST /sessions` with an optional `client_key` and metadata.
2. Calls `POST /sessions/{session_id}/messages` with a user message.
3. The service persists the user message and invokes
   `AgentTeamEngine.run_turn()` with compact prior conversation context.
4. The Agent Team Principal returns one of three typed actions:
   - `answer` — respond directly; no Manager or worker is created.
   - `clarify` — return a user-facing question; no project is started.
   - `delegate` — provide a `ProjectBrief` and continue through the project path.
5. If structured Principal intake fails, deterministic fallback routing delegates
   substantial work and directly answers small work.

## Delegated project sequence

```text
Typed brief or Agent Team Principal decision
                    │
                    │ ProjectBrief
                    ▼
             create project ID
                    │
                    ├── persist brief checkpoint
                    ├── start one project deadline
                    ▼
              Team Manager model
                    │
                    │ ManagerPlan
                    ▼
        sanitize roles, tasks, tools, IDs
                    │
                    ├── cap workers and dynamic roles
                    ├── repair unknown roles
                    ├── reject unknown tools
                    └── inject reviewer when required
                    │
                    ▼
           dependency-aware scheduler
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
       worker A            worker B       independent tasks run concurrently
          │                   │
          └─────────┬─────────┘
                    ▼
             dependent workers
                    │
                    ▼
        aggregate WorkerResult values
                    │
                    ├── verify artifact paths, sizes, and SHA-256
                    ├── build CompletionReport
                    ├── validate every required result and criterion
                    └── attempt one bounded report repair when configured
                    │
                    ▼
          Principal renders final response
                    │
                    ├── persist report/session state
                    ├── record compact memory/knowledge
                    └── reap tasks, processes, registries, and deadlines
                    ▼
                  user
```

## Planning and staffing

The Team Manager receives only the typed brief, available role catalog, permitted
worker tool names, bounded memory/knowledge summaries, and configured limits. It
does not receive an unbounded user transcript.

The runtime sanitizes the returned plan before execution:

- worker count cannot exceed `runtime.max_workers`;
- project-local roles cannot exceed `runtime.max_dynamic_roles`;
- every task requirement ID must exist in the brief;
- every dependency must refer to another plan task;
- unknown roles fall back to a known bounded role;
- unknown tools cause the affected task to be dropped;
- workers cannot create sub-teams;
- when `review_required=true`, the runtime adds a reviewer that depends on the
  implementation tasks and grants only `read_file`.

If Manager planning fails, a deterministic fallback plan is produced from the
brief instead of fabricating a completed result.

## Worker execution

The scheduler runs every dependency-ready layer concurrently while preserving
task dependencies. Each worker receives:

- one role and one assignment;
- relevant requirements and all acceptance criteria;
- brief constraints;
- compact role memory;
- summaries from dependency tasks;
- only the tools granted in its `WorkerTaskSpec`.

The builtin capability surface is:

- `list_files`
- `read_file`
- `write_file`

Filesystem tools resolve paths beneath the configured workspace. Workers cannot
invoke Python, Git, shells, tests, or arbitrary operating-system commands. A
future command capability requires a separately authorized sandbox boundary;
adding an executable allowlist is not sufficient isolation.

Pi and bootstrap dependency-probe subprocess environments are
credential-scrubbed. Credential-shaped content is redacted before it enters
model prompts, tool evidence, reports, logs, session/project state, artifact
manifests, memory, or shared knowledge.

## Evidence, artifacts, and completion status

Worker outputs are converted into runtime-owned `WorkerResult` objects. The
runtime aggregates the strongest result per requirement and acceptance criterion.

A report is `complete` only when:

- every required requirement is satisfied with evidence;
- every acceptance criterion passed with evidence;
- no worker failed or was cancelled; and
- claimed local artifacts survive runtime verification.

Local artifact verification requires the path to remain under the workspace and
records the actual size and SHA-256. A model claim alone is not proof. Missing or
invalid evidence downgrades a claimed complete report to `partial` and appends
validation issues to unresolved items.

Other terminal statuses are:

- `partial` — some work or evidence exists, but completion is not proven;
- `blocked` — cancellation, timeout, or inability to start/finish the project;
- `failed` — an unexpected project-level failure prevented a validated report.

The Principal renders the user response from the validated report and must not
claim completion for a partial, blocked, or failed result.

## Persistence and memory

The HTTP service persists session messages, current project ID, last brief, last
report, validation issues, and terminal errors. The engine separately checkpoints
project briefs, Manager plans, completion reports, and artifact manifests.

When memory is enabled, only compact decisions, worker lessons, findings, and
curated knowledge are promoted. Raw transcripts are excluded from the intended
memory boundary. All mutable state belongs under `AGENT_TEAM_STATE_DIR`, outside
the Git-managed checkout.

## Cancellation, timeout, and restart behavior

`POST /sessions/{session_id}/cancel` asks the owning engine to cancel the root
project and its descendants. Cancellation sets the project event, cancels and
awaits worker tasks, kills tracked subprocess groups, releases registry entries,
and persists a blocked terminal report where possible.

Worker and project deadlines use the same cleanup path. A project-level timeout
returns `blocked`; a worker-level timeout returns a failed worker result that
normally makes the aggregate report partial.

Only one Agent Team service process may own a state directory. Startup acquires
an operating-system lease. After an unclean restart, sessions left in
`processing` are marked `blocked`; the service does not invent successful
completion.

## Executable acceptance coverage

HTTP acceptance follows the same asynchronous control flow as the service. Tests
mount the FastAPI application directly into `httpx.ASGITransport` and send
requests with `httpx.AsyncClient`. Request handling, in-flight cancellation, and
test assertions therefore remain on one event loop; the suite does not use
Starlette's thread-bridged `TestClient` or its AnyIO `BlockingPortal` path.

Deterministic HTTP tests install isolated runtime and session-store state around
the ASGI application and restore the prior globals after each scenario. The
separate opt-in live E2E lane covers the actual supervised process and network
endpoint. This transport choice changes only the verification workflow, not the
production FastAPI/Uvicorn request path described above.

The principal executable specification is
[`tests/test_end_to_end_acceptance.py`](../tests/test_end_to_end_acceptance.py).
It covers direct answers, typed delegation, reviewed work, artifact proof,
Manager and worker failures, malformed output, clarification, timeout, tool
rejection, cancellation, persistence, and the external adapter boundary.

See [Testing](testing.md) for the deterministic and live verification commands.
