# Owned model-selection and reconciliation contract

Decision record for [issue #5](https://github.com/MyCookie/pydantic-deep-swarm/issues/5),
under the [planning map #1](https://github.com/MyCookie/pydantic-deep-swarm/issues/1).
This is the agreed contract for the subsequent build effort, not a claim that
these commands already implement it. Current behavior is described in
[operations](operations.md); its source/runfile reconciliation must be replaced
before this contract can pass acceptance. The broader adapter boundary remains
[issue #7](https://github.com/MyCookie/pydantic-deep-swarm/issues/7).

## Language

**Model selection**: The operator's choice of an explicit model ID or automatic
selection for a role. _Avoid_: model default when referring to an explicit choice.

**Advertised model**: A distinct nonempty model ID reported by the configured
OpenAI-compatible endpoint. Advertisement is availability evidence, not permission
to replace an explicit selection.

**Effective role model**: The concrete model ID and endpoint obtained after
configuration inheritance, environment expansion, and automatic selection.

**Desired configuration**: The effective role models selected for the next
runtime activation. _Avoid_: live configuration for persisted desired state.

**Observed role model**: The model ID and endpoint reported by the running
Agent Team API for a role.

**Model drift**: A mismatch between desired configuration, endpoint availability,
and observed role models. Different roles deliberately using different models
are not drift.

**Pending activation**: Desired configuration has been committed but runtime
activation is required or has not been verified.

These model terms can be integrated into the repository glossary alongside the
ongoing issue #3 knowledge glossary; this record does not replace that glossary.

## Authority and inheritance

All startup and control commands use the canonical loader and the configuration
and runtime-path rules settled in [issue #2's resolution](https://github.com/MyCookie/pydantic-deep-swarm/issues/2#issuecomment-5920512614).
Configuration-file selection is `--config`, then `AGENT_TEAM_CONFIG_FILE`, then
`<selected-state-dir>/config/config.yaml`. Runtime `state_dir` and `workspace_dir`
are selected independently per field: CLI flag, YAML, environment, default.
State environment precedence is `AGENT_TEAM_STATE_DIR`, compatibility
`AGENT_TEAM_HOME/.agent-team`, then `~/.agent-team`; workspace defaults to
`<final-state-dir>/workspace`. Null workspace is omitted; null state is invalid.
A state flag does not discard an explicit YAML workspace. Environment-selected
state containing YAML that points to a different state fails instead of escaping
that selection. Relative flag/environment paths resolve against the invocation
working directory; expanded YAML path fields must be absolute. Generated YAML
persists absolute state/workspace paths. Both final runtime paths are validated
and must be outside the source checkout.

A named missing file is exact: every `swarm` command fails without searching or
bootstrapping another file; status still renders its configuration error. `init`
creates named missing files, while `serve` and `doctor` fail, as issue #2 defines.
Only when neither file selector names a file and the canonical file is absent
may swarm use environment/default bootstrap; reconciliation may create that
canonical file. Existing unreadable, malformed, or invalid files fail closed.
Reading a configuration inside the checkout is allowed; reconciliation may not
create or overwrite one there. Startup and control must receive matching absolute
paths and path-selection flags/environment.

Existing YAML is authoritative. Direct model environment variables do not
override it; environment values enter YAML only through its explicit bounded
`${VAR}`/`${VAR:-default}` expressions. Keep the existing expansion limits and
cycle detection. Missing/null values inherit as below; an explicit empty model
ID or endpoint is invalid. The literal `auto` is reserved for automatic model
selection and cannot name a deployment.

```yaml
models:
  default:
    model: auto
    base_url: ${LLM_BASE_URL}
  principal: {}
  manager:
    model: planner-model
  worker:
    model: execution-model
  curator: {}
```

`models.default` supplies the common selection and endpoint. Its absent/null
model is `auto`; its endpoint must be explicit or inherited through a YAML
expression, and cannot silently fall back to a deployment-specific hostname.
Principal, manager, and worker inherit missing/null model and endpoint fields
independently from `default`. Curator inherits those fields from worker, unless
explicitly overridden. Only principal, manager, worker, and curator are runtime
roles; `default` is configuration metadata. Existing YAML with four fully
specified roles remains compatible and does not require a default entry.

Without YAML, bootstrap uses `LLM_MODEL` (unset means `auto`) and required
`LLM_BASE_URL`, then `PRINCIPAL_MODEL`, `MANAGER_MODEL`, `WORKER_MODEL` and their
`*_BASE_URL` overrides. Unset curator fields inherit worker; `CURATOR_MODEL` and
`CURATOR_BASE_URL` independently override them. Empty supplied values are invalid.
No deployment model name is hard-coded. Credentials remain environment-only:
`LLM_API_KEY` goes only to inference HTTP clients, and `AGENT_TEAM_API_TOKEN`
goes only to the Agent Team API.

## Selection decision tree

For each distinct effective endpoint, query `/models` once per observation.
Normalize only trailing slashes when identifying endpoint equality. Require an
object with list-valued `data` and entries with nonempty string `id`; reject
malformed entries and reserved `auto`, and deduplicate exact IDs before counting.
Never choose the first item from a list. Discovery authentication, deadlines,
and failures apply equally to startup and control resolution.

| Selection | Zero distinct IDs | One distinct ID | Multiple distinct IDs |
| --- | --- | --- | --- |
| `auto` | Fail unresolved | Select the sole ID | Fail ambiguous; require an explicit selection |
| Explicit ID | Fail unavailable | Accept only if advertised | Accept only if advertised |

Explicit role choices survive implicit reconciliation even when every role
currently has the same ID. There is no inference that equal IDs are defaults.
A disappeared explicit deployment fails with an actionable explicit-selection
instruction; an advertised replacement does not authorize changing it. Distinct
role endpoints are resolved and checked separately. An unavailable discovery
endpoint leaves availability unknown and blocks configuration commits.

## Command contract

### `swarm detect`

Read configuration and query the common discovery endpoint (YAML default
endpoint, or the effective principal endpoint if default is absent; environment
common endpoint without YAML). Print the sole advertised ID. Zero or multiple
IDs and malformed/unavailable discovery return nonzero with a useful diagnostic.
Detect never updates YAML, environment, source code, or service definitions and
never requires a supervisor. It reports discovery rather than modifying role
choices.

### `swarm status`

Load and resolve the same configuration without persisting `auto`. Report the
configuration path and source (`yaml` or `environment`), configured selections,
effective role IDs/endpoints, advertised IDs per endpoint, observed API role
IDs/endpoints, health, readiness, drift, and resolution/verification errors.
Compare each API role with its own desired ID **and endpoint**. Extra advertised
IDs are not drift when all explicit selected IDs are available. Missing API roles
are drift. An API outage is an unknown observation, never synchronized state.

Status produces readable text or structured JSON even for zero/multiple models,
invalid configuration, or API/discovery failures; unresolved effective values
are null with diagnostics. It exits nonzero on any drift, unknown required
observation, unresolved selection, unhealthy liveness, or failed readiness.
It never writes or restarts. Live environment/process introspection is not a
core source of authority.

### `swarm reconcile`

With no override, retain every explicit role selection and resolve only `auto`
(or inherited `auto`). An explicit `--model ID` intentionally selects that ID
for all four roles, requires advertisement, and preserves endpoints. This
all-role override is rejected when effective role endpoints differ: operators
must edit role YAML explicitly rather than guess an override scope. `--model
auto` is invalid; automatic selection uses configured `auto`.

Validate every desired role against its endpoint before a write. Persist a
concrete model ID for each role whose selection was automatic; restarts then do
not silently adopt a newly advertised deployment. Keep explicit role values,
endpoint settings, environment expressions unrelated to changed selections, and
unrelated YAML data. If all existing YAML selections are explicit and valid,
implicit reconciliation leaves the original bytes unchanged. A bootstrap commit
creates the intended YAML with concrete role models, effective endpoints, and
final absolute state/workspace paths required by issue #2; it does not serialize
other incidental defaults, environment dumps, or credentials.
Startup and control must continue using the same state/path environment.

After persistence, activation is a separate phase. Dry-run performs discovery,
selection, and validation, reporting planned path/role changes and predicted
activation need; it creates no directories, locks, temporary files, YAML, or
restarts. `--no-restart` commits configuration and leaves activation pending.
`--no-verify` cannot produce `verified=true`.

## Atomic persistence and ownership

The only core mutation is one owned YAML file. Do not read source-code model
fallbacks or s6 runfiles as desired configuration, and do not modify either.
Do not edit the invoking process's environment or an operator's environment file.
The explicit path or canonical external state path identifies operator ownership;
reject targets within the repository and symlinked target/ancestor paths.

Serialize cooperating reconcilers with a cross-process lock outside the YAML
file, re-read under that lock, and validate the complete candidate through the
canonical loader before committing. Check that the destination still matches the
snapshot before replacement; on detected concurrent edits, fail without overwrite.
An advisory lock and snapshot check cannot eliminate a race with an external
editor that ignores the lock: operators must coordinate such edits. Do not claim
transactional protection against noncooperating writers.

If validated YAML is unchanged, real reconciliation still acquires the ownership
lock and re-reads, but skips temporary-file staging and replacement. It still
evaluates activation: unchanged YAML with known API drift may require restart.
A fully synchronized no-op retains YAML bytes and performs no restart; its
runtime observations must satisfy the synchronization requirements below.

Stage a temporary file in the destination directory, preserve an existing mode
(or create with `0600`), flush and fsync it, atomically replace the destination,
and fsync the directory. Create missing external parent directories only during
real reconciliation. A pre-replacement failure preserves the prior YAML and
prevents restart. A failure after replacement, including directory fsync failure,
reports whether commit occurred or is uncertain and requires a fresh observation;
it must not claim rollback. Clean up owned temporary files. No multi-file
transaction or source/runfile rollback is part of this contract.

## Activation, verification, and optional adapters

Retain the existing `Restartable` seam as an optional injected capability. The
core does not require s6 commands, service paths, or an installed supervisor for
detect, status, planning, or configuration persistence. An optional s6 adapter
may report service status and perform the bounded restart through its supplied
service directory/executable; it may not restore source/runfile model mutation.
Broad adapter design and other adapter capabilities belong to issue #7.

`restart_required` is true after a configuration commit changes desired models,
or when a known observed role model differs from desired configuration. It is
false only when observations establish no pending model activation. If no commit
changed configuration and the API cannot establish activation state, it is null
with an explicit unknown diagnostic. Liveness/readiness failure alone does not
prove that a model restart is required.

When restart is requested and capability exists, restart only for known pending
activation. With verification enabled, then poll bounded readiness and API role
ID/endpoint convergence.
Availability, health, readiness, and all role comparisons must pass for
`verified=true`; issuing a restart command alone is not verification. After
successful convergence, `restart_required=false` regardless of whether a commit
changed configuration or a restart was performed. A restart or verification
failure leaves the committed YAML in place, reports the failure and pending or
unknown activation, and returns nonzero; retry or manual activation is explicit.

Verification mode determines which runtime observations are required. With the
default verification enabled, query the Agent Team API and health/readiness even
when restart capability is absent or `--no-restart` is set. A preliminary API
outage does not erase known pending activation from a changed commit: an enabled
restart capability may restart, then bounded final observations decide the
result. With unchanged configuration and unknown API state, do not restart.
Only final successful observations clear a preliminary outage.

`--no-verify` deliberately skips API, liveness, readiness, and convergence queries;
discovery/selection validation remains mandatory. A changed commit establishes
`restart_required=true`; unchanged configuration gives null because activation
was not observed. Restart only when required is true and capability is available
and `--no-restart` is absent. After an unverified restart keep required=true;
never infer convergence from the restart command. Both flags together skip
runtime queries and restart. Status always verifies and does not accept these
reconcile-only flags.

Apply this outcome precedence after any commit (a persistence failure always
returns nonzero and prevents restart):

| Condition, in precedence order | Outcome / exit | Activation fields |
| --- | --- | --- |
| Discovery/configuration/selection failure | validation failure / nonzero | No commit or restart |
| Restart attempt fails | restart failure / nonzero | Committed YAML retained; required true, verified false |
| `--no-verify`, successful validation/commit/restart if attempted | `verification_skipped` / zero | Changed: required true; unchanged: null; verified false |
| Required final API observation unavailable | `verification_unavailable` / nonzero | Changed: required true; unchanged: null; verified false |
| Final liveness/readiness or attempted convergence fails | verification failure / nonzero | Required reflects known model drift; verified false |
| Remaining known model drift, restart absent or disabled, otherwise available and healthy observations | `restart_required` / zero | Required true, restarted false, verified false |
| All final availability, health/readiness and role comparisons pass | `synchronized` / zero | Required false, verified true |

Explicitly deferred activation is not a green live-runtime claim; status remains
nonzero until activation converges. API unavailability takes precedence over
missing capability or `--no-restart`; intentional `--no-verify` skips that query
and therefore takes precedence over a hypothetical outage. Dry-run queries
model discovery only, reports `outcome=planned`, and exits zero only when
selection/validation succeeds; runtime activation is predicted without API
verification or restart. JSON includes changed/planned paths, per-role selection
changes, commit state, restart/verification states, outcome, and errors.

## Acceptance specification for the subsequent build

Use deterministic endpoint and API transports, isolated external YAML paths, and
an injected restart capability. These are future acceptance gates, not tests
executed as part of this planning decision.

| Scenario | Required evidence |
| --- | --- |
| Unnamed canonical YAML absent, `LLM_MODEL=auto`, singleton | Same startup/control ID; real reconcile creates one concrete-role YAML, no secrets |
| YAML conflicting with direct environment | YAML wins; only explicit placeholders expand; same startup/control path |
| Zero/multiple IDs with `auto` | Detect fails; status renders unresolved diagnostics; reconcile writes/restarts nothing |
| Duplicate singleton, malformed/empty/reserved IDs | Dedup singleton accepted; invalid response fails closed |
| Multiple IDs, intentionally different roles | Per-role membership/API comparison passes; implicit reconcile preserves explicit YAML bytes |
| Explicit disappeared ID, new singleton | Fail unavailable without replacement; explicit override required |
| Different role endpoints, curator inheritance | Each endpoint queried separately; worker ID/endpoint inherited correctly; all-role override refused |
| API ID matches but endpoint differs, or role missing | Status drift; no synchronized/verified claim |
| Automatic concretization, later discovery changes | Persisted explicit selection retained; no silent deployment switch |
| Explicit `--model` same endpoint | All roles intentionally updated, endpoints and unrelated configuration retained |
| Dry-run | No writes/directories/locks/restarts; planned changes distinguishable from committed changes |
| Fully synchronized no-op | YAML bytes retained; ownership lock/re-read allowed; no staging/replacement/restart; healthy/ready observations and matching roles/endpoints |
| Named missing config vs unnamed absent config | All swarm commands fail named missing; only unnamed fallback can bootstrap/create; no alternate-file search |
| CLI/YAML/environment path cross-product | Issue #2 precedence per field, null rules, cwd relative flags/env, absolute YAML, env-selected state mismatch rejection; checkout config read allowed, mutation refused |
| Missing supervisor or `--no-restart`, changed/unchanged YAML, available API | Known drift or changed commit yields deferred zero; convergence yields synchronized zero; unhealthy/readiness failure remains nonzero |
| Missing supervisor or `--no-restart`, changed/unchanged YAML, unavailable API | Both return verification_unavailable nonzero; changed required=true, unchanged required=null |
| `--no-verify`, changed/unchanged × available/unavailable API × absent capability/`--no-restart` | No API/health/readiness queries in every combination; verification_skipped zero, required=true for changed/null for unchanged; no restart |
| `--no-verify`, restart capability enabled, changed/unchanged | Changed may restart but remains true/unverified; unchanged null never restarts; restart failure nonzero |
| Initial API outage, changed/unchanged, restart enabled | Changed known pending may restart and recover to synchronized; final outage nonzero; unchanged unknown cannot restart |
| Idempotent YAML but stale API | Pending activation retained; optional restart restores convergence |
| API unavailable with/without commit | No verified claim; restart need true after changed commit, otherwise unknown |
| Restart failure or convergence timeout | YAML retained; bounded retries, nonzero error and pending/unknown state |
| Readiness failure, matching model observations | Verification fails without treating health failure as model-change evidence |
| Temp/write/replace/fsync failures | Prior bytes retained before commit; post-commit/uncertain state reported accurately; no premature restart |
| Symlink/repository target, concurrent reconcilers/editor | Forbidden paths refused; cooperating lock serialized; detected snapshot change not overwritten |
| Existing mode/new file, arbitrary unrelated settings | Existing permissions retained; new file 0600; unrelated data/expressions retained |
| Optional s6 restart adapter | Injected absolute executable/service path; no source/runfile reads or writes in core |

The build effort should replace the old three-file reconciliation tests with
these behavioral gates, then run affected configuration/control/startup suites,
CLI exit/JSON tests, and the full isolated suite. Live supervisor/inference checks
remain an explicit deployment acceptance lane.

## Grilling resolution

| Round / question | Challenge and agreed decision |
| --- | --- |
| 1 / Q1 authority | Use the shared YAML-first loader/path; environment is bootstrap or explicit substitution only. |
| 1 / Q2 cardinality | Automatic selection requires exactly one distinct ID; explicit selections may coexist with many advertised IDs. |
| 1 / Q3 role intent | Rejected replacing uniform configured IDs: equality cannot prove inheritance; explicit role IDs remain intentional. |
| 1 / Q4 supervisor | Keep the optional Restartable seam; core config is YAML and read-only commands require no s6. |
| 2 / Q5 persistence | Freeze resolved automatic choices into concrete role fields; preserve unrelated YAML and bootstrap models/endpoints and required absolute runtime paths. |
| 2 / Q6 dry-run | Perform full resolution/validation with no filesystem mutation or restart. |
| 2 / Q7 atomicity | Validate then replace one YAML; serialize cooperating writers, detect edits, report post-commit uncertainty honestly. |
| 2 / Q8 deferred activation | Changed commit or known API model drift requires activation; unavailable observation is unknown, never green. |
| 2 / Q9 adapter compatibility | Optional adapter can restart/inspect service; source/runfile model mutation is not compatible core behavior. |
| 3 / Q10 inheritance and scope | Add models.default metadata; curator inherits worker fields, reserve auto, validate endpoints separately, refuse multi-endpoint all-role override. |


The independent reviewer flagged canonical path conflicts and ambiguous activation
precedence. A newly spawned pair completed a second session: round 1 settled
Q1 named-missing swarm failure, Q2 mandatory default runtime observations, and
Q3 intentional no-verify query skipping; round 2 settled Q4 recovery after a
changed commit despite an initial API outage and Q5 nonzero health/readiness
failures even with deferred activation. They confirmed shared understanding
before these edits. The acceptance matrix now covers both changed and unchanged
configuration, outages, missing capability, and both skip flags.

Two agents challenged authority, cardinality, role intent, endpoint scope,
persistence, failure atomicity, deferred activation, exit behavior, and adapter
compatibility across three numbered frontier rounds. They explicitly confirmed
shared understanding before recording this contract. The key rejected choices
were treating equal explicit role IDs as permission to replace them, choosing the
first advertised model, overlaying environment values onto authoritative YAML,
requiring s6 for read-only commands, and presenting deferred activation as live
verification. An independent reviewer must clear this decision record before
issue #5 is closed; runtime implementation remains the subsequent build effort.
