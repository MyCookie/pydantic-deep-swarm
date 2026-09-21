# ProjectBrief contract

A `ProjectBrief` is the typed contract that defines what Agent Team is being
asked to accomplish. It is not a conversation transcript and it is not an
execution plan. The brief carries the objective, stable requirement and
acceptance identifiers, constraints, context, action boundaries, expected
output, and known ambiguities across the public request boundary.

The Team Manager converts a validated brief into a separate `ManagerPlan` that
defines how the project will be executed. The runtime later validates the
resulting `CompletionReport` against the original brief.

## Canonical contract

The canonical contract is the Pydantic `ProjectBrief` model in
[`src/agent_team/contracts/__init__.py`](../src/agent_team/contracts/__init__.py).
Its nested `Requirement` and `AcceptanceCriterion` models are defined in the
same module.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `objective` | string | yes | One high-level project goal. |
| `requirements` | list of `Requirement` | no; defaults to `[]` | Stable, individually reportable obligations. |
| `constraints` | list of strings | no; defaults to `[]` | Restrictions that planning and worker execution must respect. |
| `acceptance_criteria` | list of `AcceptanceCriterion` | no; defaults to `[]` | Explicit checks used to determine whether the result is acceptable. |
| `relevant_context` | list of strings | no; defaults to `[]` | Bounded context useful for planning or assignments. It is not raw conversation history. |
| `context_refs` | list of strings | no; defaults to `[]` | Opaque references to external or session context. Agent Team records them but does not automatically dereference them. |
| `permitted_actions` | list of strings | no; defaults to `[]` | Declarative actions the brief author permits. These do not grant runtime tools by themselves. |
| `prohibited_actions` | list of strings | no; defaults to `[]` | Declarative actions that planning must avoid. Runtime capability controls remain independently enforced. |
| `desired_output` | string | yes | Expected deliverable or response format. |
| `unresolved_ambiguities` | list of strings | no; defaults to `[]` | Known uncertainties supplied to planning. They are not automatically resolved or converted into report items. |

A `Requirement` contains:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Stable identifier used by tasks and requirement results. |
| `description` | string | yes | What must be accomplished. |
| `required` | boolean | no; defaults to `true` | Whether partial or unsatisfied completion blocks a complete result. |

An `AcceptanceCriterion` contains:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Stable identifier used by acceptance results. |
| `description` | string | yes | What constitutes acceptance. |
| `verification_method` | string or null | no | How the criterion should be verified. A criterion without a method cannot support a validated complete report. |

## Example

```json
{
  "objective": "Add bounded export support",
  "requirements": [
    {
      "id": "req-export",
      "description": "Export the selected records as JSON",
      "required": true
    },
    {
      "id": "req-docs",
      "description": "Document the public request and response shapes",
      "required": true
    }
  ],
  "constraints": [
    "Do not expose credentials or raw conversation history",
    "Do not add command or shell execution"
  ],
  "acceptance_criteria": [
    {
      "id": "ac-export-test",
      "description": "The deterministic export acceptance test passes",
      "verification_method": "Run the focused pytest case and record its output"
    }
  ],
  "relevant_context": [
    "Exports must remain beneath the configured artifact workspace"
  ],
  "context_refs": [
    "session:example-session",
    "issue:export-contract"
  ],
  "permitted_actions": [
    "Modify source, tests, and documentation in the assigned workspace"
  ],
  "prohibited_actions": [
    "Push a Git remote",
    "Read credentials"
  ],
  "desired_output": "A verified implementation with concise evidence",
  "unresolved_ambiguities": []
}
```

Use stable IDs. Worker tasks and final results refer to requirements and
acceptance criteria by ID, so changing an ID changes the contract rather than
merely editing prose.

## HTTP request envelope

An external client first creates or reuses a session with `POST /sessions`.
It may then submit an already-authored brief directly:

```http
POST /sessions/{session_id}/delegations
Content-Type: application/json
Authorization: Bearer <service token, when configured>
```

```json
{
  "brief": {
    "objective": "Add bounded export support",
    "requirements": [],
    "constraints": [],
    "acceptance_criteria": [],
    "relevant_context": [],
    "context_refs": [],
    "permitted_actions": [],
    "prohibited_actions": [],
    "desired_output": "A verified implementation with concise evidence",
    "unresolved_ambiguities": []
  }
}
```

