# Knowledge database migration contract

This is the accepted implementation plan for
[issue #3](https://github.com/MyCookie/pydantic-deep-swarm/issues/3), following
[ADR 0002](adr/0002-canonical-knowledge-database.md). It describes intended
behavior, not functionality already implemented in this checkout. The user
selected the canonical path, automatic migration, and empty/identical-peer
reconciliation; the remaining decisions were resolved through the delegated
asker/answerer interview and independent review.

## Authority and opening order

Apply the resolved configuration and path rules from
[issue #2](https://github.com/MyCookie/pydantic-deep-swarm/issues/2) before inspecting
knowledge state. The canonical database is `<state_dir>/knowledge/knowledge.db`;
the legacy database is `<state_dir>/knowledge.db`. There is no configurable
alternative database path in the core workflow.

`init` creates directories and configuration and leaves knowledge databases
untouched. Bootstrap and the optional s6 initializer likewise leave database
creation and migration to leased runtime startup. They must not invoke
`KnowledgeStore` on either candidate path during initialization or diagnostics.

Startup acquires the existing runtime lease, checks pending migration evidence,
prepares one authoritative canonical store, then shares that store with retention
and the engine. The app and engine must not independently resolve, migrate, or
create databases. Direct production engine construction must use the same leased
preparation boundary. The explicit low-level `KnowledgeStore(path)` API may remain
for isolated tests and tools; it does not acquire runtime ownership or perform
path migration.

If `memory.enabled` or `memory.shared_knowledge` is false, startup skips all
knowledge database access, migration, and knowledge retention. It reports
`disabled`; existing database problems do not prevent that disabled runtime from
starting. Enabling knowledge later requires successful preparation.

## Selection

The following table applies after pending migration recovery, safe-path checks,
and validation of every existing candidate. An empty or corrupt file is not
equivalent to an absent database.

Prior authority takes precedence over ordinary selection. If canonical is missing
and a completed migration or fresh-publication manifest records its successful
establishment, require `recovery_required` regardless of whether legacy is present.
Only a valid pending journal proving the exact intended continuation permits
automatic roll-forward. When canonical is missing, recognizable nonempty
migration/recovery artifacts with missing, corrupt, or unsupported manifests also
block selection, with reason
`evidence_unverified`; they do not prove successful completion. An empty backups
directory or lease file alone is not evidence of prior canonical authority.
If all historical evidence was removed, previous authority is unknowable; the
ordinary no-evidence rules apply.

| Existing state | Enabled startup behavior |
| --- | --- |
| Neither database nor associated sidecars, with no prior-authority/recovery evidence | Create the canonical database through staged publication. |
| Valid legacy database only | Preserve its full history and migrate automatically to canonical. |
| Valid canonical database only | Validate and use it directly; perform only supported schema upgrades if required. |
| Both valid; only legacy has records | Select legacy and archive both displaced originals before completion. |
| Both valid; only canonical has records | Keep canonical and archive legacy. |
| Both valid and empty, or identical full histories | Prefer canonical and archive legacy. |
| Both valid and populated with different histories | Fail as `ambiguous`; do not choose or merge. |
| Either candidate invalid, unsupported, unsafe, or unreadable | Fail with its specific cause; an empty or healthy peer does not override it. |
| Sidecar exists without its database | Fail as `orphan_sidecar`; do not treat the directory as fresh. |

After success, only the canonical database remains at a recognized active path.
Archives never participate in ordinary selection. A newly appearing legacy file
is new conflicting state, not an invitation to restore a completed migration.

## Validation and equality

An existing candidate must contain a recognized `knowledge_records` table. The
supported shapes are the current 14-column schema in
[`knowledge.py`](../src/agent_team/memory/knowledge.py) and its 13-column predecessor
without `source_artifacts`. Recognize structure, types, keys, defaults, and
constraints, not exact SQL text formatting. Known application indexes may be
missing and recreated on a staged copy; present known indexes must match their
recognized definitions. Allow only explicitly recognized SQLite-generated
primary-key metadata and optional ANALYZE statistics, not every `sqlite_*` object.
Unknown tables, columns, indexes,
triggers, or views prevent automatic handling. Unknown schema versions also fail.
An implementation must explicitly enumerate these supported shapes rather than
allowing arbitrary schemas through `CREATE TABLE IF NOT EXISTS`.

Require a full SQLite integrity check and a separate foreign-key check. Validate
the current nonmutating field contract, including unique non-null record IDs,
required values, parseable timestamps, and JSON fields of the expected types.
Do not add new confidence limits or supersession-cycle policies as part of this
migration. A supported database with zero rows is empty; zero-byte files and
unrecognized SQLite databases are invalid.

Compare every row, including superseded records, by stable ID and every persisted
field: topic, summary, details, source agent, project, tags, confidence, creation
and update timestamps, supersession pointer, source run, source artifact, and
source artifacts. Compare SQLite values and types exactly after the one supported
normalization: a missing `source_artifacts` column has the literal SQL default
`'[]'`. Do not infer that list from `source_artifact`, normalize timestamps or JSON
text, redact values, or use `KnowledgeRecord.from_dict`, `add`, or `update` for
comparison or copying. These APIs can transform historical fields. Conservative
inequality requires operator intervention even when visible findings look alike.

Candidate equality uses a deterministic digest or exact comparison of this full
logical record set. Physical file/bundle fingerprints separately prove that
sources have not changed during an operation; a physical hash is not logical
database equality. Counts alone prove neither.

All knowledge files and migration artifacts must remain under the resolved
external state directory. Reject symlinked descendants, nonregular database or
sidecar files, hard-linked candidate files or candidate aliases, and migration
paths that escape their owned directories. Validate before opening and recheck
identities before retirement/publication. Preserve the resolved root behavior of
the path-authority contract rather than imposing a new config precedence rule.

## Preservation and publication

One runtime owns the state directory. Operators must stop older runtimes and
direct database writers that do not participate in the lease before migration.
SQLite contention and changed source fingerprints cause bounded failure, but
neither a snapshot nor a runtime lease can guarantee cutover against an outsider
that ignores ownership and writes after its file is moved.

For each displaced candidate, create a verified, restore-ready SQLite snapshot
containing all committed data, including transactions still in its WAL. Use the
SQLite backup API on a coherent, quiescent source or a complete private bundle;
never copy only the main file and assume it contains the latest history.
Preserve pre-upgrade snapshots unchanged. A known schema upgrade changes only
the staged candidate and must preserve all existing record fields.
Every two-path reconciliation snapshots both inputs, including a canonical
winner that will remain in place. A complete hot-journal bundle is copied and
recovered privately before selection; inability to do that safely fails without
attempting recovery on the original source.

Store recovery material under
`<state_dir>/knowledge/backups/<migration-id>/`, with distinct `legacy-root/` and
`canonical-nested/` entries so the two `knowledge.db` names cannot collide.
Snapshots, retired source bundles, and a manifest identify their original paths,
schema shape, complete record count/digest, physical fingerprints, and creation
time. Record metadata without copying record payloads or secrets into logs.
Use restrictive owner permissions for new backup files and directories, with
no broader access than their sources.

Durably verify snapshots and a self-contained staged candidate before retiring
any source. Stage on the canonical destination filesystem. After stopping source
connections, retire displaced database files and their extant `-wal`, `-shm`,
and `-journal` companions into their archive entries. A bundle retirement is
several file operations, not one atomic rename; journal each operation. Do not
publish a new main file alongside old destination sidecars. Atomically publish
the closed, verified candidate only once the destination bundle is clear.

An unchanged current-schema canonical winner stays in place: archive only its
legacy peer. If the canonical winner needs a supported schema upgrade, snapshot
it and publish an upgraded staged copy through the same recovery protocol.
The retained-winner branch performs no publication rename or canonical bundle
retirement. It may retain its recorded WAL/SHM companions: the journal proves the
existing canonical bundle and full history remain unchanged while legacy is
retired. The requirement to clear destination sidecars applies to replacing or
creating canonical, not to this retained winner.
Fresh creation uses staged publication with an empty source list, so a crash
cannot leave an unexplained zero-byte database that is mistaken for fresh state.

Backups and retired originals remain until an operator explicitly removes them.
Ordinary knowledge retention never deletes migration archives or recovery
evidence. Successful migration preserves all historical records before any
explicitly configured knowledge retention runs; report migration and later
retention as separate operations.

## Interrupted migration

Maintain one versioned pending journal in the owned knowledge directory. It
records the migration ID, selected source, allowed paths, schema transformations,
verified snapshots and candidate, planned per-file moves, and phase:
`PREPARED → RETIRING → PUBLISHED → COMPLETED`. Each destructive step has durable
intent recorded before it runs and a durable result recorded afterward. Sync
data files, journal replacements, and relevant directories before advancing.
No engine access, knowledge retention, or serving begins until durable completion.
`PUBLISHED` means canonical authority has been established: either the verified
candidate was renamed into place, or the retained canonical winner was verified
against its recorded bundle and history. The journal records which branch applies.

Check pending evidence before interpreting candidate absence. On restart under
the lease, verify journal schema/version, path containment, artifact integrity,
source fingerprints, and completed/intended operations. Resolve a crash between
a file operation and its journal update only when the observed artifacts prove
that exact operation. Continue retirement/publication from valid recorded
material; do not recompute selection from a half-retired directory.

A crash after replacement publication but before its journal update may be recovered by
proving that canonical matches the recorded candidate and that destination
sidecars are absent. In the retained-winner branch, verify the recorded unchanged
canonical bundle, including any expected sidecars, instead of requiring a rename
or sidecar removal. Unexpected canonical contents, missing recovery artifacts,
multiple pending journals, unsupported journal versions, malformed evidence, or
unprovable partial moves fail as `recovery_required`. Preserve all evidence;
never overwrite an unexpected canonical file to make the journal fit.

At completion, retain the completed journal as the archive's audit manifest and
clear pending state durably. Its old candidate digest is historical evidence,
not an invariant on subsequent runtime writes. A completed manifest never
automatically restores a snapshot, even if canonical is later missing or corrupt.
Once knowledge or retention has changed canonical, rollback is an explicit
operator recovery decision that must account for those newer changes.
Missing or unreadable completed audit manifests produce an audit warning when
the live canonical store is valid; historical audit damage never changes live
authority or starts automatic restoration. When canonical is absent, the
prior-authority and unverified-evidence rules above require explicit recovery.

## Startup and doctor reporting

Knowledge preparation failures stop enabled startup before it serves requests;
the runtime releases its lease and returns a nonzero startup result. Include the
selected state directory, both candidate paths, phase if any, cause, archive or
pending-journal location, and actionable remediation. Do not silently fall back
to an empty database or disable knowledge.

Successful startup reports `created`, `canonical`, `migrated`, or `reconciled`,
plus whether pending recovery was completed and whether a schema upgrade occurred.
Migration success includes the migration ID, source selection, all-history count,
archive location, and validation outcome. Diagnostics must not dump record text.

`doctor` is nonmutating with respect to runtime state: no database creation,
source SQLite connection, source checkpoint, source-side sidecar creation,
schema upgrade, lease-file creation, or runtime-owner metadata change. It may
create private temporary inspection copies outside the state directory and
remove its own temporary material afterward.

For detailed inspection of a stopped runtime, guard the existing lease file
with a nonmutating lock operation, without using the ownership-writing
`RuntimeLease.acquire`. Under that guard and the quiesced-writer contract, copy
complete candidate bundles to private storage; SQLite recovery/validation runs
only on those copies. A hot rollback journal is recovered there, with the
original bundle preserved. A pending migration with split source bundles is
reported from its journal; do not guess at a complete source by mixing files.

An occupied lease yields `inspection_deferred` and metadata observations only.
Missing or unusable existing lock, unsupported safe locking, unreadable files,
or inability to obtain a coherent inspection copy yields `inspection_unavailable`,
not healthy or corrupt. Filesystem observations may still report that no known
bundle was observed, qualified by the unavailable SQL inspection. Never use
SQLite's immutable flag on a live source to bypass locking.

| Knowledge check result | Meaning and guidance |
| --- | --- |
| `canonical` | Safely inspected, supported canonical store; no migration needed. |
| `needs_initialization` | Neither known bundle exists in guarded state; enabled serve will create canonical. |
| `migration_needed` | Safe automatic legacy/empty/identical reconciliation is available; start enabled serve. |
| `recovery_pending` | Valid interrupted migration evidence; enabled serve must verify and resume. |
| `ambiguous` | Divergent populated histories; resolve offline. |
| `invalid`, `unsupported_schema`, `unsafe_path`, `orphan_sidecar` | Specific validation failure; preserve state and repair offline. |
| `recovery_required` | Missing prior canonical authority or recovery evidence cannot prove a safe continuation; preserve state and recover offline. |
| `inspection_deferred`, `inspection_unavailable` | Inspection has not proved health; include why and how to retry. |
| `disabled` | Knowledge is disabled; startup skipped it. Doctor may optionally inspect and report a separate warning. |

Actual migration I/O, permission, lock, changed-source, or deadline failures report
`migration_failed` with their specific reason and last durable phase. The knowledge
check classifies definite blockers as failures when enabled, disabled-store
findings as warnings, and deferred/unavailable checks as unverified. Issue #6
owns how these results combine with other doctor checks into global readiness
and process exit codes; this plan does not redefine that command's overall contract.

Use a 30-second total monotonic deadline per knowledge preparation or inspection
operation, including SQL, copy, backup, validation, contention, and retries. The
internal deadline is injectable for tests and composes with any shorter enclosing
startup/doctor deadline. Check it before every irreversible step, make long
operations interruptible, and preserve durable evidence on timeout. Do not
multiply a full timeout by each retry or wait indefinitely.

## Offline operator recovery

No new merge or force-selection CLI is required. Provide a documented offline
procedure: stop every runtime/direct writer, retain the whole state directory
including sidecars and journals, examine private restore-ready snapshots, and
choose the desired complete history explicitly. If both valid histories diverge,
archive the rejected bundle outside recognized active paths and install one
verified supported store at canonical; any merge or data repair is a separately
authored operator operation. Preserve both originals during that work.

For corrupt/unsupported state or orphan sidecars, recover a complete known-good
bundle or perform deliberate offline repair; never delete unknown sidecars as a
fresh-start shortcut. For interrupted migration, preserve the pending journal
and every archive/stage; the operator must account for all completed moves before
replacing canonical or retiring pending evidence. Re-run read-only inspection
when possible, then start enabled serve and verify its reported outcome. Archive
restoration must explicitly account for records written or pruned after migration.

## Implementation acceptance gate

Use isolated external state directories and deterministic fixtures; this planning
change does not claim these future implementation tests have run. Required proof:

- Exercise neither, legacy-only, canonical-only, both-empty, empty peer in each
  direction, identical populated histories, different IDs, and same IDs with any
  differing field. Include superseded records and full provenance.
- Preserve raw values and timestamps, including secret-shaped text, without
  redaction or reinsertion. Verify both supported schemas and the literal old
  `source_artifacts` default. Reject unknown schema objects and invalid data.
- Preserve committed WAL-only transactions and recover hot journals in private
  copies. Reject orphan sidecars, zero-byte files, malformed SQLite, missing
  references, unsafe paths, and aliasing; never invent fresh state from them.
- Inject failure/crash before and after every intent write, snapshot sync,
  individual retirement move, publication rename, phase update, and completion
  cleanup. Restart rolls forward only with proof; malformed or mismatched
  evidence leaves originals/recovery material intact and serves no requests.
- Cover disk-full, permission failure, lock contention, changed sources,
  deadline expiry, and incomplete archives. Retry is idempotent. Archive entries
  never collide and files do not become accessible to more users.
- Write new canonical knowledge after completion and verify later restart uses
  it. Completed manifests never restore old data; missing/corrupt canonical is
  never automatically restored from an archive. Missing canonical with completed
  authority evidence blocks both fresh creation and selection of a reappeared
  legacy store. When canonical is missing, nonempty recognizable archives with
  damaged manifests block with `evidence_unverified`; empty backup directories
  alone do not block fresh state. A healthy canonical store with historical audit
  damage remains usable with a warning.
- Verify app, engine, retention, and optional s6 consume the same canonical
  preparation contract; init/bootstrap/s6 init do not create competing stores.
  Disabled startup opens/creates/migrates nothing in knowledge state.
- Doctor checks before/after hashes, file inventories, permissions, and owner
  metadata demonstrate nonmutation. Cover active migration/runtime, missing
  lock, unavailable safe inspection, pending recovery, and private temp cleanup.
- Verify ordinary retention starts only after completion and never touches
  archives; a repeated startup without new conflicts does no migration again.
  A retained canonical winner preserves its inode and recorded WAL/SHM through
  reconciliation and restart recovery until normal store operation begins.

The later implementation report must identify the tested revision, fixture gate
results, fault-injection outcomes, and any platform/environment exclusions. Overall
standalone smoke evidence remains owned by issue #8. Hermes cutover, image
automation, model selection, and optional-supervisor redesign remain outside #3.

## Evidence behind this decision

Current runtime opens root-level stores in
[`app.py`](../src/agent_team/app.py) and [`engine.py`](../src/agent_team/engine.py),
while the [s6 initializer](../s6-service/agent-team-init/run) creates nested state.
The [architecture](architecture.md#durable-state) already includes `knowledge/`.
The current store constructor immediately initializes schema and normal model
conversion redacts values, so it cannot serve as a nonmutating inspection API.

SQLite's [backup documentation](https://www.sqlite.org/backup.html) supports coherent
snapshots, while its [WAL documentation](https://www.sqlite.org/wal.html#the_wal_file)
requires committed WAL state to accompany a moved/copied database. Read-only WAL
access can require sidecar creation; the private-copy doctor contract avoids that
mutation. [Integrity checks](https://www.sqlite.org/pragma.html#pragma_integrity_check)
do not replace foreign-key checks, and
[immutable URI mode](https://www.sqlite.org/uri.html#uriimmutable) disables change
detection, making it unsuitable for changing live sources.
