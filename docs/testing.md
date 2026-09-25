# Agent Team testing

## Test layers

The suite is organized around observable boundaries rather than model internals.

| Layer | Purpose | Representative files |
| --- | --- | --- |
| Contracts | Typed brief, plan, worker output, report, and identifier validation | `tests/test_contracts.py` |
| Principal routing | Direct answer, clarification, and delegation decisions | `tests/test_principal_routing.py` |
| Engine acceptance | Planning, dependencies, review, artifacts, failure, timeout, and cleanup | `tests/test_end_to_end_acceptance.py`, `tests/test_acceptance_boundaries.py` |
| HTTP boundary | Sessions, messages, reports, cancellation, readiness, and serialization | `tests/test_end_to_end_acceptance.py`, `tests/test_tdd_swarm.py` |
| Worker safety | Workspace bounds, tool grants, turn limits, process ownership, and credential isolation | `tests/test_worker_capabilities.py`, `tests/test_worker_turn_limits.py` |
| Persistence/memory | Durable sessions, scopes, knowledge, restart recovery, and concurrent safety | `tests/test_process_model.py`, `tests/test_memory_scope_boundaries.py` |
| GitOps/Pi | Repository boundary, provenance, manifest integrity, exact revision, and rollback | `tests/test_gitops_boundary.py`, `tests/test_pi_reconciler.py` |
| Publication integrity | Documentation layout, non-asserting test detection, and machine-specific literal scans | `tests/test_documentation_layout.py`, `tests/test_test_suite_integrity.py` |
| Live E2E | Real model and running HTTP service | `tests/test_e2e.py` |

## Deterministic verification

Run tests with an isolated state directory so a live Agent Team process cannot
collide with the test lease:

```sh
export AGENT_TEAM_STATE_DIR="$(mktemp -d)"
export PYTHONPATH=src
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

The deterministic suite must not require a model endpoint. Model and HTTP edges
use typed stubs, mock transports, or an in-process ASGI application.

Compile source, tests, and Pi validation code:

```sh
PYTHONPATH=src .venv/bin/python -m compileall -q src tests pi
PYTHONPATH=src .venv/bin/python pi/manifest_validator.py pi/manifest.json
sh -n s6-service/agent-team/run
sh -n s6-service/agent-team-init/run
```

Check dependency and build reproducibility:

```sh
uv lock --check --offline
uv pip check
uv build --offline --out-dir "$(mktemp -d)"
```

## Live E2E tests

Live tests are opt-in and fail closed:

```sh
export AGENT_TEAM_RUN_LIVE_E2E=1
export AGENT_TEAM_URL="${AGENT_TEAM_URL:-http://localhost:8080}"
export AGENT_TEAM_API_TOKEN=""  # set to the service token when HTTP auth is enabled
export LLM_BASE_URL="${LLM_BASE_URL:?set the live model endpoint}"
export LLM_API_KEY=""  # set only when the model endpoint requires Bearer auth
export LLM_MODEL="${LLM_MODEL:-auto}"  # explicit ID required only when selection is ambiguous
PYTHONPATH=src .venv/bin/python -m pytest tests/test_e2e.py -v -p no:cacheprovider
```

Without `AGENT_TEAM_RUN_LIVE_E2E=1`, these tests report explicit skips. They do
not catch failures and return `False`; an enabled endpoint or model failure is a
pytest failure.

The integrity meta-test prevents regression to boolean-return tests or
collection-time `os.environ` mutation:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_test_suite_integrity.py -q
```

## HTTP test transports

All in-process HTTP acceptance tests use one asynchronous transport:

```python
transport = httpx.ASGITransport(app=app)
async with httpx.AsyncClient(
    transport=transport,
    base_url="http://agent-team",
) as client:
    response = await client.post("/sessions", json={"client_key": "test"})
```

This keeps the request, application, cancellation, and assertion workflow on the
same async event loop. It also supports true in-flight tests, such as cancelling
a session while its message request is awaiting worker completion.

The suite deliberately does not import `fastapi.testclient.TestClient` or
`starlette.testclient.TestClient`. Those synchronous clients bridge into the
ASGI loop through AnyIO's thread `BlockingPortal`; the installed Starlette code
references the deprecated `anyio.abc.BlockingPortal` alias. Avoiding that bridge
removes both the deprecated import path and the extra thread-blocking layer from
the Agent Team test workflow.

`ASGITransport` does not automatically own application lifespan. Deterministic
HTTP tests therefore inject isolated configuration, engine, and session-store
state before creating the client, then restore it afterward. Tests that exercise
the real supervised startup and shutdown path belong in the explicit live E2E
lane.

`tests/test_test_suite_integrity.py` scans every test module and fails if a
Starlette or FastAPI `TestClient` import is reintroduced.

## Acceptance expectations

A green unit suite is necessary but not sufficient for a release claim. Record:

- the full commit or explicitly labeled worktree fingerprint;
- exact commands and exit codes;
- deterministic suite results;
- explicit live-test pass or skip state;
- Pi manifest validation;
- lock/dependency/build results;
- residual warnings separately from failures.

The reusable boundary matrix is implemented by
[`tests/test_end_to_end_acceptance.py`](../tests/test_end_to_end_acceptance.py).
It covers direct answers, typed delegation, reviewed work, verified artifacts,
Manager and worker failures, malformed output, cancellation, clarification,
timeout, tool rejection, routing, result minimization, persistence, and cleanup.

## Adding or changing behavior

Use strict red-green-refactor:

1. Add one focused test at the public boundary.
2. Run it and confirm the expected failure.
3. Implement the smallest behavior change.
4. Re-run the focused test.
5. Run the affected test file, compilation, and the full isolated suite.
6. Keep live model/network calls out of deterministic acceptance tests.

For runtime-boundary changes, update the [end-to-end workflow](end-to-end-workflow.md)
and [architecture](architecture.md) in the same change.
