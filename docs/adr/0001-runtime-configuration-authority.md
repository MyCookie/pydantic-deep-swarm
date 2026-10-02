# ADR 0001: Runtime configuration and path authority

Status: accepted in [issue #2](https://github.com/MyCookie/pydantic-deep-swarm/issues/2).

Configuration-file selection and runtime-path selection are separate operations.
Select a named file exactly, then resolve state and workspace independently by
CLI, YAML, environment, and defaults. An environment-selected canonical file
cannot redirect state silently. Generated configuration persists absolute paths;
mutable runtime paths remain outside the repository.

This prevents initialization in one state directory followed by startup in the
default home directory. Missing named files fail for read-only commands. Repeated
initialization preserves configuration and reports explicit path mismatches.
Whole-file replacement requires `init --overwrite-config`, a saved backup, and
atomic publication. Serve and doctor never rewrite configuration.

The complete precedence, null, expansion, and mismatch rules are in the
[configuration contract](../configuration-contract.md).
