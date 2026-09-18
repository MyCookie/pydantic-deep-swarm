# First-party Pi assets

This directory is the Git-managed desired state for Pi resources owned by the
Agent Team project. It is a project-local Pi package, not Pi runtime state.

Tracked asset classes:

- `extensions/` — first-party TypeScript extensions.
- `skills/` — reusable Pi skills with frontmatter.
- `prompts/` — prompt templates loaded explicitly by the operator.
- `themes/` — declarative theme resources.
- `agents/` — optional role/agent instructions; not enabled implicitly.
- `package.json` — Pi package metadata and resource discovery.
- `manifest.json` — managed asset inventory.
- `tests/` — structural and manifest checks.

The reconciler planned in the GitOps workflow must install only resources listed
by `manifest.json`, into the selected Pi scope. Pi sessions, keys, caches,
`node_modules`, and other runtime state remain outside this tree.

No asset in this package implicitly grants Hermes tools, credentials, network
access, or worker capabilities.

## Exact-revision reconciliation

The Agent Team reconciler accepts only a full Git commit ID. It archives and
validates that revision's `pi/` package, stores it at a stable owned release
path, and invokes the preinstalled Pi binary by its absolute path:

```text
agent-team pi reconcile <40-or-64-hex-commit> \
  --repo-root /path/to/agent-team \
  --pi-binary /absolute/path/to/pi \
  --scope project-local \
  --approve
agent-team pi status --repo-root /path/to/agent-team \
  --pi-binary /absolute/path/to/pi --scope project-local
agent-team pi rollback --repo-root /path/to/agent-team \
  --pi-binary /absolute/path/to/pi --scope project-local --approve
```

Project-local state is owned below `.pi/agent-team/`; user-global state is
owned below `~/.pi/agent/agent-team/`. Only the owned package entry in Pi
settings is changed. Existing settings fields, unmanaged package entries, and
unmanaged files are preserved. The reconciler records a previous known-good
revision and reports package/settings drift. It never installs Pi, fetches
external packages, changes Git refs, or deletes unowned resources.

`--approve` makes project trust explicit when Pi requires it. A missing or
non-executable binary is a hard failure; the reconciler does not search for a
replacement or download one.

## License

Everything under `pi/` is distributed as a separately scoped package under the
[MIT License](LICENSE), matching the `MIT` identifier in `package.json`. The
Agent Team runtime outside this directory is licensed separately under GNU
AGPLv3.
