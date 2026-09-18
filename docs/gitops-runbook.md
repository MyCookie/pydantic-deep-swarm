# GitOps repository and operations runbook

This runbook is the operational contract for the Agent Team GitOps workflow. It
separates Git-managed desired state from Pi/runtime state, requires explicit
trust for executable assets, and uses verification gates before promotion.

The operator is responsible for supplying the intended revision, preinstalled
external prerequisites, and any authorization to commit, deploy, or push. The
Agent Team tools do not silently install, download, commit, or push anything.

## Repository contract

The repository is the source of truth for declarative desired state:

- `src/agent_team/` — Agent Team runtime source, configuration schema, and CLI.
- `pi/` — first-party Pi extensions, skills, prompts, themes, optional agents,
  package metadata, `manifest.json`, and manifest tests.
- `s6-service/` — portable supervised-service templates.
- `pyproject.toml`, `uv.lock`, and `.python-version` — Python project metadata
  and dependency resolution inputs.
- `provenance/schema.json` and reviewed `provenance/records/` — release and
  validation evidence.
- `README.md`, architecture notes, and this runbook — operator documentation.

Mutable or machine-specific data is not desired state. Keep it outside the
checkout or under ignored storage:

- `AGENT_TEAM_STATE_DIR` (default `~/.agent-team`) stores sessions, runtime
  leases, projects, memory, knowledge databases, artifacts, and logs.
- `AGENT_TEAM_WORKSPACE_DIR`, when set, is the external artifact workspace;
  otherwise artifacts use a workspace below the external state directory.
- Pi user-global state and keys live below the operator's Pi home. Project-local
  owned releases live below the ignored `.pi/agent-team/` directory.
- `.env`, credentials, private keys, dependency environments, caches,
  downloaded installers, and temporary files are ignored and must never be
  committed.
- Product, market, client, and investor research for unrelated projects is not
  Agent Team desired state. Keep it in external archival storage, not under a
  repository `research/` directory.

Check the worktree before promotion:

```text
git status --porcelain
git diff --check
git rev-parse --verify HEAD
```

A clean result means no tracked desired-state file is modified. Ignored runtime
files may exist locally; they are not release inputs. If a runtime file appears
as tracked or untracked desired-state output, stop and correct the boundary
before promoting it. Do not use `git add -A` as an authorization shortcut.

## External prerequisites

Provision these independently of the repository and verify them before
bootstrap:

1. Git, with the trusted full 40- or 64-character commit ID available locally.
2. A preinstalled, executable Pi binary at an absolute path. Do not replace it
   by downloading Pi or installing a package during reconciliation.
3. Python 3.11 or newer in an external environment. The default deployment
   path is `AGENT_TEAM_VENV/bin/python`; set `AGENT_TEAM_PYTHON` for a
   different absolute executable or `AGENT_TEAM_VENV` for a different venv.
4. The Python environment must already provide `pydantic`, `pydantic_ai`,
   `httpx`, `fastapi`, `uvicorn`, `click`, `aiosqlite`, and `yaml`.
5. s6-overlay service supervision, including `s6-svc`, `s6-svstat`, and the
   `s6-setuidgid` command used by the service template.
6. Writable persistent state storage outside the checkout. A mounted volume is
   preferred for `AGENT_TEAM_STATE_DIR` and any separate artifact workspace.
7. The configured model service, including `GET /v1/models`, and its API key
   supplied through the deployment environment rather than repository files.
8. A service user with access to the external state, Python environment, Pi
   home, and model endpoint.

External requirements declared by `pi/manifest.json` are prerequisites, not
installation instructions. A missing tool is a failed gate. The bootstrap
report records `missing_external_tools`; it does not fetch or install them.

## Promotion workflow

Promotion is a sequence of reviewed, independently verifiable states:

1. Start from the intended base revision and capture provenance before making
   generated changes.
2. Generate or edit only tracked source and first-party Pi assets. Keep runtime
   state and generated reports outside the checkout unless the operator wants a
   reviewed evidence file committed deliberately.
3. Validate the Pi package and run the repository checks:

   ```text
   python pi/manifest_validator.py pi/manifest.json
   python -m pytest tests/ -q
   ```

