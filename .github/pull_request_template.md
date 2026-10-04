Describe the concrete problem and resulting behavior, including the originating
issue or agreed specification.

## Feature evidence

For each feature, link the implementing subagent's red failure and green result,
the public seam tested, and the distinct local reviewer's findings and resolution.
Record commands, exit codes, and the revision or worktree fingerprint tested.

## Validation

Record affected checks and full-suite results, measured line/branch coverage,
platform exclusions, and any unverified capabilities. Link Actions artifacts.

## Fresh holistic review

After opening this PR, record a newly dispatched reviewer's accumulated-diff
review against the base revision, final PR head SHA, and resolved findings.
The trusted orchestrator records the SHA-scoped `Holistic review` status after
the review passes. Every new push requires fresh review of the new head.
