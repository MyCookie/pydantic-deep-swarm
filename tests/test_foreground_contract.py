"""Real foreground owners and HTTP barriers for the accepted lifecycle contract.

Faults are injected in the *owner process*, never by mocking its lease, listener,
signals, deadline clock, HTTP transport, or termination. Evidence is consumed by
the standalone acceptance report through pytest user properties.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import stat

import httpx
import pytest


@pytest.fixture(autouse=True)
def finite_case_deadline(request, tmp_path):
    """Enforce the complete controller/owner/successor case, including teardown."""
    request.node._lifecycle_started = time.monotonic()
    request.node._lifecycle_before = manifest(tmp_path)
    request.node._lifecycle_root = tmp_path
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise AssertionError("lifecycle case exceeded its 20-second outer deadline")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 20)
    try:
        yield
        assert time.monotonic() - request.node._lifecycle_started < 20
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


OWNER = r'''
import asyncio, json, os, sys, time
from pathlib import Path
mode = os.environ.get("FIXTURE_MODE", "normal")
def barrier(name):
    print(json.dumps({"event":"fixture_barrier","name":name,"pid":os.getpid()}), flush=True)
def wait(name):
    barrier(name)
    assert sys.stdin.readline().strip() == "release"
import agent_team.config as selection
if mode == "preflight_block":
    original = selection.resolve_configuration
    def resolve(*args, **kwargs):
        wait("preflight")
        return original(*args, **kwargs)
    selection.resolve_configuration = resolve
import agent_team.app as boundary
import agent_team.control.discovery as discovery
from agent_team.engine import AgentTeamEngine
if mode == "recovery_fault":
    def recover(self, **kwargs):
        barrier("recovery")
        assert kwargs.get("deadline") is not None
        raise OSError("fixture-sensitive-raw-error")
    boundary.SessionStore.recover_incomplete = recover
if mode == "retention_fault":
    def sweep(self, **kwargs):
        barrier("retention")
        assert kwargs.get("deadline") is not None and self.write_guard is not None
        raise OSError("fixture-sensitive-raw-error")
    boundary.DurableRetention.sweep = sweep
if mode in {"engine_fault", "resistant_initializer"}:
    original = AgentTeamEngine._initialize
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        barrier("engine")
        if mode == "engine_fault":
            raise OSError("fixture-sensitive-raw-error")
        count = 0
        while True:
            # Deliberately resistant initialization in the owner process, not a thread.
            count += 1
            (self.config.runtime.state_dir / "initializer-probe.bin").write_text(str(count))
            time.sleep(.05)
    AgentTeamEngine._initialize = initialize
if mode == "after_engine_fault":
    original_close = AgentTeamEngine.close
    async def close(self):
        assert boundary.runtime_lease.is_held
        Path(os.environ["FIXTURE_CLOSE_EVIDENCE"]).write_text("closed-under-lease")
        await original_close(self)
    AgentTeamEngine.close = close
    def logs(*args, **kwargs):
        barrier("after_engine")
        raise OSError("fixture-sensitive-raw-error")
    boundary.logger.configure_retention = logs
if mode in {"cleanup_fault", "resistant_shutdown"}:
    original_close = AgentTeamEngine.close
    async def close(self):
        barrier("shutdown")
        if mode == "cleanup_fault":
            raise OSError("fixture-sensitive-raw-error")
        while True:
            try:
                await asyncio.sleep(.05)
            except asyncio.CancelledError:
                pass
    AgentTeamEngine.close = close
if mode == "late_listener":
    import uvicorn
    original_startup = uvicorn.Server.startup
    async def startup(self, sockets=None):
        await original_startup(self, sockets=sockets)
        barrier("listener")
        # A genuinely late listener must never publish readiness or return success.
        time.sleep(float(os.environ["FIXTURE_STARTUP_TIMEOUT"]) + .1)
    uvicorn.Server.startup = startup
if mode == "unavailable_runtime":
    @boundary.app.post("/fixture/unavailable")
    async def unavailable():
        boundary.session_store = None
        return {"status":"fixture_unavailable"}
if mode == "drain_barrier":
    import uvicorn
    original_shutdown = uvicorn.Server.shutdown
    async def shutdown(self, sockets=None):
        barrier("draining")
        released = Path(os.environ["FIXTURE_CLOSE_EVIDENCE"]).with_name("drain-release")
        while not released.exists():
            await asyncio.sleep(.01)
        await original_shutdown(self, sockets=sockets)
    uvicorn.Server.shutdown = shutdown
from agent_team.foreground import run_serve
raise SystemExit(run_serve(host="127.0.0.1", port=int(os.environ["FIXTURE_PORT"]),
    config_file=os.environ["FIXTURE_CONFIG"],
    startup_timeout=float(os.environ.get("FIXTURE_STARTUP_TIMEOUT", "5")),
    shutdown_timeout=float(os.environ.get("FIXTURE_SHUTDOWN_TIMEOUT", "5"))))
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Owner:
    def __init__(self, config, root, *, mode="normal", port=None, startup=5, shutdown=5, program=OWNER, env_additions=None):
        root.mkdir(parents=True, exist_ok=True)
        self.port = port or free_port()
        self.started = time.monotonic()
        self.events = []
        self.lines = []
        self.pending = queue.Queue()
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "PYTHONPATH", "VIRTUAL_ENV"}}
        env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / "xdg"), TMPDIR=str(root),
                   NO_PROXY="*", no_proxy="*", PYTHONDONTWRITEBYTECODE="1",
                   LLM_API_KEY="fixture-inference-key", AGENT_TEAM_API_TOKEN="fixture-api-token",
                   FIXTURE_MODE=mode, FIXTURE_PORT=str(self.port), FIXTURE_CONFIG=str(config),
                   FIXTURE_STARTUP_TIMEOUT=str(startup), FIXTURE_SHUTDOWN_TIMEOUT=str(shutdown),
                   FIXTURE_CLOSE_EVIDENCE=str(root / "closer-proof"))
        env.update(env_additions or {})
        self.process = subprocess.Popen([sys.executable, "-u", "-c", program], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, env=env, cwd=root, start_new_session=True)
        self.pid, self.group = self.process.pid, os.getpgid(self.process.pid)
        self.threads = []
        for stream, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            thread = threading.Thread(target=self._read, args=(stream, name), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _read(self, stream, name):
        for line in stream:
            self.lines.append((name, line.strip()))
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and "event" in item:
                self.events.append({**item, "stream": name, "observed_at": time.monotonic()})
                self.pending.put(item)

    def wait(self, name, *, barrier=None, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                item = self.pending.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise AssertionError(f"owner exited {self.process.returncode} before {name}: {self.lines[-12:]}")
                continue
            if item["event"] == name and (barrier is None or item.get("name") == barrier):
                return item
        raise AssertionError(f"missing {name}/{barrier}; events={self.events}")

    def release(self):
        self.process.stdin.write("release\n")
        self.process.stdin.flush()

    def send(self, signum):
        at = time.monotonic()
        self.process.send_signal(signum)
        return {"signal": int(signum), "at": at}

    def finish(self, expected, timeout=15):
        actual = self.process.wait(timeout=timeout)
        for thread in self.threads:
            thread.join(timeout=2)
        assert actual == expected, self.lines[-15:]
        assert time.monotonic() - self.started < 20
        assert not any("fixture-sensitive-raw-error" in line for _, line in self.lines)
        return {"pid": self.pid, "process_group": self.group, "exit": actual, "expected_exit": expected,
                "duration": time.monotonic() - self.started, "events": self.events}

    def terminate_fixture(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=3)
        for thread in self.threads:
            thread.join(timeout=1)


@contextmanager
def owner(config, root, **kwargs):
    item = Owner(config, root, **kwargs)
    try:
        yield item
    finally:
        item.terminate_fixture()


class Catalog:
    def __init__(self):
        self.payload = {"data": [{"id": "fixture-model"}]}
        self.status = 200
        self.catalog_entered = threading.Event()
        self.catalog_release = threading.Event()
        self.catalog_release.set()
        self.worker_entered = threading.Event()
        self.worker_cancelled = threading.Event()
        self.requests = []


@contextmanager
def endpoint():
    state = Catalog()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            assert self.path == "/v1/models"
            assert "Authorization" not in self.headers
            state.requests.append({"method": "GET", "path": self.path, "authorization": False})
            state.catalog_entered.set()
            assert state.catalog_release.wait(12)
            self.respond(state.status, state.payload)

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers.get("Authorization") == "Bearer fixture-inference-key"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            system = payload["messages"][0]["content"]
            if "You are the Team Manager." in system:
                state.requests.append({"method": "POST", "role": "manager"})
                content = json.dumps({"tasks": [{"task_id": "barrier", "role": "software-engineer",
                                                  "objective": "Hold known worker completion", "tools": []}],
                                      "review_required": False})
                self.respond(200, {"choices": [{"message": {"content": content}}]})
            elif "EXECUTABLE TOOLS" in system:
                state.requests.append({"method": "POST", "role": "worker"})
                state.worker_entered.set()
                self.connection.settimeout(10)
                try:
                    assert self.connection.recv(1) == b""
                    state.worker_cancelled.set()
                except (ConnectionResetError, BrokenPipeError):
                    state.worker_cancelled.set()
            else:
                state.requests.append({"method": "POST", "role": "principal"})
                self.respond(503, {"error": "fixture outage"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        state.catalog_release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def configuration(tmp_path, base_url, *, knowledge=False, model="fixture-model"):
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    path = tmp_path / "config.yaml"
    import yaml
    path.write_text(yaml.safe_dump({"runtime": {"state_dir": str(state), "workspace_dir": str(workspace)},
                                    "models": {"default": {"model": model, "base_url": base_url}},
                                    "memory": {"enabled": knowledge, "shared_knowledge": knowledge, "curator_enabled": False}}))
    return path, state


def manifest(path):
    result = {}
    for item in path.rglob("*"):
        if item.name in {"runtime.lock", "runtime-owner.json"}:
            continue
        info = item.lstat()
        relative = str(item.relative_to(path))
        if stat.S_ISREG(info.st_mode):
            result[relative] = {"type": "file", "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                                "size": info.st_size, "mode": stat.S_IMODE(info.st_mode), "inode": info.st_ino}
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = {"type": "directory", "mode": stat.S_IMODE(info.st_mode), "inode": info.st_ino}
        elif stat.S_ISLNK(info.st_mode):
            result[relative] = {"type": "symlink", "mode": stat.S_IMODE(info.st_mode), "inode": info.st_ino}
        else:
            result[relative] = {"type": "special", "mode": stat.S_IMODE(info.st_mode), "inode": info.st_ino}
    return result


def api(owner):
    return httpx.Client(base_url=f"http://127.0.0.1:{owner.port}", timeout=6, trust_env=False,
                        headers={"Authorization": "Bearer fixture-api-token"})


def evidence(request, case_id, owners, **extra):
    duration = time.monotonic() - request.node._lifecycle_started
    assert duration < 20
    request.node.user_properties.append(("lifecycle_evidence", {
        "schema_version": 1, "id": case_id, "case_id": case_id, "owners": owners,
        "duration_seconds": duration, "deadline_seconds": 20, "outer_deadline": 20,
        "expected": {"exits": [item["expected_exit"] for item in owners]},
        "observed": {"exits": [item["exit"] for item in owners]},
        "before": request.node._lifecycle_before, "after": manifest(request.node._lifecycle_root),
        "cleanup": {"controller_reaped_owner_pids": [item["pid"] for item in owners]},
        "proofs_passed": True, **extra}))


def successor(config, root):
    with owner(config, root) as next_owner:
        next_owner.wait("serve_ready")
        with api(next_owner) as client:
            assert client.get("/ready").json()["ready"] is True
            assert client.post("/sessions", json={"client_key": "successor"}).status_code == 200
        next_owner.send(signal.SIGTERM)
        return next_owner.finish(143)


def test_contenders_and_independent_state(tmp_path, request):
    with endpoint() as (catalog, url):
        config, state = configuration(tmp_path, url)
        other = tmp_path / "independent"
        other.mkdir()
        independent, _ = configuration(other, url)
        with owner(config, tmp_path / "home1") as incumbent:
            incumbent.wait("serve_ready")
            before = manifest(state)
            metadata = (state / "runtime-owner.json").read_bytes()
            with owner(config, tmp_path / "home2") as contender:
                contender.wait("serve_failed")
                observed = contender.finish(1)
                assert observed["duration"] < 5
                failure = next(item for item in contender.events if item["event"] == "serve_failed")
                assert (failure["reason"], failure["phase"]) == ("runtime_in_use", "lease")
            assert manifest(state) == before
            assert (state / "runtime-owner.json").read_bytes() == metadata
            with owner(independent, other / "home") as peer:
                peer.wait("serve_ready")
                peer.send(signal.SIGTERM)
                peer_proof = peer.finish(143)
            incumbent.send(signal.SIGTERM)
            proof = incumbent.finish(143)
        reused = successor(config, tmp_path / "home3")
        evidence(request, "lifecycle.ownership.contenders-independent", [proof, observed, peer_proof, reused],
                 state_preserved=True, incumbent_metadata_preserved=True, lease_reused=True)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_signal_during_preflight(tmp_path, request, sig):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home", mode="preflight_block") as process:
            process.wait("fixture_barrier", barrier="preflight")
            sent = process.send(sig)
            process.wait("serve_stopping")
            process.release()
            proof = process.finish(130 if sig == signal.SIGINT else 143)
            assert not any(item["event"] == "serve_ready" for item in process.events)
            assert not state.exists()
        evidence(request, f"lifecycle.signal.preflight.{sig.name.lower()}", [proof], signal=sent, state_absent=True)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_signal_during_leased_catalog(tmp_path, request, sig):
    with endpoint() as (catalog, url):
        config, state = configuration(tmp_path, url)
        catalog.catalog_release.clear()
        with owner(config, tmp_path / "home") as process:
            assert catalog.catalog_entered.wait(8)
            assert (state / "runtime-owner.json").is_file()
            sent = process.send(sig)
            process.wait("serve_stopping")
            catalog.catalog_release.set()
            proof = process.finish(130 if sig == signal.SIGINT else 143)
            assert not any(item["event"] == "serve_ready" for item in process.events)
            assert manifest(state) == {}
        reused = successor(config, tmp_path / "successor-home")
        evidence(request, f"lifecycle.signal.startup.{sig.name.lower()}", [proof, reused], signal=sent,
                 catalog_requests=catalog.requests, lease_reused=True)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_signal_cancels_active_worker(tmp_path, request, sig):
    with endpoint() as (catalog, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home") as process:
            process.wait("serve_ready")
            with api(process) as client:
                session = client.post("/sessions", json={}).json()["session_id"]
            responses = []
            def delegate():
                try:
                    with api(process) as client:
                        responses.append(client.post(f"/sessions/{session}/delegations", json={"brief": {
                            "objective": "Hold known worker completion", "desired_output": "Evidence"}}).status_code)
                except httpx.HTTPError:
                    responses.append("connection_closed")
            thread = threading.Thread(target=delegate, daemon=True)
            thread.start()
            assert catalog.worker_entered.wait(8)
            before = manifest(state)
            sent = process.send(sig)
            process.wait("serve_stopping")
            proof = process.finish(130 if sig == signal.SIGINT else 143)
            assert catalog.worker_cancelled.wait(3)
            thread.join(timeout=3)
            assert not thread.is_alive()
            assert manifest(state) == before
        reused = successor(config, tmp_path / "successor-home")
        evidence(request, f"lifecycle.signal.active.{sig.name.lower()}", [proof, reused], signal=sent,
                 worker_request_cancelled=True, durable_mutation_quiesced=True, lease_reused=True,
                 requests=catalog.requests, handler_result=responses)


@pytest.mark.parametrize("mode,phase", [("recovery_fault", "recovery"), ("retention_fault", "retention"),
                                        ("engine_fault", "engine"), ("after_engine_fault", "engine")])
def test_startup_phase_faults(tmp_path, request, mode, phase):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home", mode=mode) as process:
            failed = process.wait("serve_failed")
            assert (failed["reason"], failed["phase"]) == ("runtime_initialization_failed", phase)
            proof = process.finish(1)
            assert not any(item["event"] == "serve_ready" for item in process.events)
            assert not (state / "runtime-owner.json").exists()
            if mode == "after_engine_fault":
                assert (tmp_path / "home" / "closer-proof").read_text() == "closed-under-lease"
        reused = successor(config, tmp_path / "successor-home")
        evidence(request, f"lifecycle.fault.{mode}", [proof, reused], lease_reused=True,
                 prepared_resources_closed=mode == "after_engine_fault")


@pytest.mark.parametrize("payload,status,model,reason", [
    ({"data": []}, 200, "auto", "model_not_found"),
    ({"data": [{"id": "a"}, {"id": "b"}]}, 200, "auto", "ambiguous_model"),
    ({"data": [{"id": "auto"}]}, 200, "auto", "invalid_model_response"),
    ({"data": [{"id": "fixture-model"}]}, 401, "fixture-model", "model_unavailable"),
    ({"data": [{"id": "fixture-model"}]}, 200, "missing", "model_not_found"),
    ({"unexpected": []}, 200, "auto", "invalid_model_response"),
    ({"data": [{"id": ""}]}, 200, "auto", "invalid_model_response"),
    ({"data": [{"id": 7}]}, 200, "auto", "invalid_model_response"),
    ({"data": [{"id": "fixture-model"}]}, 503, "fixture-model", "model_unavailable"),
], ids=["empty-auto", "ambiguous-auto", "reserved-id", "auth-denied", "missing-explicit",
        "malformed-object", "empty-id", "nonstring-id", "http-unavailable"])
def test_model_failures_before_state_recovery(tmp_path, request, payload, status, model, reason):
    with endpoint() as (catalog, url):
        catalog.payload, catalog.status = payload, status
        config, state = configuration(tmp_path, url, model=model)
        with owner(config, tmp_path / "home") as process:
            failed = process.wait("serve_failed")
            assert (failed["reason"], failed["phase"]) == (reason, "models")
            proof = process.finish(1)
            assert manifest(state) == {}
            assert not any(item["event"] == "serve_ready" for item in process.events)
        evidence(request, f"lifecycle.model.{request.node.callspec.id}", [proof],
                 no_recovery_mutation=True, requests=catalog.requests)


@pytest.mark.parametrize("payload,model", [
    ({"data": [{"id": "fixture-model"}, {"id": "fixture-model"}]}, "auto"),
    ({"data": [{"id": "other"}, {"id": "fixture-model"}]}, "fixture-model"),
], ids=["deduplicated-auto", "explicit-multiple"])
def test_accepted_catalogs_resolve_once_without_persistence(tmp_path, request, payload, model):
    with endpoint() as (catalog, url):
        catalog.payload = payload
        config, state = configuration(tmp_path, url, model=model)
        before = config.read_bytes()
        with owner(config, tmp_path / "home") as process:
            process.wait("serve_ready")
            with api(process) as client:
                models = client.get("/models").json()["models"]
                assert all(item["model"] == "fixture-model" for item in models.values())
            assert catalog.requests == [{"method": "GET", "path": "/v1/models", "authorization": False}]
            assert config.read_bytes() == before
            process.send(signal.SIGTERM)
            proof = process.finish(143)
        evidence(request, f"lifecycle.model.{request.node.callspec.id}", [proof],
                 catalog_requests=catalog.requests, configured_yaml_unchanged=True, no_inference_during_startup=True)


def test_knowledge_failure_preserves_evidence(tmp_path, request):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url, knowledge=True)
        state.mkdir()
        (state / "knowledge.db").write_bytes(b"corrupt fixture evidence")
        before = manifest(state)
        with owner(config, tmp_path / "home") as process:
            failed = process.wait("serve_failed")
            assert (failed["reason"], failed["phase"]) == ("runtime_initialization_failed", "knowledge")
            assert failed["knowledge_reason"] == "sqlite_validation_failed"
            assert failed["knowledge"]["status"] == "invalid"
            assert failed["knowledge"]["state_dir"] == str(state)
            assert failed["knowledge"]["verification"] == "unknown"
            assert len([event for event in process.events if event["event"] == "knowledge_preparation"]) == 1
            proof = process.finish(1)
            assert manifest(state) == before
            assert not any(item["event"] == "serve_ready" for item in process.events)
        evidence(request, "lifecycle.fault.knowledge", [proof], original_evidence_preserved=True)


@pytest.mark.parametrize("kind", ["missing-config", "invalid-url", "state-file", "invalid-port", "invalid-timeout"])
def test_invalid_preflight_is_nonmutating(tmp_path, request, kind):
    config, state = configuration(tmp_path, "ftp://invalid" if kind == "invalid-url" else "http://127.0.0.1:1/v1")
    if kind == "missing-config":
        config.unlink()
    if kind == "state-file":
        state.write_text("preserved state")
    kwargs = {"port": 65536} if kind == "invalid-port" else {"startup": float("nan")} if kind == "invalid-timeout" else {}
    with owner(config, tmp_path / "home", **kwargs) as process:
        failed = process.wait("serve_failed")
        assert failed["phase"] == "preflight"
        assert failed["reason"] == ("path_error" if kind == "state-file" else "config_error")
        proof = process.finish(1)
        assert not any(item["event"] == "serve_ready" for item in process.events)
        assert not state.exists() or state.is_file()
        assert not (tmp_path / "home" / ".agent-team").exists()
    evidence(request, f"lifecycle.preflight.{kind}", [proof], durable_state_unmodified=True)


def test_listener_collision(tmp_path, request):
    with endpoint() as (_, url), socket.socket() as busy:
        config, state = configuration(tmp_path, url)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        with owner(config, tmp_path / "home", port=busy.getsockname()[1]) as process:
            failed = process.wait("serve_failed")
            assert (failed["reason"], failed["phase"]) == ("listener_error", "listener")
            proof = process.finish(1)
            assert not state.exists()
        evidence(request, "lifecycle.fault.listener-bind", [proof], actual_bound_contender=True, state_absent=True)


@pytest.mark.parametrize("mode,phase", [("resistant_initializer", "engine"), ("late_listener", "listener")])
def test_total_startup_watchdog(tmp_path, request, mode, phase):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home", mode=mode, startup=5, shutdown=5) as process:
            process.wait("fixture_barrier", barrier=phase)
            failed = process.wait("serve_failed")
            assert (failed["reason"], failed["phase"]) == ("startup_timeout", phase)
            proof = process.finish(1, timeout=12)
            assert proof["duration"] < 12
            assert not any(item["event"] == "serve_ready" for item in process.events)
            resistant_writes = (state / "initializer-probe.bin").read_bytes() if mode == "resistant_initializer" else None
        reused = successor(config, tmp_path / "successor-home")
        if resistant_writes is not None:
            assert (state / "initializer-probe.bin").read_bytes() == resistant_writes
        evidence(request, f"lifecycle.deadline.{mode}", [proof, reused], startup_budget=5,
                 shutdown_budget=5, lease_reused=True, initializer_not_abandoned=True,
                 resistant_initializer_stopped_writing=mode == "resistant_initializer")


@pytest.mark.parametrize("mode,second", [("cleanup_fault", False), ("resistant_shutdown", False),
                                        ("resistant_shutdown", True)], ids=["cleanup-exception", "shutdown-deadline", "second-signal"])
def test_failed_shutdown_keeps_ownership_until_exit(tmp_path, request, mode, second):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home", mode=mode, shutdown=5) as process:
            process.wait("serve_ready")
            sent = [process.send(signal.SIGTERM)]
            process.wait("serve_stopping")
            process.wait("fixture_barrier", barrier="shutdown")
            if second:
                sent.append(process.send(signal.SIGINT))
            proof = process.finish(1, timeout=8)
            assert not any(item["event"] == "serve_stopped" for item in process.events)
            assert any(item["event"] == "serve_failed" and item["reason"] == "shutdown_failed" for item in process.events)
        reused = successor(config, tmp_path / "successor-home")
        evidence(request, f"lifecycle.shutdown.{request.node.callspec.id}", [proof, reused], signals=sent,
                 shutdown_budget=5, lease_reused=True, owner_os_termination=True)


def test_readiness_is_local_and_admission_never_initializes(tmp_path, request):
    with endpoint() as (catalog, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home") as process:
            process.wait("serve_ready")
            with api(process) as client:
                assert client.get("/health").json() == {"status": "ok"}
                assert client.get("/ready").json() == {"status": "ok", "ready": True}
                assert client.get("/ready", headers={"Authorization": "Bearer invalid"}).status_code == 401
                catalog.status = 503
                assert client.get("/ready").json()["ready"] is True
                session = client.post("/sessions", json={}).json()["session_id"]
                result = client.post(f"/sessions/{session}/messages", json={"message": "What is 2+2?"})
                assert result.status_code == 200
                assert result.json()["status"] != "complete"
                assert client.get("/ready").json()["ready"] is True
            process.send(signal.SIGTERM)
            proof = process.finish(143)
        evidence(request, "lifecycle.readiness.local-outage-auth", [proof], local_readiness_after_model_outage=True,
                 inference_error_exposed=True, authenticated_probes=True)


def test_http_does_not_initialize_missing_runtime(tmp_path, request):
    with endpoint() as (catalog, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home", mode="unavailable_runtime") as process:
            process.wait("serve_ready")
            with api(process) as client:
                assert client.post("/fixture/unavailable").status_code == 200
                before = manifest(state)
                ready = client.get("/ready")
                assert ready.status_code == 503 and ready.json()["reason"] == "engine_not_initialized"
                assert client.post("/sessions", json={}).status_code == 503
                assert client.get("/health").status_code == 200
                assert manifest(state) == before
                assert catalog.requests == [{"method": "GET", "path": "/v1/models", "authorization": False}]
            process.send(signal.SIGTERM)
            proof = process.finish(143)
        evidence(request, "lifecycle.admission.no-lazy-initialization", [proof],
                 http_requests_cannot_initialize=True, durable_state_preserved=True, catalog_probe_count=1)


def test_draining_rejects_work_before_listener_shutdown(tmp_path, request):
    with endpoint() as (_, url):
        config, state = configuration(tmp_path, url)
        with owner(config, tmp_path / "home", mode="drain_barrier") as process:
            process.wait("serve_ready")
            before = manifest(state)
            sent = process.send(signal.SIGTERM)
            process.wait("serve_stopping")
            process.wait("fixture_barrier", barrier="draining")
            with api(process) as client:
                assert client.get("/health").json() == {"status": "ok"}
                ready = client.get("/ready")
                assert ready.status_code == 503 and ready.json()["reason"] == "runtime_stopping"
                assert client.post("/sessions", json={}).status_code == 503
                assert manifest(state) == before
            (tmp_path / "home" / "drain-release").write_text("release")
            proof = process.finish(143)
        reused = successor(config, tmp_path / "successor-home")
        evidence(request, "lifecycle.admission.draining", [proof, reused], signal=sent,
                 listener_bound_while_draining=True, new_work_rejected=True, lease_reused=True)