4. Review the diff, asset paths, manifest hashes, executable extension changes,
   and external requirements. Run `git status --porcelain` again.
5. Capture a candidate provenance record. Write complete validation evidence
   outside the candidate tree first; every executed check needs its exact argv,
   concise result, and timezone-aware start/finish times. Then pass that file to
   the recorder:

   ```text
   agent-team provenance capture \
     --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
     --output "$AGENT_TEAM_STATE_DIR/provenance/candidate.json" \
     --record-id candidate-001 \
     --project-id GITOPS-PLAN \
     --agent swarm-worker \
     --model "$LLM_MODEL" \
     --task-id GITOPS-10 \
     --validation-evidence "$AGENT_TEAM_STATE_DIR/provenance/validation.json" \
     --asset pi/manifest.json \
     --json
   ```

6. If and only if the operator authorizes a commit, use ordinary Git commands
   to commit the reviewed candidate. Record the resulting full SHA; the
   provenance recorder itself never stages, commits, tags, or pushes:

   ```text
   git add src pi s6-service pyproject.toml uv.lock provenance
   git commit -m "Promote reviewed Agent Team assets"
   REVISION="$(git rev-parse --verify HEAD)"
   ```

7. Run the dry-run reconciliation and the non-installing bootstrap gate for
   that exact revision. Do not substitute a branch, tag, `HEAD`, or short SHA.
8. Reconcile only after the dry-run and bootstrap checks pass. Record
   `deployed_revision` only after Pi loading and service health are verified.
9. If a remote push is desired, treat it as a separate operator-authorized
   release action. `remote_push_enabled` and `auto_push` remain false in
   provenance records.

Validate a provenance record without changing Git state:

```text
agent-team provenance validate \
  "$AGENT_TEAM_STATE_DIR/provenance/candidate.json" \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --check-candidate
```

Use `--check-candidate` only while the recorded worktree fingerprint is expected
to match. Writing a tracked provenance file changes an uncommitted worktree
fingerprint, so validate tracked records without that flag or validate before
writing the record into the tree.

## Executable extension trust policy

Pi extensions are executable code. Treat every change under
`pi/extensions/`, and any skill, prompt, theme, or agent that can alter runtime
behavior, as untrusted until reviewed.

The trust gate is:

- review the exact full commit and `git diff` against the previously trusted
  revision;
- run `python pi/manifest_validator.py pi/manifest.json` and the focused/full
  tests;
- confirm every asset is listed with a matching SHA-256 and size in
  `manifest.json`;
- inspect `external_requirements` and provision them separately;
- run `agent-team pi reconcile ... --dry-run` first; and
- pass `--approve` for project-local loading only after the operator explicitly
  accepts the reviewed revision.

The reconciler invokes the supplied absolute Pi binary, installs only the
manifested package into the selected scope, verifies package registration and
RPC startup, and records ownership. It preserves unmanaged Pi package entries,
settings fields, and files. It never runs `npm install`, fetches packages,
searches for a replacement Pi binary, changes Git refs, or deletes resources
outside its ownership markers.

Project-local releases are below `.pi/agent-team/`; user-global releases are
below `~/.pi/agent/agent-team/`. Both are runtime state, not release inputs.
The project-local settings file may be tracked only when intentionally managed;
never put credentials or machine-specific secrets in it.

## Bootstrap a clone

Set deployment values explicitly. The state and Python paths below are outside
the repository:

