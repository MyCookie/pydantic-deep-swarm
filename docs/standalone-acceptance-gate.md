# Standalone acceptance gate

This is the decision contract for [issue #8](https://github.com/MyCookie/pydantic-deep-swarm/issues/8), under the [planning map #1](https://github.com/MyCookie/pydantic-deep-swarm/issues/1). It specifies the acceptance runner and evidence required in the subsequent runtime build. Closing this planning issue does not mean that runner exists or that the runtime passes it. Documentation publication checks are separate evidence. Hermes cutover and image automation remain subsequent work.

The gate composes the accepted [path authority #2](https://github.com/MyCookie/pydantic-deep-swarm/issues/2#issuecomment-5920512614), [knowledge migration #3](https://github.com/MyCookie/pydantic-deep-swarm/issues/3#issuecomment-5936935411), [foreground lifecycle #4](foreground-serve-contract.md), [model selection #5](model-selection-contract.md), [doctor #6](https://github.com/MyCookie/pydantic-deep-swarm/issues/6#issuecomment-5938241501), and [optional adapters #7](https://github.com/MyCookie/pydantic-deep-swarm/issues/7). Their runtime semantics remain authoritative; this document specifies how to prove them.

## Gate outcome and lanes

The required standalone lane uses a clean checkout, installed console script, isolated runtime directories, real local HTTP listeners, and a deterministic fake model. It must work without Hermes, Pi, s6, Docker, root, or an externally prepared virtual environment. Typed mocks and ASGI tests remain useful required suite coverage, but cannot substitute for installed-CLI, actual-listener, real-process, signal, or migration evidence.

Every required case has a stable ID and outcome `passed`, `failed`, or `unverified`. Missing capability, missing evidence, required skip/xfail, incomplete collection, or zero selected cases is unverified and blocks acceptance. A negative case passes when the asserted rejection and preservation are proved. An expected doctor exit 2 can therefore be a passed case; it is different from an unverified acceptance case. XPASS requires investigation and cannot silently turn a required expectation green.

Optional actual-integration lanes have `passed`, `failed`, or `not_selected`. Not selected must list the exact exclusions. Once selected, a missing prerequisite, unavailable endpoint, timeout, or unexpected response fails that lane; no catch-and-skip fallback is allowed. A passing standalone lane does not assert deployment compatibility for a failed or unselected integration lane.

The subsequent implementation must publish an executable case registry mapping every required row below and every acceptance bullet in #2–#7 to actual test node IDs/runner cases. A link to a contract alone is not executed coverage. Each case specifies setup, operation, expected exit/events/content, filesystem changes allowed, deadline, and cleanup. Enumerated cross-products must have concrete case IDs; report missing combinations as unverified.

## Installation and isolation

Use a fresh Git clone at the full candidate commit SHA, without copying an existing `.venv`, source tree overlays, or developer configuration. A managed worktree is useful for development but is not the fresh-clone installation proof. Record the origin and exact revision; a local clone must avoid hard-linked working files. Reject an unintended dirty candidate rather than silently testing a different tree.

The minimum-runtime gate uses Python 3.11, matching `requires-python >=3.11`. Pin and report the exact Python patch and uv version used. Run the same required gate on each platform/Python combination claimed as supported; one combination does not prove every interpreter satisfying the metadata. Git, Python, uv, and declared test utilities are provisioning prerequisites. Runtime ownership/signal/locking/confinement capabilities are tested explicitly, not inferred from their presence.

Create a per-run external root containing the clone, a neutral invocation directory, isolated HOME, XDG config/data/cache, temporary files, uv cache, fixture controls, reports, and per-case state/workspace. Each case receives fresh state and workspace unless it explicitly tests restart/recovery. Seed harmless sentinel files under the fake HOME's default `.agent-team` and `.hermes` paths in pollution cases; fresh-path cases start with those locations absent and record that absence. Never use the user's actual home or deployed state as fixture input.

The controller sources the authoritative local session environment as required by the machine's instructions. It then constructs child environments from an allowlist, rather than forwarding the sourced environment. Keep only required OS/runtime keys and case-owned absolute paths. Unset `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, inherited Agent Team/role/model selectors, proxy settings, deployment addresses, tokens, and unrelated uv/Python overrides. Supply a private PATH containing only declared required executables; assert Pi, s6, Hermes, and Docker are unavailable there. Absolute child executables are also controlled. Fixture credentials are newly generated sentinel values, never production secrets.

Provisioning may download the pinned interpreter and exact locked packages from declared registries. Record whether caches/network were used and all dependency identities. After provisioning, runtime HTTP targets are exclusively allocated loopback fixtures. Strict request validation and connection logs prove the tested request paths. If OS network confinement is used, record its enforcement; environment filtering or an audit alone does not prove universal network isolation. Unsupported network enforcement must be disclosed. Enforced child durable-write confinement from #4 remains mandatory and cannot be replaced by this network qualification.

### Clone lane

Run `uv sync --frozen` in the fresh checkout with its selected Python 3.11 interpreter, creating that checkout's own `.venv`. Do not use `PYTHONPATH=src`, an inherited environment, or an existing external venv to make installation work. Assert success, unchanged lock/source bytes, `uv pip check`, and the installed `agent-team` entry point. Record interpreter, module origins, installed package versions, lock digest, and installation exit codes.

Invoke the absolute installed `.venv/bin/agent-team` from both the checkout and the neutral directory. Config/state/workspace paths must still follow #2. Run doctor with explicit repository/revision inputs where needed to separate the invocation directory from the selected repository. The deterministic test suite runs using the newly installed Python and its locked dev dependencies, without a source-path override. Compile source/tests/Pi validation code, validate the Pi manifest and s6 shell syntax without invoking either runtime, and perform lock/dependency/build checks from [testing](testing.md).

### Wheel lane

Build a wheel from the same clean candidate using `uv build --no-sources` and record its hash plus the build backend/version. This additional distribution check never replaces clone `uv sync --frozen`.

Export runtime dependencies using `uv export --frozen --no-dev --no-emit-project --format requirements.txt`, preserving hashes. Create a second empty venv with the same interpreter. Install the exported dependencies with `uv pip sync --require-hashes --python <wheel-venv-python> <exported-requirements>` and install the wheel with `uv pip install --no-deps --python <wheel-venv-python> <wheel>`. Record the exact installed versions against the applicable lock markers and verify dependencies. A new resolution selecting different versions is not equivalent evidence.

Invoke its absolute console script from a neutral directory with source imports unavailable. Assert `agent_team` resolves under this venv's site-packages, not the checkout/editable source. The repository can remain accessible solely as doctor's explicit `--repo-root` metadata input; that does not permit importing it. Run installed init/serve/doctor and the deterministic fake-HTTP roundtrip, stop/restart, and pollution oracles in this lane too. Record any build-install failure as a required failure.

## Historical escape proof and path cases

The immutable defect baseline is `b3c0d2263b8a606efc0caa849876d0fb1bd6ef00`. Its `init` template writes `runtime.state_dir: ~/.agent-team` despite initialization selecting `AGENT_TEAM_STATE_DIR`. The candidate at the planning revision already serializes selected configuration, so reproducing the defect must use the historical revision, never describe the candidate as still having that template.

Provision the baseline independently. In a new case with empty selected external state and fake HOME, unset config/home compatibility selectors; set `AGENT_TEAM_STATE_DIR`, fixture `LLM_BASE_URL`, and explicit valid principal/manager/worker/curator role IDs/endpoints so missing role variables cannot mask the path bug. Run installed baseline `agent-team init`. Record selected path, emitted/generated YAML, and before/after manifests. In a new process with the same environment, invoke its canonical `get_config()` read-only and compare the resolved state with the requested selected path. The oracle must observe default-HOME redirection and fail its equality assertion. Store this as `baseline_escape: reproduced_red`, with exact commands and observed paths, not a passing standalone result.

The baseline lacks the new serve/doctor interfaces, so this narrow loader probe is an explicitly reported historical-interface deviation. Do not run a deployed runtime to reproduce it. In the candidate, run the same selected-path equality and pollution oracle through public init→serve→doctor and require success. Also deliberately place conflicting YAML under the environment-selected state: expect #2 refusal rather than silently following it. Both red reproduction and candidate positive/negative checks are mandatory subsequent-build evidence; this planning change has not executed them.

| Required path case group | Oracle |
| --- | --- |
| Config flag / config environment / unnamed canonical file | Exact selection priority; named missing init creates it, serve/doctor fail; unnamed absence permits the specified fallback, no alternate search. |
| State and workspace flag / YAML / environment / default | Full per-field cross-product; omitted workspace and `null` workspace inherit, null state fails; workspace default uses final state. |
| Environment compatibility | Primary `AGENT_TEAM_STATE_DIR`, then `AGENT_TEAM_HOME/.agent-team`, then fake HOME default, when higher-authority values are absent. |
| Relative flag/environment / YAML expressions | Resolve flag/env against invocation cwd; generated paths absolute; expanded YAML paths absolute; bounded existing substitution grammar and invalid expansion errors. |
| State flag with explicit YAML workspace | Flag wins state for this invocation while YAML workspace stays selected and independently validated. |
| Environment-selected state with conflicting YAML state | Fail path escape; no durable application-state mutation or default-home writes. |
| Repeat init / selected-path mismatch / overwrite | Repeat preserves existing YAML bytes; explicit mismatch/missing persistence fails; overwrite backs up and atomically replaces whole YAML; injected failures respect reported commit state. |
| Source paths and aliases | State/workspace inside checkout rejected; serve/doctor may read checkout config, init cannot create/overwrite it there; resolved alias safety follows the owning contracts. |

Manifests record path inventories, regular/symlink type, content hashes, modes, file identities where relevant, and SQLite `-wal`, `-shm`, `-journal` companions. Compare the checkout, fake HOME/default-state sentinels, selected state, workspace, and recovery directories. Separate harness-owned `.venv`, caches, build products, reports, and scratch with explicit allowed locations; do not silently exempt all untracked or ignored files. Source files and default-home sentinels must remain unchanged. Runtime writes must occur only at the selected external paths, with migration/lease exceptions defined by their contracts.

## Deterministic HTTP model

Run a versioned fixture server at `http://127.0.0.1:<allocated-port>/v1`. It implements actual `GET /v1/models` and `POST /v1/chat/completions`; no external inference is required. Use independent listeners for distinct-endpoint cases. Capture sanitized request metadata and bounded control-channel handshakes separately from runtime output.

Catalog scenarios include singleton; duplicate singleton; empty; multiple distinct IDs; explicit present/missing IDs; malformed JSON/object/list/ID; empty/nonstring/reserved `auto` ID; HTTP/auth denial; connection/timeout; and barrier-controlled blocked response. Count one catalog request per distinct trailing-slash-normalized effective endpoint **per command observation**, not once across the entire smoke sequence. Startup and doctor perform catalog checks without inference. Test principal/manager/worker and all-three-switch-enabled curator; inactive curator is neither probed nor required. Neither API sentinel nor inference sentinel may appear on discovery requests.

The production client is [SimpleChatModel](../src/agent_team/models/http_model.py). The fixture must validate advertised exact `model`, a list of role/content `messages`, and `stream: false`. Inference Bearer auth contains only the fixture LLM sentinel when selected; the Agent Team API sentinel is accepted only by Agent Team probes/requests. Structured JSON schema instructions appear inside system content; do not require native OpenAI `response_format` or native tool-call fields that this client does not send.

The response envelope is `{"choices":[{"message":{"content":"<text>"}}]}`. Content is a string: serialize typed `PrincipalDecision`, `ManagerPlan`, and `WorkerOutput` fixture payloads into that string. A worker tool iteration's decoded `choices[0].message.content` string is `{"tool_calls":[{"name":"write_file","arguments":{"path":"acceptance.txt","content":"standalone gate\n"}}]}`. Its inner JSON uses one `\n` escape for a trailing LF; outer response serialization escapes that backslash again as `\\n`. `SimpleChatModel` decodes the outer HTTP JSON, then `ToolAwareWorkerAgent` parses the inner JSON before the tool writes UTF-8 bytes. Construct the nested response by serialization, for example:

```python
import json

artifact_bytes = bytes.fromhex("7374616e64616c6f6e6520676174650a")
payload = {"tool_calls": [{"name": "write_file", "arguments": {
    "path": "acceptance.txt", "content": artifact_bytes.decode("utf-8")
}}]}
inner = json.dumps(payload)
outer = json.dumps({"choices": [{"message": {"content": inner}}]})
decoded = json.loads(json.loads(outer)["choices"][0]["message"]["content"])
assert decoded["tool_calls"][0]["arguments"]["content"].encode("utf-8") == artifact_bytes
```

The next request contains a user `TOOL RESULTS` message. This is the existing worker content protocol, not native OpenAI tool calls.

Use an explicit finite-state request script keyed by scenario marker, semantic schema/prompt, role, and task. Model ID alone cannot identify roles when they share one model. Independent workers may interleave only as explicitly allowed by their per-task state machines. Unknown requests fail the fixture and the harness; missing or extra expected calls fail even if a runtime fallback produces a superficially successful response. Never return a generic success for unexpected traffic. Assert the happy path uses the intended requests and valid schemas rather than fallback intake, staffing, report repair, or rendering.

## Canonical CLI smoke sequence

Use explicit `memory.enabled=true`, `shared_knowledge=true`, `curator_enabled=false` for this sequence; dedicated fixtures cover curator-enabled inheritance and discovery. Set automatic model selection and advertise exactly one fixture ID. Supply external paths and loopback host/port. Repeat with fixture API authentication to prove health/readiness/work requests reject absent/wrong tokens without leaking them.

1. Installed `init` succeeds and persists final absolute state/workspace paths. Repeat init preserves YAML. Record all selected paths.
2. Fresh stopped doctor with enabled knowledge and no usable existing lock exits 2 / unverified knowledge inspection, nonmutating. Do not create a lock just to make doctor green.
3. Installed foreground `serve` starts. Observe one JSON `serve_ready` on stdout only after every startup gate and actual intended listener binding. Authorized `/health` and `/ready` return the exact #4 bodies; startup performs no completions. Record effective PID/host/port/state and shared selected paths.
4. Create a session with `POST /sessions`, submit a direct turn to `/sessions/<id>/messages`, and assert the expected answer plus no manager/worker calls. Create another session for a typed delegated brief. The scripted manager grants a worker `write_file` and `read_file`; execute the real tool loop producing `acceptance.txt` with Python byte literal `b"standalone gate\n"`: exactly 16 UTF-8 bytes, ending in one LF (`0x0a`), with SHA-256 `a708b4e69f16df1936fcab90033eec606052015046e8cec350a06b8cad6533d2`. The artifact contains no literal backslash followed by `n`. Assert its actual bytes/size/SHA-256, permitted workspace containment, runtime-verified artifact provenance, a complete typed report with exact requirement/acceptance IDs and tool-derived evidence, stored session/report, and no raw internal chatter in the user answer. Read session/report through the public endpoints.
5. Busy enabled-knowledge doctor exits 2 with `inspection_deferred`, even when readiness is 200. Runtime correlation remains unknown: readiness does not identify the selected state owner. Manifests/owner metadata prove doctor nonmutation.
6. Send real SIGTERM to the owner PID; assert clean stopped events and exit 143, bounded quiescence and reap, released lease, preserved session/report/artifact. A separately launched successor immediately acquires the same state and reads the persisted session/report without rerunning completed work or replaying inference. New work still succeeds.
7. Stop successor, run stopped/free-guard doctor, assert exit 0 with safely inspected canonical knowledge and all required checks verified. Compare manifests after doctor. Core stopped readiness connectivity failure is normal and cannot override valid stopped state.

Also test knowledge-disabled stopped/running doctor exit 0 when all its required checks pass; knowledge is not opened. An unrelated ready endpoint cannot upgrade missing-lock/busy enabled knowledge to validated. Verify #6 failure/unverified precedence, JSON/text/report behavior, usage errors versus diagnostic errors, report-write failures, scrubbed child environments, and legacy bootstrap compatibility.

## Real-process lifecycle matrix

Use barrier handshakes to observe the intended phase; fixed sleeps and HTTP 200 alone are insufficient. Reserve/allocate loopback ports, start listeners, and verify actual binds; on a race, perform a bounded recorded retry or fail, never skip. Stream and parse stdout/stderr so a deadlock cannot be hidden by buffered output. For short process cases select 5-second startup and 5-second shutdown budgets, with one 20-second outer deadline including controller teardown/reap. Record actual durations using a monotonic clock. Longer storage cases declare their finite outer limit in the case registry; #3's total 30-second operation limit and a shorter enclosing deadline compose, never restart per retry. These fixture limits do not change #4's runtime defaults of 60/30 seconds.

| Required process case | Evidence |
| --- | --- |
| Same state contenders / independent state | Two real processes; immediate `runtime_in_use` without incumbent state/metadata changes; other state starts independently; clean stop permits reacquisition. |
| Invalid preflight / startup phase faults / bind collision | #4 exact reason/phase, exit 1, no ready event, allowed lease/recovery evidence only, resources unwound. Cover models, knowledge, recovery, retention, engine, listener and total startup expiry. |
| SIGINT and SIGTERM during startup | Block a leased catalog request with a handshake, send each signal to actual owner; stop wins readiness race, no ready event, cleanup quiesces, exits 130/143, lease reusable. |
| SIGINT and SIGTERM during active work | Block a known in-flight completion after observed worker request; send each signal, stop admission, cancel work/model requests, join tasks/reap tracked children, clear runtime state, release ownership after quiescence, exits 130/143. |
| Cleanup exception / shutdown deadline / resistant initializer | `shutdown_failed`, exit 1 and bounded safe owner termination; no initializer continues durable mutation after lease release. Independently verify successor ownership and safe writes. |
| Detached TERM-resistant child | A `start_new_session` child continually writes old-generation scratch, attempts state/store/workspace/artifact writes and buffered commits. Enforced direct durable writes are denied; parent-mediated commits require live generation/ownership/nonstopping checks. |
| Forced timeout / second signal with detached child | Observe first stop request then send second signal or expire shutdown. Reap owner; successor rejects old buffered commits and old child cannot change durable state. A surviving child may write only old scratch; do not claim all descendants reaped or graceful finalization. Controller tracks and ultimately terminates fixture leftovers. |
| Unsupported child-confinement mode | Runtime refuses uncontained modes; assert refusal and no durable mutation. If the claimed platform cannot prove safe allowed modes/refusal, mark required gate unverified and block its promotion. |
| Liveness/readiness/auth/admission | No HTTP-triggered lazy initialization; draining rejects new work. Stopped/startup disconnected probes are allowed. Post-start model outage leaves local readiness semantics unchanged while inference exposes its error. |

Report exact event order, signal timestamps, actual PIDs/process groups, exits, deadline enforcement, model-request cancellation observations, file-write/commit generation oracles, and ownership reacquisition. Parent exit, a scratch cwd, or environment scrubbing alone cannot establish child durable-write safety. SIGINT/SIGTERM capability absent on a claimed target is required-unverified, not an optional live skip.

## Knowledge migration and nonmutation matrix

All #3 acceptance branches are mandatory deterministic fixtures. Construct expected histories with independent raw SQLite SQL and compare every persisted value **and type**, including superseded rows, timestamps, tags/JSON text, confidence, source run/artifact/artifacts, and secret-shaped text. Do not use production `KnowledgeStore.add`, `update`, `KnowledgeRecord.from_dict`, or redaction/conversion to build the oracle. Both supported 13-/14-column shapes are covered; the only equality normalization is the missing `source_artifacts` column's literal SQL default `'[]'`. Physical bundle fingerprints prove source stability separately from full logical equality. Counts alone prove neither.

| Required fixture group | Oracle and mapping to #3 |
| --- | --- |
| No bundles / legacy-only / canonical-only | Fresh staged creation; legacy migration; unchanged canonical authority. No unexplained zero-byte creation. |
| Both empty / each direction of empty peer / identical populated peers | Correct reconcile winner; snapshots of both inputs, complete history preserved, distinct archive entries. Retained current canonical winner keeps inode and expected WAL/SHM until normal store operation. |
| Divergence | Different IDs and same ID differing in each persisted field/type fail ambiguous; include superseded/full-provenance cases, conservative inequality, no merge or empty fallback. |
| Supported schema upgrade / invalid schema or data | Staged-only supported upgrade preserves raw history and old snapshot; reject unknown objects, unrecognized SQLite, zero-byte/corrupt files, invalid rows/references. |
| Committed WAL / hot rollback journal | Complete coherent bundles include committed WAL-only data; recover hot journals privately, never recover originals for doctor; verified restore-ready snapshots before source retirement. |
| Unsafe paths / aliases / orphan companions | Reject symlink descendants, nonregular candidates/sidecars, hard links/candidate aliases, escaped artifacts, orphan WAL/SHM/journal; preserve evidence. |
| Every irreversible site and durability boundary | Inject before and after every actual journal intent/result write, snapshot/stage sync, individual database/sidecar retirement move, publication rename, directory sync, phase update, completion/archive cleanup. Restart rolls forward only from verified evidence; retained-winner recovery proves unchanged bundle instead of requiring rename. |
| Recovery-evidence failure | Unsupported/malformed/multiple journals, changed fingerprints, missing candidate/snapshot/archive, unprovable partial move, unexpected canonical contents/sidecars fail recovery_required with preserved material, no serving/retention. |
| I/O and budget failures | Disk full, permissions, lock contention, changed sources, deadline expiry and incomplete archives are bounded; keep last durable phase, no invented rollback, retry idempotent, archive permissions never broaden. |
| Completed authority and newer writes | Write new canonical records after completion, restart uses them. Completed audit never auto-restores old snapshots. Missing/corrupt canonical blocks restoration and reappeared legacy selection when prior authority exists. |
| Damaged archive audit | Missing canonical plus recognizable nonempty damaged archive evidence blocks `evidence_unverified`; empty backup directories alone permit fresh state. Valid canonical with historical audit damage remains authoritative with warning. |
| Runtime consumers / disabled knowledge / retention | App, engine, retention and optional s6 use one canonical preparation boundary. Init/bootstrap/s6 init create no competing stores. Disabled startup opens/creates/migrates nothing in knowledge. Retention starts only after completion, never touches archives; unchanged repeat startup does not migrate again. |
| Doctor inspection | Existing nonmutating guard, busy runtime/migration deferred, missing/unusable lock or unsupported safe inspection unavailable, valid pending recovery reported. Complete private copies outside state; hashes/inventory/modes/owner metadata/sidecars unchanged; original SQLite never opened/checkpointed, private temps securely cleaned. |

The executable registry must enumerate exact fixture IDs and every actual implementation fault site. New or uncovered irreversible sites fail coverage; do not replace exhaustive boundary injection with a few representative crashes. Expected negative outcomes are checked against independent before/after logical and physical manifests, allowing only contract-defined lease/recovery changes. Verify backup durability/self-containment, collision-free legacy-root/canonical-nested archive names, preserved originals/snapshots, journal phases and published authority, not just a reported success label. Ordinary retention effects and migration preservation are reported separately. These are acceptance requirements, not claims that migration exists in the planning checkout.

## Required suites and optional actual integrations

Run the full installed deterministic suite plus the real-process/HTTP/migration cases above. Require configuration/path, CLI exit/output, lease/ownership, process memory safety, worker grants/turns/cancellation, typed contracts, model selection/control atomicity/convergence, knowledge/recovery/retention, auth/readiness, secret boundaries, repository-local adapters, package assets and publication checks. #5's acceptance cross-products are required: changed/unchanged config, verification flags, absent/injected restart capability, API outage/drift/readiness, role endpoints/inheritance, reconciliation lock/concurrency/fsync failures and exact no-mutation dry run. #6's Pi/s6 behavior uses deterministic fake executables/status transports without installed services, including strict Pi SemVer floor parsing and adapter scope/requiredness; all source minima are validated. #7 optionality must be proved with optional binaries, deployment assets, and adapter environment inputs absent.

Give the required optional-asset-absence fixture its own case IDs. Use a separately identified candidate-derived installation and asset-stripped repository, with unchanged core Python code/dependencies and a complete removal/diff manifest tied to the original candidate SHA. Remove optional `s6-service/`, Pi deployment assets under `pi/`, Hermes deployment assets under `integrations/hermes/`, and installed/live copies; inventory any additional packaged or deployed copies. Unset supervisor/service/runfile/executable and other adapter/deployment selectors; retain only required core fixture inputs such as loopback model/API URLs and external state/workspace. Assert installed `init`, foreground `serve`, core `doctor`, model discovery, and core detect/status/planning/YAML configuration persistence work without restart capability. Core status reports supervision `not_configured` with null runfile fields and no missing-supervisor drift. The existing s6-only compatibility `swarm reconcile` must refuse the absent profile without mutation; #5's subsequent core YAML reconciliation has its own required cases.

Doctor may use the asset-stripped repository through explicit `--repo-root` solely for its required revision/core-layout checks. Retain full candidate Git revision metadata and required core source/packaging layout, report the deliberately dirty fixture warning and exact delta fingerprint, and scope optional adapter checks `not_selected`; do not waive required core checks. Source imports remain unavailable to the installed runtime. File-access evidence must prove no successful optional-asset read or fallback through intact clone, build/install copies, parent directories, or Git object contents; before/after inventories prove absence and preservation. Require the same knowledge-dependent doctor exits as the canonical sequence. This explicitly derived absence fixture does not replace or alter the untouched fresh-clone/wheel installation proofs or normal Pi manifest/s6 shell validation.

Inventory every collected/deselected/skipped/xfail/XPASS node and reason; assign a lane or required equivalent before aggregation. Current known examples are:

- `tests/test_agent_memory_safety.py::test_process_writes_reload_under_lock_without_lost_updates` skips without `fork` (`requires a process model with inherited test fixtures`). Its real-file-backed process safety is required. On a supported non-fork platform, execute and name an equivalent portable/spawn subprocess oracle; map the old skipped node to that passing replacement. Without that proof the gate is unverified.
- `tests/test_principal_manager_architecture.py::test_principal_plugin_registers_tool_for_normal_agent` skips absent installed plugin (`plugin is installed after implementation`). It belongs to optional installed-Hermes integration. Repository-local adapter compatibility remains required without an installation.
- `tests/test_e2e.py` is actual external inference/API and requires `AGENT_TEAM_RUN_LIVE_E2E=1`. Without selection, record every node as optional `not_selected`. With selection, prerequisite/model/API failures fail; a skip cannot hide them.

Actual model inference, installed Hermes integration/cutover, real Pi session activation, live s6 supervision/restart, and container/image execution are separately selected deployment lanes. Record supplied external versions/endpoints safely and exact assertions; deterministic fake inference does not assert semantic quality or real deployment availability. Do not install/invoke optional components merely to turn standalone green. A requested deployment claim requires its selected integration lanes to pass.

## Final evidence and promotion

Emit a versioned machine-readable acceptance report and readable summary outside runtime state/source. The implementation chooses the report storage format while preserving these required fields and a stable case registry:

- Full candidate SHA/tree, clean/dirty status and any explicitly tested diff fingerprint; historical baseline SHA plus red oracle evidence.
- OS/architecture, exact Python/uv/build backend/dependency versions, lock/export/wheel hashes; clone and wheel commands, module origins and locked dependency comparison.
- Sanitized exact commands, cwd, environment key names and nonsensitive fixture/path settings, start/end monotonic durations and exit codes. No production values, raw secrets/prompts/knowledge text, authorization headers or URL credentials/query/fragment.
- Every required/optional case ID and outcome, selected scope, expected versus observed diagnostic codes, collection/run/deselection totals, all skips/xfails/XPASS and any equivalent replacement mapping. Required excluded cases block green.
- Parsed readiness/failure/stopping/stopped events and safe HTTP request/response evidence; finite-state fixture expected/observed call counts; direct/delegated result, artifact digest and persisted/restarted session/report proof.
- Before/after manifests, logical history digests/raw-type comparison results, snapshot/archive integrity, actual fault-site coverage and restart outcomes; subordinate migration reasons and last durable phase.
- Signal/barrier/PID/process-group/deadline/exit/lease-reuse/confinement evidence, generation rejection and attempted durable-write results; controller cleanup, any scratch-only orphan, unsupported capability/platform and network-enforcement qualifications.
- Separately selected actual integration lanes and pass/fail/not-selected reasons, residual warnings, exact acceptance conclusion for each claimed platform.

The report records required cases with correctly expected doctor failures/unverified observations as passed tests, without changing doctor's own result. It cannot turn unexecuted tests, missing proof, mocked process behavior, or optional external success into required standalone green.

Before Hermes cutover or image automation, all mandatory cases must have executed and passed for the intended platform, with no uncovered required capability/fixture/fault site. Selected deployment integration lanes must also pass for the intended deployment claim. Publish the report tied to the exact build revision; retain the live service until those later gates and the separately authorized cutover are complete. This issue authorizes closing the planning decision after independent review and documentation checks; it does not authorize Hermes cutover or implement image automation.

## Grilling record

Two delegated agents completed three frontier rounds and explicitly confirmed shared understanding before documentation edits. The frontier was empty:

| Round / question | Agreed decision |
| --- | --- |
| 1 / Q1 scope | Planning-only specification; future executable acceptance remains mandatory. |
| 1 / Q2 provisioning | Genuine frozen-sync fresh clone plus separate same-revision, locked-dependency installed wheel proof. |
| 1 / Q3 isolation | Per-case external HOME/state/workspace/XDG/temp/cache; allowlisted child env, absent optional binaries, explicit manifests and network claim limits. |
| 1 / Q4 baseline | Immutable historical escape reproduced red by init/fresh loader, declared missing-interface deviation; candidate public CLI positive and conflict-negative oracle. |
| 2 / Q5 model | Strict actual-loopback catalog/completion FSM matching custom content/tool protocol, no permissive fallback, artifact/report/restart evidence. |
| 2 / Q6 process | Handshake-based real signals, bounded clocks, cleanup/fault/forced-child confinement and successor generation evidence. |
| 2 / Q7 outcomes | Required pass/fail/unverified differs from expected diagnostic exits; optional actual lanes selected explicitly, no silent skips. |
| 3 / Q8 doctor | Initial missing-lock2, busy2 despite readiness, stopped guarded canonical0; wheel repository metadata separate from import origin. |
| 3 / Q9 paths | Complete authority cross-products, idempotence/overwrite/faults and source/default-home pollution proof. |
| 3 / Q10 migration | Every #3 branch and actual irreversible fault site, independent raw SQLite/history oracles, doctor nonmutation and newer authority preserved. |
| 3 / Q11 report | Exact revision/toolchain/case/event/storage/process evidence, skip replacement inventory, platform qualification and future promotion blockers. |

Dependent clarifications resolved provisioning downloads versus runtime loopback, one catalog request per command observation, semantic markers when role IDs coincide, explicit inactive-curator smoke settings, safe refusal of unsupported uncontained child modes, and a required passing process oracle replacing a legacy non-fork skip. An independent reviewer checks this complete decision specification and publication changes after the session; any flags require another delegated grilling round before closure. Runtime tests remain the subsequent build effort.

The independent review raised two flags. A fresh pair of delegated agents completed correction rounds Q1–Q4 and explicitly confirmed shared understanding with an empty frontier before editing: Q1 fixed the artifact to the 16-byte UTF-8 sequence ending in LF and its exact SHA-256; Q2 specified inner/outer JSON serialization and the client's two decodes; Q3 required a separately manifested optional-deployment-asset absence fixture and extended registry coverage through #7; Q4 preserved doctor's required candidate Git/core-layout checks, dirty-fixture warning, unselected optional scopes, absence of asset fallback, and the distinction between current s6-only reconciliation refusal and future core YAML persistence. Independent review must clear both corrections before planning closure; they add no claim of executed runtime acceptance.

A subsequent factual review flag reopened one naming question. A fresh delegated pair confirmed that `SimpleChatModel` decodes the outer HTTP response and `ToolAwareWorkerAgent._parse_object` decodes its inner content string. Both agents explicitly confirmed shared understanding and an empty frontier before correcting the class name above; serialization and artifact-byte requirements are unchanged.
