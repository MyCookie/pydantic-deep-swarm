"""One foreground listener, leased lifespan, and bounded signal teardown."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from urllib.parse import urlsplit


def event(name, *, stdout=False, **fields):
    from .redaction import redact_sensitive_data
    print(json.dumps(redact_sensitive_data({"event": name, **fields}), sort_keys=True), file=sys.stdout if stdout else sys.stderr, flush=True)


def _failure(reason, phase, knowledge_reason=None, *, knowledge=None):
    fields = {"reason": reason, "phase": phase, "detail": "Runtime could not complete this phase; inspect configuration and preserved state."}
    if knowledge_reason is not None:
        fields["knowledge_reason"] = knowledge_reason
    if knowledge is not None:
        fields["knowledge"] = knowledge
        if fields.get("knowledge_reason") is None and knowledge.get("reason") is not None:
            fields["knowledge_reason"] = knowledge["reason"]
    event("serve_failed", **fields)


def run_serve(*, host=None, port=None, config_file=None, state_dir=None, workspace_dir=None, startup_timeout=None, shutdown_timeout=None):
    """Return the foreground CLI exit code. Watchdogs never abandon mutating threads."""
    began = time.monotonic()
    boundary = server = listener = None
    startup_timer = teardown_timer = None
    stopping_signal = None
    failure = None
    failure_emitted = False
    ready_published = False
    phase = "preflight"
    shutdown_budget = 30.0
    owner_loop = None
    guard = threading.RLock()

    def current_phase():
        return boundary._phase if boundary is not None else phase

    def publish_failure():
        nonlocal failure_emitted
        if failure is not None and not failure_emitted:
            _failure(*failure, knowledge=boundary._knowledge_result if boundary is not None else None)
            failure_emitted = True

    def force_exit():
        # OS teardown releases ownership; no initializer is left running without it.
        with guard:
            if boundary is not None:
                boundary._knowledge_interrupted("deadline_exceeded" if failure is not None and failure[0] == "startup_timeout" else "owner_terminated")
            publish_failure()
            if failure is None:
                _failure("shutdown_failed", "shutdown", knowledge=boundary._knowledge_result if boundary is not None else None)
            os._exit(1)

    def begin_teardown():
        nonlocal teardown_timer
        if boundary is not None:
            boundary.mark_stopping()
        if server is not None:
            server.should_exit = True
        if owner_loop is not None and boundary is not None:
            owner_loop.call_soon_threadsafe(boundary.cancel_active_work)
        if teardown_timer is None:
            teardown_timer = threading.Timer(shutdown_budget, force_exit)
            teardown_timer.daemon = True
            teardown_timer.start()

    def expire_startup():
        nonlocal failure
        with guard:
            if ready_published or stopping_signal is not None:
                return
            failure = ("startup_timeout", current_phase(), None)
            # Preparation's failure result owns its durable phase/archive evidence.
            # Let it unwind under the lease; forced teardown emits qualified unknowns.
            if current_phase() != "knowledge":
                publish_failure()
            begin_teardown()

    def stop(signum, frame):
        nonlocal stopping_signal
        with guard:
            if stopping_signal is not None:
                force_exit()
            stopping_signal = signum
            event("serve_stopping", pid=os.getpid(), signal=signum)
            begin_teardown()

    # Installed before configuration and application imports, including blocking preflight.
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        host = host if host is not None else os.getenv("AGENT_TEAM_HOST", "localhost")
        port = int(port if port is not None else os.getenv("AGENT_TEAM_PORT", "8080"))
        startup_budget = float(startup_timeout if startup_timeout is not None else os.getenv("AGENT_TEAM_STARTUP_TIMEOUT", "60"))
        shutdown_budget = float(shutdown_timeout if shutdown_timeout is not None else os.getenv("AGENT_TEAM_SHUTDOWN_TIMEOUT", "30"))
        if not host or not 1 <= port <= 65535 or any(not math.isfinite(value) or value <= 0 for value in (startup_budget, shutdown_budget)):
            raise ValueError("invalid listener or deadline")
        deadline = began + startup_budget
        startup_timer = threading.Timer(max(0, deadline - time.monotonic()), expire_startup)
        startup_timer.daemon = True
        startup_timer.start()
        from .config import resolve_configuration
        frozen = resolve_configuration(config_file, state_dir=state_dir, workspace_dir=workspace_dir).config
        for path in (frozen.runtime.state_dir, frozen.runtime.workspace_dir):
            if path.exists() and not path.is_dir():
                from .runtime_boundary import RuntimeBoundaryError
                raise RuntimeBoundaryError("runtime roots must be directories")
        active = ["principal", "manager", "worker"]
        if frozen.memory.enabled and frozen.memory.shared_knowledge and frozen.memory.curator_enabled:
            active.append("curator")
        for role in active:
            model = frozen.models[role]
            parsed = urlsplit(model.base_url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or not model.model or not model.model.strip():
                raise ValueError("invalid active role model")
            parsed.port
        if stopping_signal is not None:
            event("serve_stopped", pid=os.getpid(), signal=stopping_signal)
            return 130 if stopping_signal == signal.SIGINT else 143
        if failure is not None:
            return 1

        from . import app as boundary
        import uvicorn
        boundary._startup_config = frozen
        boundary._startup_deadline = deadline
        boundary._stopping = stopping_signal is not None or failure is not None
        boundary._startup_failure = boundary._shutdown_failure = None
        boundary._knowledge_result = boundary._knowledge_context = None
        boundary._phase = "preflight"

        class ForegroundServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                yield

            async def startup(self, sockets=None):
                nonlocal ready_published, phase, failure, owner_loop
                owner_loop = asyncio.get_running_loop()
                await super().startup(sockets=sockets)
                with guard:
                    phase = boundary._phase = "listener"
                    if time.monotonic() >= deadline and failure is None and stopping_signal is None:
                        expire_startup()
                    if self.started and failure is None and stopping_signal is None and not boundary._stopping:
                        ready_published = True
                        startup_timer.cancel()
                        event("serve_ready", stdout=True, pid=os.getpid(), host=host, port=port, state_dir=str(frozen.runtime.state_dir))
                    else:
                        self.should_exit = True

        server = ForegroundServer(uvicorn.Config(boundary.app, host=host, port=port, workers=1, lifespan="on", timeout_graceful_shutdown=shutdown_budget, log_level="warning"))
        phase = "listener"
        address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        listener = socket.socket(address[0], address[1], address[2])
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(address[4])
        listener.listen(128)
        listener.setblocking(False)
        if stopping_signal is None and failure is None:
            asyncio.run(server.serve(sockets=[listener]))
        if boundary.runtime_lease is not None and not boundary._shutdown_failure:
            asyncio.run(boundary._shutdown_runtime())
        if boundary._shutdown_failure:
            failure = ("shutdown_failed", "shutdown", None)
        elif boundary._startup_failure is not None and stopping_signal is None and failure is None:
            error = boundary._startup_failure
            failure = (error.reason, error.phase, error.knowledge_reason)
        elif not server.started and stopping_signal is None and failure is None:
            failure = ("runtime_initialization_failed", current_phase(), None)
        if failure is not None:
            publish_failure()
            return 1
        if stopping_signal is not None:
            event("serve_stopped", pid=os.getpid(), signal=stopping_signal)
            return 130 if stopping_signal == signal.SIGINT else 143
        return 0
    except BaseException as exc:
        if boundary is not None and boundary._startup_failure is not None and failure is None:
            error = boundary._startup_failure
            if stopping_signal is not None and error.reason != "shutdown_failed":
                event("serve_stopped", pid=os.getpid(), signal=stopping_signal)
                return 130 if stopping_signal == signal.SIGINT else 143
            failure = (error.reason, error.phase, error.knowledge_reason)
        if failure is None:
            if boundary is not None and boundary.runtime_lease is not None:
                failure = ("shutdown_failed", "shutdown", None)
            elif phase == "listener":
                failure = ("listener_error", "listener", None)
            else:
                failure = ("path_error" if exc.__class__.__name__ == "RuntimeBoundaryError" else "config_error", "preflight", None)
        publish_failure()
        return 1
    finally:
        if startup_timer is not None:
            startup_timer.cancel()
        if teardown_timer is not None:
            teardown_timer.cancel()
        if listener is not None:
            listener.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if boundary is not None:
            boundary._startup_config = boundary._startup_deadline = None