```text
export AGENT_TEAM_PROJECT_ROOT="${AGENT_TEAM_PROJECT_ROOT:-$(pwd)}"
export AGENT_TEAM_VENV="${AGENT_TEAM_VENV:?set AGENT_TEAM_VENV to the external venv path}"
export AGENT_TEAM_STATE_DIR="${AGENT_TEAM_STATE_DIR:?set AGENT_TEAM_STATE_DIR to external storage}"
export AGENT_TEAM_WORKSPACE_DIR="${AGENT_TEAM_WORKSPACE_DIR:?set AGENT_TEAM_WORKSPACE_DIR to external storage}"
export AGENT_TEAM_URL="${AGENT_TEAM_URL:-http://localhost:8080}"
export AGENT_TEAM_SERVICE_DIR="${AGENT_TEAM_SERVICE_DIR:?set AGENT_TEAM_SERVICE_DIR to the live service directory}"
export AGENT_TEAM_SERVICE_DEFINITION="${AGENT_TEAM_SERVICE_DEFINITION:?set AGENT_TEAM_SERVICE_DEFINITION to the s6 definition}"
export PI_BINARY="${PI_BINARY:?set PI_BINARY to the preinstalled Pi path}"
export MODEL_ENDPOINT="${MODEL_ENDPOINT:?set MODEL_ENDPOINT to the vLLM URL}"
REVISION="$(git rev-parse --verify HEAD)"

agent-team bootstrap \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --python "$AGENT_TEAM_VENV/bin/python" \
  --model-endpoint "$MODEL_ENDPOINT" \
  --state-dir "$AGENT_TEAM_STATE_DIR" \
  --expected-revision "$REVISION" \
  --service-dir "$AGENT_TEAM_SERVICE_DIR" \
  --report "$AGENT_TEAM_STATE_DIR/bootstrap.json" \
  --json
```

Bootstrap verifies:

- repository layout, clean Git state, and the exact expected revision when
  `--expected-revision` is supplied;
- the repository boundary and ignored runtime/external paths;
- Pi version and the bundled asset manifest;
- Python version and required imports;
- model `/models` and Agent Team `/ready`;
- external state-directory writability and configuration validity; and
- s6 service readiness.

It may create missing external state directories and temporary write probes. It
never installs or fetches tools, packages, Pi, or service state; it does not run
`git clone`, `pip`, `npm`, `uv`, Pi installation, or service restart commands. A
failed report is non-ready and returns a nonzero exit code.
Read `missing_external_tools` and provision those tools explicitly before
retrying; do not work around the report by installing from inside the clone.

## Reconcile Pi assets

Resolve the trusted commit and preview before mutation:

```text
REVISION="$(git rev-parse --verify HEAD)"

agent-team pi reconcile "$REVISION" \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --scope project-local \
  --dry-run \
  --json
```

After reviewing the dry-run result and approving project trust:

```text
agent-team pi reconcile "$REVISION" \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --scope project-local \
  --approve \
  --json
```

For a user-global installation, select `--scope user-global` and provide
`--user-home` when the target Pi home is not the current user's home. User-global
changes affect every project using that Pi installation and require separate
operator approval.

Inspect the result and owned-resource drift:

```text
agent-team pi status \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --scope project-local \
  --json
```

A nonzero status means drift, a missing ownership marker, a missing release, a
settings-registration problem, or another failed inspection. Preserve the
reported state and investigate; do not delete `.pi/agent-team/` or the lock
file manually.

## Health and readiness checks

Check the model service first:

```text
curl -fsS "$MODEL_ENDPOINT/models"
```

Check the Agent Team service and its dependency supervisor:

```text
curl -fsS "$AGENT_TEAM_URL/health"
curl -fsS "$AGENT_TEAM_URL/ready"
curl -fsS "$AGENT_TEAM_URL/models"
s6-svc -s "$AGENT_TEAM_SERVICE_DEFINITION"
s6-svstat -o up "$AGENT_TEAM_SERVICE_DIR"
agent-team swarm status --json
```

When `AGENT_TEAM_API_TOKEN` is nonempty, add
`-H "Authorization: Bearer $AGENT_TEAM_API_TOKEN"` to direct Agent Team curl
commands. Bootstrap, swarm control, the CLI status/cancel commands, and the
Hermes delegation plugin read the token automatically. `LLM_API_KEY` remains a
separate credential sent only to the model endpoint.

Expected results are HTTP 200 with `status: ok` and `ready: true`, an
accessible external state directory, an `up` s6 service, and no unexplained
model/configuration drift. `agent-team swarm status --json` compares the model
advertised by vLLM, the source configuration, the source and live s6 runfiles,
and all Agent Team API roles.

For a deployment change, verify again after restart rather than trusting the
exit code of the restart command alone. A provenance `deployed_revision` is
valid only after these loading and health checks pass. Capture that fact only
after verification:

