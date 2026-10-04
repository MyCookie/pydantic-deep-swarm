# Agent Team Runtime

Agent Team is a persistent local multi-agent orchestration service built on
FastAPI, Pydantic, and OpenAI-compatible model endpoints. A user-facing
Principal either answers directly, asks for clarification, or delegates a typed
`ProjectBrief` to a Team Manager that creates and supervises a bounded worker
plan.

## Documentation

Start with the [documentation index](docs/README.md):

- [End-to-end workflow](docs/end-to-end-workflow.md)
- [ProjectBrief contract](docs/project-brief.md)
- [Architecture](docs/architecture.md)
- [Operations](docs/operations.md)
- [Testing](docs/testing.md)
- [GitOps and Pi runbook](docs/gitops-runbook.md)
- [Feature development and PR gates](docs/agents/feature-workflow.md)

Component-specific specifications remain beside their schemas and assets:
[Pi package](pi/README.md), [delegation skill](pi/skills/agent-team-delegation/SKILL.md),
[optional delegation adapter](integrations/hermes/principal-agent-team/README.md), and
[Git provenance](provenance/README.md).

## Runtime flow

```text
    External client
          │
          │ message or typed ProjectBrief
          ▼
   Agent Team HTTP service
          │
          ├── Agent Team Principal for message intake
          └── direct typed-brief delegation
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
   response boundary
```

The identity and user-facing behavior of the invoking client are outside the
Agent Team architecture boundary.

The runtime keeps raw worker reasoning out of typed handoffs, verifies local
artifacts by path, size, and SHA-256, enforces worker/project limits, and persists
sessions and checkpoints outside the source checkout.

## Quick start

Provision the repository's locked Python environment and select a model endpoint:

```sh
uv sync --frozen
export LLM_BASE_URL="${LLM_BASE_URL:?set the OpenAI-compatible base URL}"
export LLM_MODEL=auto  # requires exactly one advertised model
uv run --frozen agent-team init
uv run --frozen agent-team serve
```

Use a separate terminal for `uv run --frozen agent-team doctor --json`. State
defaults to `~/.agent-team`, with workspace below it; use `--state-dir` and
`--workspace-dir` to select other external paths. A new state directory has no
runtime lock, so enabled-knowledge inspection initially exits 2. During runtime
ownership it also exits 2 because SQL inspection is deferred. After a clean stop,
the existing free lock permits full inspection. Doctor never repairs state.

Use an explicit advertised model ID when the catalog contains several models.
Hermes, Pi, s6, Docker, root, and an external virtual environment are optional
deployment concerns. The exact behavior is specified in the
[configuration](docs/configuration-contract.md),
[foreground serve](docs/foreground-serve-contract.md), and
[doctor](docs/doctor-contract.md) contracts.

Validate the installed deterministic suite with isolated runtime settings:

```sh
.venv/bin/python -m compileall -q src tests pi
.venv/bin/python scripts/run_isolated_tests.py
.venv/bin/python pi/manifest_validator.py pi/manifest.json
```

For clone verification, optional supervised startup, API examples, health/readiness,
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
