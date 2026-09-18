# Agent Team Runtime

Agent Team is a persistent local multi-agent orchestration service built on
FastAPI, Pydantic, and OpenAI-compatible model endpoints. A user-facing
Principal either answers directly, asks for clarification, or delegates a typed
`ProjectBrief` to a Team Manager that creates and supervises a bounded worker
plan.

## Documentation

Start with the [documentation index](docs/README.md):

- [End-to-end workflow](docs/end-to-end-workflow.md)
- [Architecture](docs/architecture.md)
- [Operations](docs/operations.md)
- [Testing](docs/testing.md)
- [GitOps and Pi runbook](docs/gitops-runbook.md)

Component-specific specifications remain beside their schemas and assets:
[Pi package](pi/README.md), [delegation skill](pi/skills/agent-team-delegation/SKILL.md),
[Hermes Principal plugin](integrations/hermes/principal-agent-team/README.md), and
[Git provenance](provenance/README.md).

## Runtime flow

```text
User / Hermes Principal
          │
          │ answer, clarify, or typed ProjectBrief
          ▼
   Agent Team HTTP service
          │
          ▼
      Team Manager
          │
          ├── bounded specialist workers
          ├── optional reviewer
          └── verified artifacts and evidence
          │
          ▼
    CompletionReport
          │
          ▼
       Principal
          │
          ▼
         User
```

The runtime keeps raw worker reasoning out of typed handoffs, verifies local
artifacts by path, size, and SHA-256, enforces worker/project limits, and persists
sessions and checkpoints outside the source checkout.

## Quick start

Provision Python and the model endpoint separately, then set deployment values
from [`.env.example`](.env.example). At minimum:

```sh
export AGENT_TEAM_PROJECT_ROOT="${AGENT_TEAM_PROJECT_ROOT:-$(pwd)}"
export AGENT_TEAM_VENV="${AGENT_TEAM_VENV:?set an external virtual environment}"
export AGENT_TEAM_PYTHON="${AGENT_TEAM_PYTHON:-$AGENT_TEAM_VENV/bin/python}"
export AGENT_TEAM_STATE_DIR="${AGENT_TEAM_STATE_DIR:?set external state storage}"
export AGENT_TEAM_SERVICE_DIR="${AGENT_TEAM_SERVICE_DIR:?set the live service directory}"
export LLM_BASE_URL="${LLM_BASE_URL:?set the OpenAI-compatible base URL}"
export LLM_MODEL="${LLM_MODEL:?set the advertised model ID}"
```

Validate the source and deterministic suite:

```sh
export PYTHONPATH=src
.venv/bin/python -m compileall -q src tests pi
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
.venv/bin/python pi/manifest_validator.py pi/manifest.json
```

For clone verification, supervised startup, API examples, health/readiness,
swarm reconciliation, and cancellation, follow [Operations](docs/operations.md).

## Security boundary

The default deployment model is a bounded local POC:

- HTTP Bearer authentication is optional: unset `AGENT_TEAM_API_TOKEN` preserves
  anonymous local mode, while a nonempty value protects every API and generated
  documentation route;
- anonymous mode must remain loopback/container-local; shared or non-loopback
  use requires the token plus TLS or a trusted terminating proxy;
- worker tools are limited to bounded list/read/write operations; command,
  Python, Git, and shell execution are not exposed;
- workers do not inherit Hermes-native tools, connectors, or model credentials;
- credential-shaped content is redacted before prompts, evidence, reports,
  logs, durable state, memory, and knowledge;
- durable retention is opt-in and configurable for session history, session and
  project state, artifact manifests, memory, knowledge, and rotated logs;
- mutable state, logs, memory, and artifacts must remain outside the checkout;
- one shared token is service authentication, not multi-tenant authorization.

## Release discipline

A green test suite is not itself a release revision. Public release evidence
requires a committed full Git revision, current provenance, validated assets,
lock/dependency checks, and a reproducible build. See the
[GitOps runbook](docs/gitops-runbook.md).

## License

The Agent Team runtime and repository documentation are distributed under
[GNU AGPLv3](LICENSE), version 3 only. The separately scoped first-party assets
under `pi/` remain available under the [MIT License](pi/LICENSE).
