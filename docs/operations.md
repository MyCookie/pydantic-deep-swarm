# Agent Team operations

## Prerequisites

The clone-native runtime requires:

- Python 3.11 or newer and uv (`uv sync --frozen` provisions the clone's environment);
- an OpenAI-compatible model endpoint. The worker endpoint must support native
  tool calls (`tools`, `tool_calls`) and `tool_choice: "required"`; the
  principal, manager and curator need only chat completions;
- Git;

Pi, Hermes, s6, containers, and a separately provisioned virtual environment are
optional deployment adapters. The repository does not install inference runtimes
or system services.

Use the portable variable list in [`.env.example`](../.env.example). Keep actual
values in an ignored environment file or inject them through the supervisor.

## Core startup and optional supervision

Core startup requires Python and runtime/model configuration, without s6:

```sh
agent-team init
agent-team serve --host localhost --port 8080
agent-team doctor --json
agent-team swarm detect
agent-team swarm status --json
```

`doctor` validates configuration, advertised models, observable path access, and
nonmutating knowledge inspection. A stopped service is normal in core scope.
With enabled knowledge, a missing existing lock exits 2 (`inspection_unavailable`)
and an occupied lock exits 2 (`inspection_deferred`). A safely inspected stopped
canonical store passes; definite failures exit 1. Optional deployment checks are
selected with `--with-pi` or `--supervisor=s6`. Readiness observations do not prove
that an endpoint owns the selected state directory. The existing `bootstrap`
workflow retains its separate deployed-installation report and requirements.

`init`, `serve`, and `doctor` share `--config`, `--state-dir`, and
`--workspace-dir`. File selection is CLI, environment, then the canonical state
configuration path. Path fields resolve independently by CLI, YAML, environment,
and default, with mismatches that silently redirect environment-selected state
rejected. Generated YAML contains absolute paths. Repeat init preserves it;
`init --overwrite-config` saves a backup before whole-file atomic replacement.
See the [configuration contract](configuration-contract.md) for the full rules.

Leased child commands use the Darwin `sandbox-exec` backend with an absolute,
resolved executable and a fresh generation-specific scratch directory. Executable
aliases and platforms without this confinement backend are refused. Children have
no durable write or commit authority; the owner checks its live generation before
managed mutations. Acceptance reports qualify the tested host and native sandbox
policy rather than claiming confinement on other operating-system versions.

Set `AGENT_TEAM_SUPERVISOR=none` to disable the optional adapter even when legacy
service variables are inherited. Set it to `s6` to require the adapter. When unset,
only a nonempty `AGENT_TEAM_SERVICE_DIR` opts into s6 compatibility. Installed
executables alone do not opt in. In core mode, supervisor state is
`not_configured`, runfile fields are null, and missing s6 does not produce drift.
Selected but unusable supervision is `unavailable` and fails status.

Optional templates stay in `s6-service/`; deployment tooling installs live copies
outside the checkout. Core `swarm reconcile` persists one owned YAML file and
does not require restart capability for planning or configuration persistence.
Dry runs create no locks, directories, or temporary files. `--no-restart` leaves
activation pending, and `--no-verify` cannot report verified activation. An
unavailable requested restart never turns a committed configuration into a
verified runtime. Source and runfiles are not model authority and are not edited.
Core status resolves `auto` without writing it and compares each observed role
with its own desired model and endpoint. See the
[model-selection contract](model-selection-contract.md).

## Optional s6 deployment paths

Deployment-specific paths are never guessed by the runtime:

| Variable | Meaning |
| --- | --- |
| `AGENT_TEAM_PROJECT_ROOT` | Repository checkout root |
| `AGENT_TEAM_VENV` | External Python environment |
| `AGENT_TEAM_PYTHON` | Optional explicit Python executable; defaults to `$AGENT_TEAM_VENV/bin/python` in service templates |
| `AGENT_TEAM_HOME` | Service user's home |
| `AGENT_TEAM_STATE_DIR` | External mutable runtime state |
| `AGENT_TEAM_SERVICE_DIR` | Live supervised service directory |
| `AGENT_TEAM_SUPERVISOR` | Explicit `none` or `s6`; overrides legacy selection |
| `AGENT_TEAM_LIVE_RUNFILE` | Live runfile; defaults to `$AGENT_TEAM_SERVICE_DIR/run` |
| `AGENT_TEAM_SERVICE_DEFINITION` | Service definition used by operator commands |
| `AGENT_TEAM_LOG_PATH` | External service log |
| `AGENT_TEAM_S6_SVC` | `s6-svc` command name or injected executable path |
| `AGENT_TEAM_S6_SVSTAT` | `s6-svstat` command name or injected executable path |
| `AGENT_TEAM_S6_SETUIDGID` | `s6-setuidgid` command name or injected executable path |

The s6 templates resolve `with-contenv`, `s6-svc`, and `s6-setuidgid` through
`PATH` or explicit variables rather than repository-specific absolute paths.

## Model configuration

Environment fallback configuration uses:

```sh
export LLM_BASE_URL="${LLM_BASE_URL:?set the OpenAI-compatible base URL}"
export LLM_API_KEY=""  # leave empty for an unauthenticated local endpoint
export LLM_MODEL="${LLM_MODEL:-auto}"  # discovers the sole advertised model
export PRINCIPAL_MODEL="${PRINCIPAL_MODEL:-$LLM_MODEL}"
export MANAGER_MODEL="${MANAGER_MODEL:-$LLM_MODEL}"
export WORKER_MODEL="${WORKER_MODEL:-$LLM_MODEL}"
export CURATOR_MODEL="${CURATOR_MODEL:-$WORKER_MODEL}"
```

`LLM_MODEL=auto` queries the standard `GET /models` endpoint and selects the
model only when exactly one ID is advertised. Configure an explicit ID when the
endpoint advertises multiple models; the runtime fails closed rather than
guessing between them.

A YAML configuration at `$AGENT_TEAM_STATE_DIR/config/config.yaml` becomes
authoritative when present. The loader supports a bounded substitution grammar:
`${VAR}`, `${VAR:-default}`, and nested defaults such as
`${PRINCIPAL_BASE_URL:-${LLM_BASE_URL}}`. Expansion is limited to 16 levels;
invalid names, unterminated expressions, and cycles fail closed. It does not
execute shell syntax or command substitutions.

```yaml
runtime:
  state_dir: ${AGENT_TEAM_STATE_DIR}
  workspace_dir: ${AGENT_TEAM_WORKSPACE_DIR}
  max_workers: 6
  max_concurrent_workers: 3
  max_turns: 10

models:
  principal:
    model: ${PRINCIPAL_MODEL}
    base_url: ${PRINCIPAL_BASE_URL:-${LLM_BASE_URL}}
  manager:
    model: ${MANAGER_MODEL}
    base_url: ${MANAGER_BASE_URL:-${LLM_BASE_URL}}
  worker:
    model: ${WORKER_MODEL}
    base_url: ${WORKER_BASE_URL:-${LLM_BASE_URL}}
  curator:
    model: ${CURATOR_MODEL:-${WORKER_MODEL}}
    base_url: ${CURATOR_BASE_URL:-${LLM_BASE_URL}}

memory:
  enabled: true
  shared_knowledge: true
  curator_enabled: true

tools:
  enabled: true
  max_tool_calls: 8

retention:
  max_session_messages: 200
  session_max_age_days: null
  project_max_age_days: null
  knowledge_max_age_days: null
  memory_max_age_days: null
  log_max_bytes: null
  log_backup_count: 5
```

Do not place a real model credential in worker configuration. Workers have no
command-execution capability. The service reads `LLM_API_KEY` only for model HTTP
clients; Pi and bootstrap probe child environments scrub credential-shaped
variables. Runtime boundaries redact credential-shaped content before prompts,
evidence, reports, logs, durable state, memory, or knowledge.

All age limits and log rotation are opt-in: `null` preserves existing indefinite
storage. Positive configured values are enforced at startup after interrupted
sessions are recovered. Session expiry never removes `processing` work; project
expiry removes checkpoints and artifact manifests but not workspace deliverables.
Memory and knowledge have independent age controls. `max_session_messages`
bounds prompt/response history per session, and rotating logs retain at most
`log_backup_count` backups.

## Optional HTTP authentication

Agent Team supports one environment-only service credential:

```sh
export AGENT_TEAM_API_TOKEN=""  # empty/unset keeps anonymous local mode
```

When nonempty, every Agent Team route—including `/health`, `/ready`, `/models`,
OpenAPI, and documentation—is protected by Bearer authentication. Bootstrap,
swarm control, `agent-team status`, `agent-team cancel`, live E2E tests, and the
optional delegation adapter read the same variable automatically. Keep it
separate from `LLM_API_KEY`, which is sent only to the model endpoint.

The first-party plugin source is versioned under
`integrations/hermes/principal-agent-team/`. Install or update that directory
explicitly from the same trusted Agent Team revision before restarting the host
gateway; the acceptance tests load this repository copy rather than host-local
state.

For a direct request in authenticated mode:

```sh
curl -fsS \
  -H "Authorization: Bearer $AGENT_TEAM_API_TOKEN" \
  "$AGENT_TEAM_URL/ready"
```

The token authenticates the service as a whole; it does not provide per-session
ownership or multi-tenant authorization. Shared or non-loopback deployments
also require TLS or a trusted terminating proxy. Anonymous mode must stay
loopback/container-local.

## Validate a clone without installing

Set external paths, then run the non-installing bootstrap gate:

```sh
export AGENT_TEAM_PROJECT_ROOT="${AGENT_TEAM_PROJECT_ROOT:-$(pwd)}"
export AGENT_TEAM_VENV="${AGENT_TEAM_VENV:?set the external venv}"
export AGENT_TEAM_PYTHON="${AGENT_TEAM_PYTHON:-$AGENT_TEAM_VENV/bin/python}"
export AGENT_TEAM_STATE_DIR="${AGENT_TEAM_STATE_DIR:?set external state storage}"
export AGENT_TEAM_SERVICE_DIR="${AGENT_TEAM_SERVICE_DIR:?set the live service directory}"
export PI_BINARY="${PI_BINARY:?set the preinstalled Pi executable}"

agent-team bootstrap \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --python "$AGENT_TEAM_PYTHON" \
  --model-endpoint "$LLM_BASE_URL" \
  --state-dir "$AGENT_TEAM_STATE_DIR" \
  --service-dir "$AGENT_TEAM_SERVICE_DIR" \
  --ready-url "${AGENT_TEAM_URL:-http://localhost:8080}/ready" \
  --report "$AGENT_TEAM_STATE_DIR/bootstrap.json" \
  --json
```

Bootstrap validates repository state, Python and dependencies, model discovery,
external writable state, configuration, Pi manifest, s6 status, and Agent Team
readiness. Its service gate always calls `/ready`, never the liveness-only
`/health` endpoint. It does not clone, install packages, reconcile Pi, or restart
services.

## Start and inspect the service

The source service definitions are:

- `s6-service/agent-team-init/` — initializes external directories and configuration
  without creating or migrating knowledge databases;
- `s6-service/agent-team/` — invokes foreground serve with the configured service user,
  project root, Python executable, host, and port.

Operate the installed service definition through the configured command:

```sh
"${AGENT_TEAM_S6_SVC:-s6-svc}" -u "$AGENT_TEAM_SERVICE_DEFINITION"
"${AGENT_TEAM_S6_SVC:-s6-svc}" -s "$AGENT_TEAM_SERVICE_DEFINITION"
```

The default HTTP bind is `localhost:8080`. Keep it loopback/container-local when
`AGENT_TEAM_API_TOKEN` is empty.

Standalone serve uses one leased FastAPI lifespan, bounded startup/shutdown, and
one listener. It emits `serve_ready` on stdout after actual binding. The first
SIGINT/SIGTERM stops admission and drains owned work; clean exits are 130/143.
Startup or cleanup failures exit 1 with a sanitized `serve_failed` event on
stderr. The [foreground contract](foreground-serve-contract.md) defines phases,
deadlines, readiness, and child-write boundaries.

Knowledge has one canonical path, `<state_dir>/knowledge/knowledge.db`. Enabled
leased startup automatically reconciles supported legacy/empty/identical state,
preserving full history in verified archives with journaled recovery. Divergent,
invalid, unsafe, or unprovable state fails closed. Disabled knowledge opens and
migrates nothing. See [knowledge migration](knowledge-database-migration.md) for
safe offline recovery; retain originals, sidecars, and recovery evidence.

## Liveness and readiness

```sh
curl -fsS "$AGENT_TEAM_URL/health"
curl -fsS "$AGENT_TEAM_URL/ready"
```

`/health` is the process liveness endpoint. `/ready` checks that configuration,
engine, session store, and state directory are initialized. Use `/ready` when an
operator or deployment gate needs actual readiness.

## Submit and inspect work

Direct conversation path:

```sh
session_json="$(curl -fsS -X POST "$AGENT_TEAM_URL/sessions" \
  -H 'content-type: application/json' \
  -d '{"client_key":"operator-example"}')"
```

Extract the returned session ID with the operator's JSON tool, then submit a
message:

```sh
curl -fsS -X POST "$AGENT_TEAM_URL/sessions/$SESSION_ID/messages" \
  -H 'content-type: application/json' \
  -d '{"message":"Implement and verify the requested change"}'
```

Typed delegation path:

```sh
curl -fsS -X POST "$AGENT_TEAM_URL/sessions/$SESSION_ID/delegations" \
  -H 'content-type: application/json' \
  -d '{"brief":{"objective":"Implement the change","desired_output":"Verified report"}}'
```

Inspect or cancel:

```sh
curl -fsS "$AGENT_TEAM_URL/sessions/$SESSION_ID"
curl -fsS "$AGENT_TEAM_URL/sessions/$SESSION_ID/report"
curl -fsS -X POST "$AGENT_TEAM_URL/sessions/$SESSION_ID/cancel"
```

See the [end-to-end workflow](end-to-end-workflow.md) for behavior behind each
request.

## Swarm control plane

The commands below describe current implementation. The planned replacement is
the [owned model-selection contract](model-selection-contract.md); its YAML-only
reconciliation and optional supervisor behavior require the subsequent build.

With live service paths configured:

```sh
agent-team swarm detect
agent-team swarm status --json
agent-team swarm reconcile --dry-run --json
```

A non-dry-run reconcile updates only the live deployment runfile, never tracked
source defaults. Auto mode pins the live selection to `auto`; `--model` pins the
chosen advertised ID. The command then restarts through s6 and verifies
convergence. It is an operational mutation and should be run only with the
intended live paths and privileges.

## Pi assets and release operations

Pi reconciliation accepts only a full trusted commit ID and never fetches a ref.
Use the [GitOps runbook](gitops-runbook.md) for promotion, reconciliation,
rollback, cancellation, and recovery. Package-specific details are in the
[Pi README](../pi/README.md).

## Operational constraints

- One runtime process owns one state directory.
- Mutable state and workspaces must remain outside the checkout.
- HTTP authentication is optional; anonymous mode must not be exposed to
  untrusted clients, and a shared/non-loopback deployment also needs TLS or a
  trusted terminating proxy.
- Workers expose bounded list/read/write tools only; command, shell, Python, and
  Git execution are not available.
- Set `LLM_API_KEY` only when the model endpoint requires it.
- Set `AGENT_TEAM_API_TOKEN` independently when Agent Team HTTP auth is desired.
- A network operator who modifies Agent Team must prominently offer remote
  users the corresponding source for that deployed version as required by
  AGPLv3 section 13. Keep deployment-specific and private Git remote URLs in
  local Git configuration rather than tracked package metadata or tests.
- Never treat ignored logs or external state as automatically confidential;
  apply filesystem and backup controls appropriate to the deployment.
