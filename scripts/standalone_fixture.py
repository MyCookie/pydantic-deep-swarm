"""Strict loopback model fixture for installed-console acceptance.

This module has no dependency on the source package. It exercises actual HTTP
catalog and completion traffic, including the worker's inner JSON tool protocol.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


ARTIFACT_BYTES = b"standalone gate\n"
ARTIFACT_SHA256 = "a708b4e69f16df1936fcab90033eec606052015046e8cec350a06b8cad6533d2"
BRIEF = {
    "objective": "Write acceptance.txt containing standalone gate followed by LF",
    "requirements": [{"id": "artifact", "description": "Create the exact acceptance artifact"}],
    "acceptance_criteria": [{"id": "exact-bytes", "description": "Read and verify artifact bytes",
                             "verification_method": "read_file and runtime digest"}],
    "permitted_actions": ["write_file", "read_file"],
    "desired_output": "A complete report with verified acceptance.txt",
}


class FixtureState:
    def __init__(self):
        self.calls = Counter()
        self.errors: list[str] = []
        self.events: list[dict] = []
        self.catalog_entered = threading.Event()
        self.catalog_release = threading.Event()
        self.catalog_release.set()
        self.completion_entered = threading.Event()
        self.completion_release = threading.Event()
        self.completion_release.set()
        self.catalog: object = {"data": [{"id": "fixture-model"}]}

    def response(self, payload: dict) -> object:
        assert set(payload) == {"model", "messages", "stream"}, "unexpected completion payload keys"
        assert payload["model"] == "fixture-model"
        assert payload["stream"] is False
        messages = payload["messages"]
        assert isinstance(messages, list) and len(messages) >= 2
        assert all(isinstance(message, dict) and set(message) == {"role", "content"}
                   and message["role"] in {"system", "user", "assistant"}
                   and isinstance(message["content"], str) for message in messages)
        assert messages[0]["role"] == "system"
        system = messages[0]["content"]
        if "You are the Principal:" in system:
            if "PrincipalDecision" in system:
                assert self.calls["direct"] < 2, "extra direct request"
                assert any("What is 2+2?" in message["content"] for message in messages[1:]), "unknown direct scenario"
                self.calls["direct"] += 1
                return {"action": "answer", "response": "The answer is 4."}
            assert self.calls["manager"] == 1 and self.calls["worker"] == 3 and self.calls["summary"] == 0, "unexpected summary request"
            self.calls["summary"] += 1
            return "Created and verified acceptance.txt."
        if "You are the Team Manager." in system:
            assert "ManagerPlan" in system, "unexpected manager repair request"
            assert self.calls["manager"] == 0, "extra manager request"
            assert any(BRIEF["objective"] in message["content"] for message in messages[1:]), "unknown delegated scenario"
            self.calls["manager"] += 1
            return {"tasks": [{"task_id": "write-artifact", "role": "software-engineer",
                               "objective": BRIEF["objective"], "requirement_ids": ["artifact"],
                               "tools": ["write_file", "read_file"]}], "review_required": False}
        assert "EXECUTABLE TOOLS" in system, "unexpected completion role"
        assert self.calls["manager"] == 1 and self.calls["worker"] < 3, "unexpected worker request"
        self.calls["worker"] += 1
        results = [message for message in messages if message["role"] == "user"
                   and message["content"].startswith("TOOL RESULTS:\n")]
        if not results:
            return {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "acceptance.txt", "content": ARTIFACT_BYTES.decode("utf-8")}}]}
        for message in results:
            decoded = json.loads(message["content"].split("\n", 1)[1])
            assert len(decoded) == 1 and decoded[0]["ok"] is True
        if len(results) == 1:
            assert json.loads(results[0]["content"].split("\n", 1)[1])[0]["tool"] == "write_file"
            return {"tool_calls": [{"name": "read_file", "arguments": {"path": "acceptance.txt"}}]}
        assert len(results) == 2
        read = json.loads(results[1]["content"].split("\n", 1)[1])[0]
        assert read["tool"] == "read_file" and read["output"] == ARTIFACT_BYTES.decode("utf-8")
        return {"status": "complete", "summary": "Created and read exact artifact",
                "requirement_results": [{"requirement_id": "artifact", "status": "satisfied",
                                         "evidence": ["read_file verified exact acceptance.txt content"]}],
                "acceptance_results": [{"criterion_id": "exact-bytes", "status": "passed",
                                        "verification_method": "read_file and runtime digest",
                                        "evidence": ["read_file verified exact 16 UTF-8 bytes ending in LF"]}],
                "artifacts": [{"path": "acceptance.txt", "description": "Acceptance artifact",
                               "created_by": "software-engineer"}]}


@contextmanager
def model_server():
    state = FixtureState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value):
            body = json.dumps(value).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            try:
                assert self.path == "/v1/models", "unexpected catalog path"
                assert "Authorization" not in self.headers, "credentials sent to discovery"
                state.events.append({"method": "GET", "path": self.path, "monotonic": time.monotonic(), "credential_present": False})
                state.calls["catalog"] += 1
                state.catalog_entered.set()
                assert state.catalog_release.wait(15), "catalog barrier timeout"
                self.send(200, state.catalog)
            except Exception as error:
                state.errors.append(str(error))
                self.send(500, {"error": "fixture assertion failed"})

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions", "unexpected completion path"
                assert self.headers.get("Authorization") == "Bearer fixture-inference-key"
                state.events.append({"method": "POST", "path": self.path, "monotonic": time.monotonic(), "fixture_inference_auth": True})
                size = int(self.headers["Content-Length"])
                assert 0 < size < 1_000_000
                payload = json.loads(self.rfile.read(size))
                state.completion_entered.set()
                assert state.completion_release.wait(15), "completion barrier timeout"
                response = state.response(payload)
                content = response if isinstance(response, str) else json.dumps(response)
                self.send(200, {"choices": [{"message": {"content": content}}]})
            except Exception as error:
                state.errors.append(str(error))
                self.send(500, {"error": "fixture assertion failed"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        state.catalog_release.set()
        state.completion_release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
