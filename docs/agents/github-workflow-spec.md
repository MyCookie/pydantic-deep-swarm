# GitHub workflow specification

User-confirmed scope: implement GitHub workflows and PR gates, with every
feature implemented by a subagent using red/green TDD, reviewed locally by a
different subagent, followed by a PR and a fresh holistic review of the final
head. The fixed review baseline for this change is `37501dc`. The agreed public
seams are subprocess confinement/refusal, isolated test inventory, coverage
measurement/gating, and the aggregate CI gate command.

Acceptance obligations:

- Preserve fail-closed child launching. Darwin native confinement positives
  remain required on Darwin and are explicitly excluded off-host; portable
  success makes no claim that native confinement was executed. Keep socket
  temporary paths short and portable.
- Run locked isolated deterministic tests and measured line/branch coverage on
  Linux Python 3.11 and 3.14. Enforce honest baseline floors while targeting
  at least 95% of each; report parent-interpreter-only measurement honestly.
- Gate assets, compilation, lock/dependencies, workflow syntax and builds;
  exercise the separately installed wheel with hash-locked runtime dependencies.
- Run Python 3.11 native macOS standalone acceptance on a clean committed
  candidate with fresh-clone/wheel, real-process confinement and registry proof.
- Trigger CI for PRs, master pushes, manual runs and merge groups without path
  exclusions. Use pinned Actions/toolchain, read-only permissions, no secrets,
  and retained failure evidence. The stable `PR gate` rejects missing, failed,
  skipped or cancelled required jobs.
- Preserve per-feature red/green and independent local review evidence. Open
  the PR after local review passes, review the accumulated diff independently
  after opening, and re-review new pushes. Server-side gates require `PR gate`
  and truthful `Holistic review` status on the exact head SHA.

The baseline deterministic suite had 828 passed, four Linux failures involving
Darwin-only expectations or paths, and three optional skips. That historical
result is not evidence for the modified revision. Record current counts,
coverage and all platform limitations in fresh validation evidence.
