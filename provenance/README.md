# Git provenance

This directory defines the provenance record for swarm-generated assets and
Agent Team project work. Records are JSON and follow `schema.json`.

Each record captures:

- `base_revision`: the Git `HEAD` observed before the change, or `null` for an
  unborn repository;
- `candidate_revision`: the full candidate commit SHA, or a deterministic
  `worktree:<sha256>` fingerprint when the candidate is not committed;
- `deployed_revision`: the full committed revision actually deployed after an
  independent health/loading check. It is never inferred from the current
  `HEAD`, and a worktree fingerprint is not accepted as deployment evidence;
- `generated_by`: non-secret agent, model, session, task, and tool metadata;
- `assets`: optional repository-relative generated files with their captured
  SHA-256 digests;
- `validation_results`: named, reproducible validation observations; and
- the observed repository state and immutable policy metadata.

## Capture

Release-valid checks require complete validation evidence. Write a JSON object or
array outside the candidate tree with the exact command, concise result,
timezone-aware start time, and timezone-aware finish time:

```json
[
  {
    "name": "full-suite",
    "status": "passed",
    "command": ["python", "-m", "pytest", "tests", "-q"],
    "details": "All deterministic tests passed; live E2E was explicitly skipped",
    "started_at": "2026-09-18T00:00:00+00:00",
    "finished_at": "2026-09-18T00:01:00+00:00"
  }
]
```

Then capture the committed candidate from the repository root:

```text
REVISION="$(git rev-parse --verify HEAD^{commit})"
agent-team provenance capture \
  --repo-root . \
  --output "$AGENT_TEAM_STATE_DIR/provenance/run-001.json" \
  --record-id run-001 \
  --project-id GITOPS-5 \
  --agent swarm-worker \
  --model model-name \
  --task-id GITOPS-5 \
  --candidate-revision "$REVISION" \
  --validation-evidence "$AGENT_TEAM_STATE_DIR/provenance/validation.json" \
  --asset pi/manifest.json \
  --json
```

`--asset PATH` hashes the current regular file. `--asset PATH=SHA256` also
checks a caller-supplied digest. Asset paths must remain inside the repository
and cannot be symlinks, traversal paths, or absolute paths.

For an uncommitted candidate, capture the record outside the candidate tree if
you need to run `--check-candidate` later; writing the record itself changes a
worktree fingerprint. For a committed candidate, pass the full candidate SHA
with `--candidate-revision`; validation reads each recorded asset from that
exact commit rather than trusting potentially dirty worktree bytes.

Validate the record schema, evidence, repository association, candidate commit,
and every recorded asset digest with:

```text
agent-team provenance validate \
  "$AGENT_TEAM_STATE_DIR/provenance/run-001.json" \
  --repo-root . \
  --check-candidate
```

The shorter `--validation NAME=STATUS` form is suitable only for an observational
draft. A `passed` or `failed` release claim without command, details, and times is
rejected during validation. A skipped check requires an explanatory detail.

For a worktree candidate, add `--check-candidate` only while the recorded
fingerprint should still match the current files. Writing a record inside an
uncommitted tree changes that fingerprint, so release evidence should normally
be stored outside the candidate tree.

## Revision workflow

1. Capture the base revision before generation or editing.
2. Generate assets or project changes and run the required validations.
3. If an operator explicitly authorizes a local commit, commit the candidate
   with ordinary Git tooling and record its full SHA as `candidate_revision`.
4. Reconcile/install the trusted candidate. Record `deployed_revision` only
   after the deployment and health/loading checks pass.
5. Keep the record alongside release evidence, or store operational records in
   an ignored runtime state directory when they describe instance-only state.

## Commit and push policy

The provenance recorder is observational and fail-closed:

- it never runs `git add`, `git commit`, `git tag`, or `git push`;
- `commit_policy.auto_commit` is always `false` and commits are
  `operator-authorized-only`;
- `push_policy.remote_push_enabled` and `push_policy.auto_push` are always
  `false`; its mode is `disabled-unless-explicit-authorization`;
- any remote push, if later authorized, is a separate operator-controlled
  release action outside this recorder; and
- generated-by metadata and validation details must not contain credentials,
  tokens, private keys, or other secrets.

A successful capture means only that the local record was written atomically.
It does not claim that a commit was created, a remote was updated, or a
revision was deployed.
