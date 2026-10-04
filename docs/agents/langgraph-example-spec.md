# LangGraph swarm example

The user requested one short example under `examples/` or `integrations/` that
reimplements Principal → Manager → Worker as a LangGraph graph with a typed
handoff. The user confirmed the compiled graph's `invoke()` interface and the
runnable command-line example as the public test seams.

Review baseline: `ce3be5ba7668f0b462a90bcdffe2b148c020cf3a` (merged PR #9).
Initial worktree: no tracked changes; the user-owned untracked `diff` file is
preserved and excluded from this feature.

## Acceptance

- Provide a short executable example with real LangGraph nodes and edges:
  START → Principal → Manager → Worker → END.
- Reuse the repository's `ProjectBrief`, `WorkerTaskSpec`, and `WorkerResult`
  contracts. Give the Manager a brief and the Worker a bounded assignment.
- Use deterministic whitespace word counting to make the example runnable
  without model endpoints, credentials, or Agent Team runtime startup.
- Validate input and typed handoffs at runtime; restrict graph input to the
  caller's text and show typed handoffs through the public graph result.
- Run the module from the repository with a quoted text argument and print a
  JSON worker result. Document setup, graph invocation, and counting semantics.
- Install LangGraph for development and CI without adding it to production
  runtime dependencies. Retain a reproducible lock and required example tests.
- Test the confirmed seams with subagent red/green TDD, distinct local review,
  and fresh holistic PR review. Measure example coverage separately from the
  existing production coverage ratchet.

This is a teaching example of the delegated path. It does not claim to implement
production persistence, process confinement, scheduling, tools, or inference.
Node input schemas describe which state each node reads; they are not a security
boundary. Production runtime behavior is outside this change.
