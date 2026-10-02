"""Installed CLI, real HTTP, durable restart, and diagnostic nonmutation gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
import tempfile

import httpx
import yaml

from standalone_fixture import ARTIFACT_BYTES, ARTIFACT_SHA256, BRIEF, model_server


def manifest(root: Path) -> dict:
    result = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            stat = path.lstat()
            result[str(path.relative_to(root))] = {
                "mode": stat.st_mode, "inode": stat.st_ino, "size": stat.st_size,
                "hash": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            }
    return result


class RuntimeProcess:
    def __init__(self, command, cwd, env):
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.events = queue.Queue()
        self.started = time.monotonic()
        self.process = subprocess.Popen(command, cwd=cwd, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.readers = []
        for stream, collected in ((self.process.stdout, self.stdout), (self.process.stderr, self.stderr)):
            def read(pipe=stream, target=collected):
                for line in pipe:
                    target.append(line.rstrip("\n"))
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and item.get("event"):
                            self.events.put(item)
                    except json.JSONDecodeError:
                        pass
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            self.readers.append(thread)

    def ready(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=0.1)
            except queue.Empty:
                assert self.process.poll() is None, "runtime exited before ready: " + "\n".join(self.stderr)
                continue
            if event["event"] == "serve_failed":
                raise AssertionError("runtime startup failed: " + json.dumps(event))
            if event["event"] == "serve_ready":
                return event
        raise AssertionError("runtime readiness event deadline")

    def stop(self, signum=signal.SIGTERM):
        self.process.send_signal(signum)
        code = self.process.wait(timeout=15)
        for reader in self.readers:
            reader.join(timeout=2)
        assert code == 128 + signum, f"signal exit {code}, expected {128 + signum}: {self.stderr}"
        events = [json.loads(line) for line in self.stderr if line.startswith("{")]
        names = [item.get("event") for item in events]
        assert "serve_stopping" in names and "serve_stopped" in names
        assert names.index("serve_stopping") < names.index("serve_stopped")
        assert sum('"event": "serve_ready"' in line or '"event":"serve_ready"' in line for line in self.stdout) == 1
        return {"pid": self.process.pid, "exit": code, "events": events,
                "duration_seconds": time.monotonic() - self.started}

    def cleanup(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)


def run_gate(binary: Path, repository: Path, root: Path, evidence: dict | None = None) -> dict:
    home, state, workspace, neutral = (root / name for name in ("home", "state", "workspace", "neutral"))
    home.mkdir()
    neutral.mkdir()
    binaries = root / "bin"
    binaries.mkdir()
    for executable in ("git", "uv", "python3", "sh", "env"):
        found = shutil.which(executable)
        if found is not None:
            (binaries / executable).symlink_to(found)
    for optional in ("pi", "s6-svstat", "s6-svc", "hermes", "docker"):
        assert shutil.which(optional, path=str(binaries)) is None
    for name in (".agent-team", ".hermes"):
        (home / name).mkdir()
        (home / name / "sentinel").write_bytes(b"preserve fixture home\n")
    home_before = manifest(home)
    evidence = evidence if evidence is not None else {}
    evidence.update(cases=[], runtime=[], cleanup=False)
    processes = []

    def passed(case, **details):
        evidence["cases"].append({"id": case, "outcome": "passed", **details})

    with model_server() as (fixture, model_url):
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
        env.update(HOME=str(home), AGENT_TEAM_STATE_DIR=str(state), AGENT_TEAM_WORKSPACE_DIR=str(workspace),
                   LLM_BASE_URL=model_url, LLM_MODEL="auto", LLM_API_KEY="fixture-inference-key",
                   AGENT_TEAM_API_TOKEN="fixture-runtime-key", NO_PROXY="*", no_proxy="*",
                   XDG_CONFIG_HOME=str(root / "xdg-config"), XDG_DATA_HOME=str(root / "xdg-data"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"))
        env["PATH"] = str(binaries)

        def cli(*arguments, expected=0):
            operation = subprocess.run([str(binary), *arguments], cwd=neutral, env=env,
                                       capture_output=True, text=True, timeout=20)
            assert operation.returncode == expected, f"{arguments}: exit {operation.returncode}: {operation.stdout} {operation.stderr}"
            assert "fixture-inference-key" not in operation.stdout + operation.stderr
            assert "fixture-runtime-key" not in operation.stdout + operation.stderr
            return operation

        def doctor(expected):
            selected_state = Path(env["AGENT_TEAM_STATE_DIR"])
            selected_workspace = Path(env["AGENT_TEAM_WORKSPACE_DIR"])
            before = (manifest(selected_state), manifest(selected_workspace), manifest(home))
            count = fixture.calls["catalog"]
            output = cli("doctor", "--repo-root", str(repository), "--json", "--timeout", "5", expected=expected)
            report = json.loads(output.stdout)
            assert report["report_kind"] == "agent-team.doctor" and report["schema_version"] == 1
            assert report["exit_code"] == expected
            assert report["runtime_observation"]["selected_runtime_correlation"] == "unknown"
            after = (manifest(selected_state), manifest(selected_workspace), manifest(home))
            assert before == after, "doctor mutated runtime files"
            assert fixture.calls["catalog"] - count == 1, "doctor repeated shared endpoint discovery"
            evidence.setdefault("doctor_filesystem_proofs", []).append({
                "expected_exit": expected,
                "before": {"state": before[0], "workspace": before[1], "home": before[2]},
                "after": {"state": after[0], "workspace": after[1], "home": after[2]},
                "after_equals_before": True,
                "digest": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
            })
            return report

        try:
            cli("init")
            config_file = state / "config/config.yaml"
            config_bytes = config_file.read_bytes()
            config = yaml.safe_load(config_bytes)
            assert Path(config["runtime"]["state_dir"]) == state
            assert Path(config["runtime"]["workspace_dir"]) == workspace
            cli("init")
            assert config_file.read_bytes() == config_bytes
            assert not (state / "knowledge/knowledge.db").exists() and not (state / "knowledge.db").exists()
            passed("installed.init.paths-idempotence-no-store")
            config.setdefault("memory", {}).update(enabled=True, shared_knowledge=True, curator_enabled=False)
            config_file.write_text(yaml.safe_dump(config, sort_keys=False))
            before_serve_yaml = config_file.read_bytes()
            fresh = doctor(2)
            assert any(check["reason"] == "inspection_unavailable" for check in fresh["checks"])
            passed("installed.doctor.fresh-no-lock", report=fresh)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.close()
            api = f"http://127.0.0.1:{port}"
            env["AGENT_TEAM_API_URL"] = api
            env["AGENT_TEAM_PORT"] = str(port)
            command = [str(binary), "serve", "--host", "127.0.0.1", "--port", str(port),
                       "--startup-timeout", "5", "--shutdown-timeout", "5"]
            count = fixture.calls["catalog"]
            owner = RuntimeProcess(command, neutral, env)
            processes.append(owner)
            ready = owner.ready()
            assert ready["pid"] == owner.process.pid and ready["port"] == port
            assert Path(ready["state_dir"]) == state
            assert fixture.calls["catalog"] - count == 1 and fixture.calls["direct"] == fixture.calls["manager"] == fixture.calls["worker"] == 0
            assert config_file.read_bytes() == before_serve_yaml
            with httpx.Client(base_url=api, headers={"Authorization": "Bearer fixture-runtime-key"},
                              timeout=10, trust_env=False) as client:
                assert client.get("/health").json() == {"status": "ok"}
                assert client.get("/ready").json() == {"status": "ok", "ready": True}
                assert httpx.get(api + "/health", trust_env=False).status_code == 401
                passed("installed.serve.listener-health-readiness-auth", event=ready)
                count = fixture.calls["catalog"]
                detected = cli("swarm", "detect")
                assert detected.stdout.strip() == "fixture-model"
                assert fixture.calls["catalog"] - count == 1
                count = fixture.calls["catalog"]
                status = json.loads(cli("swarm", "status", "--json").stdout)
                assert status["supervisor_state"] == "not_configured"
                assert status["source_model"] is None and status["live_model"] is None and not status["drift"]
                assert fixture.calls["catalog"] - count == 1
                passed("installed.core.detect-status-no-supervisor")
                session = client.post("/sessions", json={"client_key": "direct"}).json()["session_id"]
                direct = client.post(f"/sessions/{session}/messages", json={"message": "What is 2+2?"})
                assert direct.status_code == 200, direct.text
                assert direct.json()["action"] == "answer" and direct.json()["response"] == "The answer is 4."
                assert fixture.calls["direct"] == 1 and fixture.calls["manager"] == fixture.calls["worker"] == 0
                passed("installed.http.direct-no-worker")
                delegated_session = client.post("/sessions", json={"client_key": "delegated"}).json()["session_id"]
                delegated = client.post(f"/sessions/{delegated_session}/delegations", json={"brief": BRIEF})
                assert delegated.status_code == 200, delegated.text
                result = delegated.json()
                assert result["status"] == "complete", json.dumps(result)
                report = result["report"]
                assert report["requirement_results"][0]["requirement_id"] == "artifact"
                assert report["acceptance_results"][0]["criterion_id"] == "exact-bytes"
                assert report["acceptance_results"][0]["status"] == "passed"
                artifact = report["artifacts"][0]
                assert artifact["verified"] is True and artifact["sha256"] == ARTIFACT_SHA256 and artifact["size_bytes"] == 16
                files = list(workspace.rglob("acceptance.txt"))
                assert len(files) == 1 and files[0].read_bytes() == ARTIFACT_BYTES
                assert fixture.calls["manager"] == 1 and fixture.calls["worker"] == 3
                assert "TOOL RESULTS" not in result["response"]
                stored = client.get(f"/sessions/{delegated_session}/report").json()
                passed("installed.http.delegated-tools-artifact-report", artifact_sha256=ARTIFACT_SHA256,
                       artifact_size=16, protocol_calls=dict(fixture.calls))
                busy = doctor(2)
                assert any(check["reason"] == "inspection_deferred" for check in busy["checks"])
                assert busy["runtime_observation"]["endpoint_ready"] is True
                passed("installed.doctor.busy-nonmutating", report=busy)
            evidence["runtime"].append(owner.stop())
            passed("installed.signal.sigterm-clean-exit")
            completions = fixture.calls["direct"] + fixture.calls["manager"] + fixture.calls["worker"] + fixture.calls["summary"]
            successor = RuntimeProcess(command, neutral, env)
            processes.append(successor)
            successor.ready()
            with httpx.Client(base_url=api, headers={"Authorization": "Bearer fixture-runtime-key"},
                              timeout=10, trust_env=False) as client:
                assert client.get(f"/sessions/{delegated_session}").json()["status"] == "complete"
                assert client.get(f"/sessions/{delegated_session}/report").json() == stored
                assert completions == fixture.calls["direct"] + fixture.calls["manager"] + fixture.calls["worker"] + fixture.calls["summary"]
                assert files[0].read_bytes() == ARTIFACT_BYTES
                response = client.post(f"/sessions/{session}/messages", json={"message": "What is 2+2?"})
                assert response.status_code == 200 and response.json()["response"] == "The answer is 4."
            evidence["runtime"].append(successor.stop(signal.SIGINT))
            passed("installed.restart.persisted-report-no-replay-new-work")
            stopped = doctor(0)
            assert any(check["reason"] == "canonical" for check in stopped["checks"])
            passed("installed.doctor.stopped-canonical", report=stopped)
            before_plan = (manifest(state), manifest(workspace), manifest(home))
            plan = json.loads(cli("swarm", "reconcile", "--dry-run", "--json").stdout)
            assert plan["verified"] is False
            assert before_plan == (manifest(state), manifest(workspace), manifest(home))
            passed("installed.core.reconcile-dry-run-no-mutation")
            committed = json.loads(cli("swarm", "reconcile", "--no-restart", "--no-verify", "--json").stdout)
            assert committed["verified"] is False and committed["restarted"] is False
            assert committed["pending_restart"] is True
            persisted = yaml.safe_load(config_file.read_text())
            assert all(persisted["models"][role]["model"] == "fixture-model"
                       for role in ("principal", "manager", "worker", "curator"))
            passed("installed.core.reconcile-owned-yaml-pending-activation")
            disabled_state, disabled_workspace = root / "disabled-state", root / "disabled-workspace"
            env["AGENT_TEAM_STATE_DIR"] = str(disabled_state)
            env["AGENT_TEAM_WORKSPACE_DIR"] = str(disabled_workspace)
            cli("init")
            disabled_file = disabled_state / "config/config.yaml"
            disabled_config = yaml.safe_load(disabled_file.read_text())
            disabled_config["memory"] = {"enabled": False, "shared_knowledge": False, "curator_enabled": False}
            disabled_file.write_text(yaml.safe_dump(disabled_config, sort_keys=False))
            disabled_stopped = doctor(0)
            assert any(check["reason"] == "disabled" for check in disabled_stopped["checks"])
            assert not (disabled_state / "runtime.lock").exists()
            passed("installed.doctor.disabled-stopped", report=disabled_stopped)
            disabled_owner = RuntimeProcess(command, neutral, env)
            processes.append(disabled_owner)
            disabled_owner.ready()
            disabled_running = doctor(0)
            assert disabled_running["runtime_observation"]["lease_status"] == "occupied"
            assert any(check["reason"] == "disabled" for check in disabled_running["checks"])
            assert not (disabled_state / "knowledge.db").exists() and not (disabled_state / "knowledge/knowledge.db").exists()
            passed("installed.doctor.disabled-running-no-store", report=disabled_running)
            evidence["runtime"].append(disabled_owner.stop())
            assert fixture.errors == [], fixture.errors
            assert {role: fixture.calls[role] for role in ("direct", "manager", "worker", "summary")} == {
                "direct": 2, "manager": 1, "worker": 3, "summary": 1,
            }, "incomplete or extra inference traffic"
            evidence["model_protocol"] = {"fixture_version": 1, "expected_completions": {"direct": 2, "manager": 1, "worker": 3, "summary": 1},
                                          "observed_calls": dict(fixture.calls), "events": fixture.events,
                                          "unknown_requests": [], "finite_state_complete": True}
            assert manifest(home) == home_before
            passed("installed.isolation.default-home-preserved")
        finally:
            for process in processes:
                process.cleanup()
            evidence["cleanup"] = all(process.process.poll() is not None for process in processes)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"report_kind": "agent-team.installed-smoke", "schema_version": 1,
              "outcome": "failed", "binary": str(args.binary.resolve())}
    try:
        with tempfile.TemporaryDirectory(prefix="agent-team-smoke-") as directory:
            run_gate(args.binary.resolve(), args.repo_root.resolve(), Path(directory).resolve(), report)
            report["outcome"] = "passed"
    except Exception as error:
        report["failure"] = str(error)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"outcome": report["outcome"], "cases": len(report.get("cases", [])),
                      "failure": report.get("failure")}))
    return 0 if report["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