The FastAPI envelope is `DelegationRequest`, whose `brief` field is a
`ProjectBrief`, in [`src/agent_team/app.py`](../src/agent_team/app.py). Invalid
Pydantic input is rejected before project execution. A direct conversation sent
to `POST /sessions/{session_id}/messages` can also produce a `ProjectBrief` as
the brief carried by a typed `PrincipalDecision`. Both paths converge on the
same project execution method; direct typed delegation does not repeat message
intake.

The response may include the normalized brief, generated plan, validated report,
project identifier, status, response text, and validation issues. Optional
adapters should return only the compact fields appropriate to their caller.

## Runtime lifecycle

1. **Validation and redaction** — FastAPI validates the request envelope. The
   service applies credential-shaped-content redaction before storing or
   executing the brief.
2. **Session serialization** — The delegation route acquires the session lock,
   records the bounded delegation event, and marks the session `processing`.
3. **Project creation** — The engine creates a project ID, starts the single
   project deadline, and writes the redacted brief to the external project
   store as the `brief` checkpoint.
4. **Planning** — The complete brief is included in the Team Manager's typed
   planning prompt. The returned `ManagerPlan` is validated and sanitized:
   requirement IDs must belong to the brief, roles and worker counts are
   bounded, and tool names must belong to the runtime allowlist.
5. **Worker assignment** — Each worker receives only its bounded assignment,
   the requirements assigned to that task, the project's acceptance criteria
   and verification methods, project constraints, compact dependency summaries,
   and bounded memory. Workers do not receive an unbounded transcript.
6. **Evidence collection** — Worker outputs use the exact requirement and
   criterion IDs. Runtime-owned tool evidence and artifact verification are
   attached before aggregation.
7. **Completion validation** — The runtime builds a `CompletionReport` and
   checks it against the original brief. Every brief requirement needs a
   corresponding result; required requirements must be satisfied with evidence;
   every acceptance criterion needs a passed result, a verification method, and
   evidence; and local artifact claims must pass runtime verification.
8. **Repair or downgrade** — The runtime may attempt one bounded report repair.
   If validation still fails, a report claiming `complete` is downgraded to
   `partial`, and the validation issues become unresolved items.
9. **Persistence and response** — The redacted brief, `ManagerPlan`,
   `CompletionReport`, and validation issues are persisted in external session
   and project state. The runtime renders the response from the validated report
   and then reaps project tasks, processes, registries, and deadlines.

## Validation semantics

The canonical model currently enforces:

- `objective` and `desired_output` must be present;
- `desired_output` has a minimum length of one character;
- requirement and acceptance-criterion IDs must be nonempty after trimming for
  validation; and
- IDs must be unique within each list after trimming for comparison.

Nested requirement and criterion descriptions also have a minimum length of one
character. The model does not currently trim stored values.

There are two strictness differences to account for when writing clients:

- the canonical Pydantic model currently accepts an empty or whitespace-only
  `objective` and a whitespace-only `desired_output`, while the optional adapter
  rejects both after trimming; and
- the adapter's declared JSON Schema rejects additional properties, while the
  canonical Pydantic model currently ignores unknown fields.

Clients should follow the stricter behavior: send a nonblank `objective` and
`desired_output`, use only declared fields, and assign unique nonblank IDs.
The Pydantic contract remains the runtime source of truth; separately declared
adapter schemas must be kept aligned with it.

## Enforcement boundaries

`permitted_actions` and `prohibited_actions` express project policy, but they are
not capability tokens. Actual worker authority comes from the sanitized
`ManagerPlan`, each `WorkerTaskSpec.tools` list, the fixed runtime worker-tool
allowlist, and workspace/path enforcement. A brief cannot grant command, shell,
Python, Git, network, credential, or host-agent access that the runtime does not
implement.

Similarly, `context_refs` are provenance-oriented references rather than an
instruction to fetch arbitrary data. Any referenced content must be resolved by
an authorized caller or explicitly supported runtime boundary before it becomes
worker context.

## Data handling guidance

Keep the brief compact and task-specific:

- use stable IDs rather than relying on list position;
- include verification methods for every acceptance criterion;
- state permissions and prohibitions explicitly, even though runtime controls
  remain authoritative;
- put only bounded, necessary information in `relevant_context`;
- use `context_refs` for provenance instead of pasting raw documents or
  transcripts;
- never include credentials or secrets; redaction is defense in depth, not a
  transport mechanism for sensitive values; and
- leave genuine uncertainty in `unresolved_ambiguities` rather than inventing a
  decision.

See the [end-to-end workflow](end-to-end-workflow.md) for the surrounding
request lifecycle and [architecture](architecture.md) for the typed boundary,
worker capability, state, and evidence model.