```text
agent-team provenance capture \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --output "$AGENT_TEAM_STATE_DIR/provenance/deployed.json" \
  --record-id "deploy-$REVISION" \
  --project-id GITOPS-PLAN \
  --agent operator \
  --task-id GITOPS-10 \
  --candidate-revision "$REVISION" \
  --deployed-revision "$REVISION" \
  --validation-evidence "$AGENT_TEAM_STATE_DIR/provenance/deployment-validation.json" \
  --asset pi/manifest.json \
  --json
```

## Rollback

Rollback uses the last known-good owned Pi revision recorded by the reconciler;
it does not reset Git and does not discard Agent Team sessions or memory.

```text
agent-team pi rollback \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --scope project-local \
  --approve \
  --json

agent-team pi status \
  --repo-root "$AGENT_TEAM_PROJECT_ROOT" \
  --pi-binary "$PI_BINARY" \
  --scope project-local \
  --json
curl -fsS "$AGENT_TEAM_URL/ready"
```

A rollback fails closed when no last-known-good revision exists. If status still
reports drift, preserve the evidence, compare the exact release marker and
manifest, and restore only through a reviewed exact revision. Do not manually
edit the ownership marker, delete unmanaged Pi resources, or claim deployment
success before the post-rollback checks pass.

## Cancellation and restart recovery

Cancel a running session through the owning Agent Team API:

```text
curl -fsS -X POST \
  "$AGENT_TEAM_URL/sessions/{session_id}/cancel"
curl -fsS "$AGENT_TEAM_URL/sessions/{session_id}"
```

Cancellation is best-effort at the API boundary, but the engine reaps worker
processes/tasks and releases its runtime slots. Inspect the durable session and
report rather than fabricating completion. Use reset only when intentionally
clearing the conversation and project association:

```text
curl -fsS -X POST \
  "$AGENT_TEAM_URL/sessions/{session_id}/reset"
```

The supported process model is one Agent Team runtime process per
`AGENT_TEAM_STATE_DIR`. The process holds `<state_dir>/runtime.lock`; a second
runtime fails closed. Never delete a live lock file to force startup. Stop the
owning service through s6, then start it again:

```text
s6-svc -d "$AGENT_TEAM_SERVICE_DEFINITION"
s6-svc -u "$AGENT_TEAM_SERVICE_DEFINITION"
s6-svc -s "$AGENT_TEAM_SERVICE_DEFINITION"
curl -fsS "$AGENT_TEAM_URL/ready"
```

After an orderly restart or process crash:

1. Keep the persistent state volume in place.
2. Confirm the service is using the same external `AGENT_TEAM_STATE_DIR`.
3. Check `/ready`, `/health`, and the session record.
4. Sessions left in `processing` are recovered as `blocked` with a restart
   error; they are not reported as completed.
5. Re-run the relevant exact-revision Pi status check and bootstrap/health
   checks before starting new work.
6. If a session needs to be retried, preserve its blocked record and create a
   deliberate reset/new session rather than rewriting history.

If a state or provenance file is corrupt, stop the service, back up the entire
external state directory, restore from the last known-good backup, and then run
the health and bootstrap checks. Do not delete the state directory or recreate
it inside the Git checkout as a shortcut.

## Operator checklist

Before declaring a revision promoted, confirm all of the following:

- `git status --porcelain` and `git diff --check` are clean for the intended
  desired-state changes.
- The candidate is identified by a full commit SHA.
- `python pi/manifest_validator.py pi/manifest.json` passes.
- The full test suite passes.
- Provenance records contain generator, validation, and candidate information;
  remote push remains disabled unless separately authorized.
- The Pi binary and Python environment were preinstalled externally.
- `agent-team bootstrap --json` is ready and has no missing prerequisites.
- `agent-team pi reconcile ... --dry-run --json` passes.
- Project-local trust was explicitly approved before the real reconcile.
- `agent-team pi status --json` reports no drift.
- `/v1/models`, `/health`, `/ready`, `/models`, and s6 status all pass.
- The deployed revision is recorded only after those checks.
- Persistent state remains outside the checkout and is backed up.

If any gate fails, stop at that gate, preserve the report/output, and correct the
external prerequisite or reviewed desired state. Never replace a failed check
with an implicit install, a mutable branch name, a short revision, or a manual
edit to runtime ownership state.
