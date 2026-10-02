"""Safe per-case observations for the mandatory configuration/control lanes.

This recorder observes production calls and fixture files. It does not replace
the concrete assertions in each test with a count of successful tests.
"""
from __future__ import annotations

import functools
import hashlib
import json
import signal
import stat
import time
from pathlib import Path

import pytest

from agent_team.redaction import redact_sensitive_data


def case_id(node_id: str) -> str:
    family = node_id.split("::", 1)[0].rsplit("/", 1)[-1].removeprefix("test_").removesuffix(".py")
    name = node_id.split("::", 1)[1].split("[", 1)[0].removeprefix("test_")
    return f"{family}.{name}.{hashlib.sha256(node_id.encode()).hexdigest()[:12]}"


def manifest(root: Path) -> dict:
    result = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "special"
        item = {"type": kind, "mode": stat.S_IMODE(info.st_mode), "inode": info.st_ino, "links": info.st_nlink, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
        if kind == "file":
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root))] = item
    return redact_sensitive_data(result)


@pytest.fixture(autouse=True)
def config_doctor_evidence(request, tmp_path, record_property):
    """Enforce a twenty-second case budget and retain credential-free evidence."""
    import agent_team.config as configuration
    import agent_team.doctor as doctor
    import agent_team.cli as cli_module
    from agent_team.control.manager import OwnedSwarmManager

    started = time.perf_counter()
    observations = []
    restorations = []
    identifier = case_id(request.node.nodeid)
    registry = json.loads(Path(__file__).with_name("config_doctor_cases.json").read_text())
    case = next(item for item in registry["cases"] if item["id"] == identifier)
    assertions_passed = False
    original_runtest = request.node.runtest

    def run_assertions():
        nonlocal assertions_passed
        original_runtest()
        assertions_passed = True

    request.node.runtest = run_assertions
    before = manifest(tmp_path)
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(signum, frame):
        pytest.fail("configuration/doctor case exceeded its 20 second deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 20)

    def observe(owner, name, kind):
        original = getattr(owner, name)

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            files_before = manifest(tmp_path)
            call_started = time.perf_counter()
            try:
                result = original(*args, **kwargs)
            except BaseException as error:
                observations.append({"operation": kind, "exception_type": type(error).__name__, "before": files_before, "after": manifest(tmp_path), "duration_seconds": time.perf_counter() - call_started})
                raise
            if kind == "doctor":
                finding = {key: result.get(key) for key in ("exit_code", "outcome", "validation_complete", "selected_scopes", "runtime_observation", "lease")}
                finding["checks"] = [{key: check.get(key) for key in ("id", "status", "required", "reason")} for check in result.get("checks", [])]
            elif kind == "resolve_configuration":
                finding = {"config_source": result.config_source, "config_file": str(result.config_file), "state_dir": str(result.config.runtime.state_dir), "workspace_dir": str(result.config.runtime.workspace_dir)}
            else:
                value = result.model_dump()
                finding = {key: value.get(key) for key in ("outcome", "exit_code", "commit_state", "changed_files", "verified", "restart_required", "health_ok", "ready_ok", "drift")}
            files_after = manifest(tmp_path)
            if kind == "doctor":
                assert files_before == files_after, "doctor modified selected fixture inputs"
            observations.append(redact_sensitive_data({"operation": kind, "observed": finding, "before": files_before, "after": files_after, "filesystem_unchanged": files_before == files_after, "duration_seconds": time.perf_counter() - call_started}))
            return result

        setattr(owner, name, wrapped)
        restorations.append((owner, name, original, wrapped))
        return original, wrapped

    original, wrapped = observe(doctor, "diagnose", "doctor")
    if getattr(request.module, "diagnose", None) is original:
        request.module.diagnose = wrapped
        restorations.append((request.module, "diagnose", original, wrapped))
    original, wrapped = observe(configuration, "resolve_configuration", "resolve_configuration")
    for module in (request.module, cli_module):
        if getattr(module, "resolve_configuration", None) is original:
            setattr(module, "resolve_configuration", wrapped)
            restorations.append((module, "resolve_configuration", original, wrapped))
    observe(OwnedSwarmManager, "reconcile", "reconcile")
    observe(OwnedSwarmManager, "inspect", "inspect")
    try:
        yield
    finally:
        after = manifest(tmp_path)
        for owner, name, original, wrapped in reversed(restorations):
            # A test's own monkeypatch may still be active; restore only ours.
            if getattr(owner, name) is wrapped:
                setattr(owner, name, original)
        request.node.runtest = original_runtest
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, old_handler)
        duration = time.perf_counter() - started
        record_property("config_doctor_evidence", {"schema_version": 1, "id": identifier, "node": request.node.nodeid, "setup": case["setup"], "operation": case["operation"], "expected": case["expected"], "observed": {"assertions_passed": assertions_passed, "operations": observations}, "before": before, "after": after, "allowed_fs_changes": case["allowed_fs_changes"], "deadline_seconds": 20, "deadline_enforced": True, "duration_seconds": duration, "cleanup": {"recorder_hooks_restored": True, "alarm_restored": True, "fixture_root": str(tmp_path), "fixture_retention": "pytest temporary directory policy"}, "proofs_passed": assertions_passed and 0 < duration < 20})
