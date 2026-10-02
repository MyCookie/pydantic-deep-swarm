"""Provision exact-revision clone/wheel installations and run standalone gates.

Run this controller from the authoritative sourced shell environment. Child
processes deliberately receive only fixture-owned paths and required OS keys.
Reports remain outside the checkout and runtime trees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

from acceptance_registry import verify_registries
from asset_access_audit import install_audit, read_audit


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(repository: Path) -> dict:
    """Exclude only declared installation/compile products and Git metadata."""
    entries = {}
    for path in sorted(repository.rglob("*")):
        relative = path.relative_to(repository)
        if relative.parts[0] in {".git", ".venv"}:
            continue
        if "__pycache__" in relative.parts and path.suffix == ".pyc":
            continue
        if path.is_symlink():
            entries[str(relative)] = {"type": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            entries[str(relative)] = {"type": "file", "mode": path.stat().st_mode & 0o777,
                                      "size": path.stat().st_size, "sha256": digest(path)}
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        parser.error("run the minimum-runtime controller with Python 3.11")
    candidate, reports = args.candidate.resolve(), args.report_dir.resolve()
    if not reports.is_dir() or reports == candidate or candidate in reports.parents:
        parser.error("report directory must exist outside candidate")
    uv = shutil.which("uv")
    git = shutil.which("git")
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    if not uv or not git:
        parser.error("git and uv are required provisioning utilities")
    revision = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=candidate, text=True).strip()
    dirty = subprocess.check_output([git, "status", "--porcelain"], cwd=candidate, text=True).strip()
    if dirty:
        parser.error("candidate must be committed and clean")
    summary = {"report_kind": "agent-team.standalone-acceptance", "schema_version": 1,
               "revision": revision, "tree": subprocess.check_output([git, "rev-parse", "HEAD^{tree}"], cwd=candidate, text=True).strip(),
               "dirty": False, "platform": {"os": platform.system(), "release": platform.release(),
                                             "kernel_build": platform.version(), "macos": platform.mac_ver()[0],
                                             "architecture": platform.machine(), "python": platform.python_version()},
               "provisioning_interpreter": interpreter,
               "outcome": "failed", "commands": [], "lanes": {}, "cleanup": False,
               "provisioning": {"initial_cache": "empty per-run uv cache", "registry_downloads": "permitted for frozen dependencies and declared build backend"},
               "actual_integrations": {name: {"outcome": "not_selected", "reason": reason} for name, reason in {
                   "external-inference": "Strict loopback fixture selected; no external inference requested.",
                   "installed-hermes": "Repository-local adapter suite selected; installed Hermes and cutover excluded.",
                   "live-pi": "Deterministic executable fixtures selected; live Pi activation excluded.",
                   "live-s6": "Deterministic status fixtures selected; live supervision/restart excluded.",
                   "container": "Container and image execution excluded."}.items()},
               "network_enforcement": "No universal OS network confinement claimed; runtime fixture HTTP is loopback and strictly validated."}
    try:
        with tempfile.TemporaryDirectory(prefix="agent-team-acceptance-") as temporary:
            root = Path(temporary).resolve()
            home = root / "controller-home"
            home.mkdir()
            env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
            env.update(HOME=str(home), NO_PROXY="*", no_proxy="*", UV_CACHE_DIR=str(root / "uv-cache"),
                       UV_PYTHON=interpreter, XDG_CONFIG_HOME=str(root / "xdg-config"),
                       XDG_DATA_HOME=str(root / "xdg-data"), XDG_CACHE_HOME=str(root / "xdg-cache"))

            def command(case, argv, cwd, timeout=180, expected=0):
                started = time.monotonic()
                try:
                    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    for stream in ("stdout", "stderr"):
                        content = getattr(error, stream) or ""
                        if isinstance(content, bytes):
                            content = content.decode("utf-8", errors="replace")
                        (reports / f"{case}.{stream}.log").write_text(content)
                    ended = time.monotonic()
                    summary["commands"].append({"id": case, "argv": argv, "cwd": str(cwd),
                                                "environment_keys": sorted(env), "exit": None,
                                                "outcome": "timeout", "deadline_seconds": timeout,
                                                "start_monotonic": started, "end_monotonic": ended,
                                                "duration_seconds": ended - started})
                    raise RuntimeError(f"{case} exceeded {timeout}s; see retained logs") from None
                (reports / f"{case}.stdout.log").write_text(result.stdout)
                (reports / f"{case}.stderr.log").write_text(result.stderr)
                summary["commands"].append({"id": case, "argv": argv, "cwd": str(cwd),
                                            "environment_keys": sorted(env), "exit": result.returncode,
                                            "deadline_seconds": timeout, "start_monotonic": started,
                                            "end_monotonic": time.monotonic(),
                                            "duration_seconds": time.monotonic() - started})
                if result.returncode != expected:
                    raise RuntimeError(f"{case} exit {result.returncode}; see retained logs")
                return result.stdout

            clone = root / "clone"
            command("clone-create", [git, "clone", "--quiet", "--no-hardlinks", str(candidate), str(clone)], root)
            command("clone-checkout", [git, "checkout", "--quiet", "--detach", revision], clone)
            original_source = source_manifest(clone)
            summary["source_manifest_before"] = original_source
            summary["source_manifest_exclusions"] = [".git metadata", ".venv installation", "__pycache__/*.pyc compiler/import products"]
            lock_hash = digest(clone / "uv.lock")
            command("clone-sync", [uv, "sync", "--frozen"], clone)
            assert digest(clone / "uv.lock") == lock_hash
            command("clone-dependencies", [uv, "pip", "check"], clone)
            command("clone-lock-check", [uv, "lock", "--check", "--offline"], clone)
            python = str(clone / ".venv/bin/python")
            command("clone-entrypoint-checkout", [str(clone / ".venv/bin/agent-team"), "--help"], clone)
            inspect = "import agent_team,importlib.metadata,json,sys; print(json.dumps({'origin':agent_team.__file__,'python':sys.version,'versions':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}))"
            clone_metadata = json.loads(command("clone-origins", [python, "-c", inspect], root))
            assert Path(clone_metadata["origin"]).is_relative_to(clone / "src")
            summary["clone_metadata"] = clone_metadata
            summary["uv_version"] = command("uv-version", [uv, "--version"], root).strip()
            summary["lock_sha256"] = lock_hash
            command("clone-compile", [python, "-m", "compileall", "-q", "src", "tests", "pi", "scripts"], clone)
            command("clone-pi-assets", [python, "pi/manifest_validator.py", "pi/manifest.json"], clone)
            for name in ("agent-team", "agent-team-init"):
                command(f"clone-s6-syntax-{name}", [shutil.which("sh"), "-n", f"s6-service/{name}/run"], clone)
            inventory_path = reports / "test-inventory.json"
            command("clone-tests", [python, "scripts/run_isolated_tests.py", "--inventory", str(inventory_path)], clone, timeout=600)
            summary["test_inventory"] = str(inventory_path)
            registry = verify_registries(clone, json.loads(inventory_path.read_text()))
            storage_registry = json.loads((clone / "tests/knowledge_cases.json").read_text())
            actual_fault_sites = json.loads(command("clone-storage-fault-sites", [python, "-c",
                "import json; from agent_team.memory.preparation import FAULT_SITES; print(json.dumps(FAULT_SITES))"], clone))
            assert set(storage_registry.get("fault_sites", [])) == set(actual_fault_sites), "unregistered migration fault site"
            summary["migration_fault_sites"] = actual_fault_sites
            summary["migration_fault_instances"] = storage_registry.get("fault_instance_count")
            (reports / "case-registry.json").write_text(json.dumps(registry, indent=2) + "\n")
            summary["case_registry"] = str(reports / "case-registry.json")
            if registry["outcome"] != "passed":
                raise RuntimeError("mandatory contract registry incomplete; see case-registry.json")
            for lane, binary, repo in (("clone", clone / ".venv/bin/agent-team", clone),):
                report = reports / f"{lane}-smoke.json"
                command(f"{lane}-smoke", [python, "scripts/standalone_smoke.py", "--binary", str(binary),
                                         "--repo-root", str(repo), "--report", str(report)], clone, timeout=180)
                summary["lanes"][lane] = json.loads(report.read_text())
            builds = root / "builds"
            command("wheel-build", [uv, "build", "--verbose", "--no-sources", "--out-dir", str(builds)], clone)
            build_log = (reports / "wheel-build.stderr.log").read_text()
            backend_versions = re.findall(r"uv[-_]build(?:==|[- ])(\d+\.\d+\.\d+)", build_log)
            if not backend_versions:
                backend_versions = re.findall(r"uv_build-(\d+\.\d+\.\d+)", str(list((root / "uv-cache").rglob("*uv_build*"))))
            assert backend_versions, "build backend version evidence unavailable"
            summary["build_backend"] = {"name": "uv_build", "versions": sorted(set(backend_versions))}
            wheels = list(builds.glob("*.whl"))
            assert len(wheels) == 1
            wheel = wheels[0]
            summary["wheel_sha256"] = digest(wheel)
            import zipfile
            with zipfile.ZipFile(wheel) as archive:
                summary["wheel_files"] = sorted(archive.namelist())
            exported = root / "runtime-requirements.txt"
            command("wheel-export", [uv, "export", "--frozen", "--no-dev", "--no-emit-project",
                                      "--format", "requirements.txt", "--output-file", str(exported)], clone)
            summary["export_sha256"] = digest(exported)
            shutil.copyfile(exported, reports / "runtime-requirements.txt")
            wheel_env = root / "wheel-venv"
            command("wheel-venv", [uv, "venv", "--python", interpreter, str(wheel_env)], root)
            wheel_python = str(wheel_env / "bin/python")
            command("wheel-sync", [uv, "pip", "sync", "--require-hashes", "--python", wheel_python, str(exported)], root)
            command("wheel-install", [uv, "pip", "install", "--no-deps", "--python", wheel_python, str(wheel)], root)
            command("wheel-dependencies", [uv, "pip", "check", "--python", wheel_python], root)
            wheel_metadata = json.loads(command("wheel-origins", [wheel_python, "-c", inspect], root))
            assert Path(wheel_metadata["origin"]).is_relative_to(wheel_env)
            assert "site-packages" in Path(wheel_metadata["origin"]).parts
            clone_versions = {key.lower().replace("_", "-"): value for key, value in clone_metadata["versions"].items()}
            assert all(clone_versions[key.lower().replace("_", "-")] == value for key, value in wheel_metadata["versions"].items())
            summary["wheel_metadata"] = wheel_metadata
            wheel_report = reports / "wheel-smoke.json"
            command("wheel-smoke", [wheel_python, str(clone / "scripts/standalone_smoke.py"), "--binary",
                                     str(wheel_env / "bin/agent-team"), "--repo-root", str(clone),
                                     "--report", str(wheel_report)], root, timeout=180)
            summary["lanes"]["wheel"] = json.loads(wheel_report.read_text())
            baseline = root / "historical-baseline"
            command("baseline-clone", [git, "clone", "--quiet", "--no-hardlinks", str(candidate), str(baseline)], root)
            command("baseline-checkout", [git, "checkout", "--quiet", "--detach", "b3c0d2263b8a606efc0caa849876d0fb1bd6ef00"], baseline)
            command("baseline-sync", [uv, "sync", "--frozen"], baseline)
            baseline_report = reports / "baseline-escape.json"
            command("baseline-red", [python, str(clone / "scripts/reproduce_path_escape.py"), str(baseline),
                                     "--report", str(baseline_report)], root)
            summary["baseline"] = json.loads(baseline_report.read_text())
            stripped = root / "asset-stripped"
            command("stripped-clone", [git, "clone", "--quiet", "--no-hardlinks", str(clone), str(stripped)], root)
            stripped_before = source_manifest(stripped)
            removed = []
            for relative in ("s6-service", "pi", "integrations/hermes"):
                target = stripped / relative
                removed.extend({"path": str(path.relative_to(stripped)), "sha256": digest(path)}
                               for path in target.rglob("*") if path.is_file())
                shutil.rmtree(target)
            summary["removed_assets"] = removed
            stripped_after = source_manifest(stripped)
            assert all(stripped_before[path] == value for path, value in stripped_after.items())
            assert set(stripped_before) - set(stripped_after) == {item["path"] for item in removed}
            summary["asset_stripped_manifest_before"] = stripped_before
            summary["asset_stripped_manifest_after"] = stripped_after
            summary["asset_stripped_revision"] = command("stripped-revision", [git, "rev-parse", "HEAD"], stripped).strip()
            assert summary["asset_stripped_revision"] == revision
            stripped_report = reports / "asset-stripped-smoke.json"
            audit_log = reports / "asset-access.jsonl"
            audit_source = install_audit(Path(wheel_metadata["origin"]).parent.parent, audit_log,
                [clone / "src", clone / "pi", clone / "s6-service", clone / "integrations/hermes",
                 stripped / "src", stripped / "pi", stripped / "s6-service", stripped / "integrations/hermes",
                 candidate / "src", candidate / "pi", candidate / "s6-service", candidate / "integrations/hermes"])
            summary["asset_audit_source_sha256"] = digest(audit_source)
            command("stripped-smoke", [wheel_python, str(clone / "scripts/standalone_smoke.py"), "--binary",
                                        str(wheel_env / "bin/agent-team"), "--repo-root", str(stripped),
                                        "--report", str(stripped_report)], root, timeout=180)
            summary["lanes"]["asset-stripped"] = json.loads(stripped_report.read_text())
            audit = read_audit(audit_log)
            (reports / "asset-access-summary.json").write_text(json.dumps(audit, indent=2) + "\n")
            summary["asset_access_audit"] = str(reports / "asset-access-summary.json")
            assert audit["denied_attempts"] == 0, "core attempted an optional asset or source fallback"
            assert source_manifest(stripped) == stripped_after, "runtime changed derived core source"
            final_source = source_manifest(clone)
            summary["source_manifest_after"] = final_source
            assert final_source == original_source, "undeclared checkout file mutation"
            assert not command("clone-final-clean", [git, "status", "--porcelain"], clone).strip()
            summary["outcome"] = "passed"
        summary["cleanup"] = True
    except Exception as error:
        summary["failure"] = str(error)
    (reports / "acceptance.json").write_text(json.dumps(summary, indent=2) + "\n")
    (reports / "summary.md").write_text(
        f"Acceptance {summary['outcome']} for `{revision}` on {platform.system()} {platform.machine()}, Python {platform.python_version()}.\n\n"
        + (f"Failure: {summary['failure']}\n\n" if summary.get("failure") else "")
        + f"Migration: {summary.get('migration_fault_instances', 'unverified')} fault occurrences across {len(summary.get('migration_fault_sites', []))} sites.\n\n"
        + "Installed lanes: " + ", ".join(f"{name}: {value['outcome']} ({len(value.get('cases', []))} cases)" for name, value in summary["lanes"].items()) + ".\n\n"
        + "External inference, installed Hermes, live Pi/s6 and containers were not selected. No deployment cutover was performed.\n\n"
        + summary["network_enforcement"] + "\n")
    print(json.dumps({"revision": revision, "outcome": summary["outcome"], "failure": summary.get("failure")}))
    return 0 if summary["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
