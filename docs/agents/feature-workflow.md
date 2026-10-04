# Subagent feature workflow

The coordinating agent owns dispatch, evidence and integration. Apply this
workflow to each feature, including bug fixes. Read [CONTEXT.md](../../CONTEXT.md)
and the affected contracts before choosing the public test seams.

1. Record the agreed scope, acceptance cases and seams, base SHA, and clean or
   dirty worktree state. Obtain seam confirmation before writing tests when
   required by the selected TDD skill. Locate the specification through the
   [issue-tracker guide](issue-tracker.md).
2. Dispatch an implementing subagent for the feature with exclusive file
   ownership. It writes one behavioral test at an agreed public seam, runs it
   red, records the expected failure, implements the smallest change, and runs
   it green. Repeat in vertical slices. Keep exact commands, exit codes and
   logs outside the checkout, tied to the tested revision or diff fingerprint.
3. Dispatch a different subagent for local review after implementation. Give it
   the feature specification, base revision, final diff and red/green evidence.
   It checks behavior, repository standards, safety boundaries and meaningful
   coverage. Resolve each actionable finding, rerun affected checks, and have
   that reviewer clear the final feature diff before integration.
4. Run affected checks and the full isolated suite, coverage ratchet, package
   and asset checks. Commit the reviewed feature. Open a PR only after every
   feature passes its local review and required local checks; fill the
   [PR template](../../.github/pull_request_template.md) with verifiable evidence.
5. Dispatch a fresh holistic reviewer after opening the PR. Supply only the
   originating specification, repository instructions, PR base and head SHA,
   accumulated diff and validation evidence, so its conclusions are independent
   of implementation discussion. It reviews interactions across all features,
   interfaces, packaging, platform claims, tests and the effectiveness of gates.
6. Resolve findings and re-review the final head after every push. A passing
   holistic review must name the exact head SHA. The trusted orchestrator then
   publishes the `Holistic review` commit status with a link to that evidence;
   Actions never publishes this attestation. Verify both required GitHub checks
   and resolved review findings before merging when merge is authorized.

Aim for at least 95% line and branch coverage with behavioral tests. Enforce the
measured ratchet declared in `pyproject.toml`; a target is not an achieved result.
Report the measurement scope, omitted subprocess execution, platform exclusions
and optional integrations using [testing guidance](../testing.md).

## GitHub enforcement

The checked-in [CI workflow](../../.github/workflows/ci.yml) is the source of
truth for platform/interpreter jobs and pinned tool versions. Its stable `PR
gate` fails if any required dependency fails, skips or is cancelled. Feature
subagent identity and red/green discipline are evidence reviewed by agents;
Actions verifies executable checks, not who performed the work.

[The branch ruleset](../../.github/branch-ruleset.json) requires a PR, successful
Actions `PR gate`, and `Holistic review` on the current SHA, with no bypass.
Applying this file to GitHub is a separate authorized repository operation;
its presence alone does not enable server-side enforcement. Update an existing
ruleset rather than creating duplicates. GitHub approving-review count is zero
because independent subagents operating through the same account cannot approve
their own PR as separate GitHub users; the SHA-scoped attestation represents the
actual independent review.

The authorized orchestrator uses the GitHub commit-status API only after review
passes. Write the exact JSON body to an external file containing `state:
"success"`, `context: "Holistic review"`, a concise `description`, and an HTTPS
`target_url` pointing to the retained review evidence. Submit it with:

```sh
gh api --method POST repos/MyCookie/pydantic-deep-swarm/statuses/REVIEWED_HEAD_SHA \
  --input /absolute/external/holistic-review-status.json
```

Verify that `REVIEWED_HEAD_SHA` is still the PR head immediately before submission.
New commits need new attestations. Never emit a success status as a placeholder
or infer passing review from missing findings before the reviewer finishes.
