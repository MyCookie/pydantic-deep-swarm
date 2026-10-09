"""Strict loopback model fixture for installed-console acceptance.

This module has no dependency on the source package. It exercises actual HTTP
catalog and completion traffic: the principal and manager text protocol, and the
worker's native OpenAI tool calling (``tools`` / ``tool_calls`` / ``role: tool``).
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
WORKER_HEADERS = {
    "host": "*", "content-length": "*", "accept": "application/json", "accept-encoding": "gzip, deflate",
    "content-type": "application/json", "user-agent": "agent-team-worker", "connection": "close",
    "authorization": "Bearer fixture-inference-key",
}
WORKER_OUTPUT = {
    "status": "complete", "summary": "Created and read exact artifact",
    "requirement_results": [{"requirement_id": "artifact", "status": "satisfied",
                             "evidence": ["read_file verified exact acceptance.txt content"]}],
    "acceptance_results": [{"criterion_id": "exact-bytes", "status": "passed",
                            "verification_method": "read_file and runtime digest",
                            "evidence": ["read_file verified exact 16 UTF-8 bytes ending in LF"]}],
    "artifacts": [{"path": "acceptance.txt", "description": "Acceptance artifact",
                   "created_by": "software-engineer"}],
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

    def response(self, payload: dict, headers: dict | None = None) -> dict:
        """Return the complete HTTP response body for one completion request."""
        assert payload.get("model") == "fixture-model"
        assert payload.get("stream") is False
        messages = payload.get("messages")
        assert isinstance(messages, list) and len(messages) >= 2
        assert isinstance(messages[0], dict) and messages[0].get("role") == "system"
        assert isinstance(messages[0].get("content"), str)
        if "tools" in payload:
            assert {name.lower(): value for name, value in (headers or {}).items()} | {
                "content-length": "*", "host": "*"} == WORKER_HEADERS, "unexpected worker headers"
            return self.worker_response(payload)
        assert set(payload) == {"model", "messages", "stream"}, "unexpected completion payload keys"
        assert all(isinstance(message, dict) and set(message) == {"role", "content"}
                   and message["role"] in {"system", "user", "assistant"}
                   and isinstance(message["content"], str) for message in messages)
        content = self.text_response(messages)
        return {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}]}

    def text_response(self, messages: list[dict]) -> object:
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
        assert "You are the Team Manager." in system, "unexpected completion role"
        assert "ManagerPlan" in system, "unexpected manager repair request"
        assert self.calls["manager"] == 0, "extra manager request"
        assert any(BRIEF["objective"] in message["content"] for message in messages[1:]), "unknown delegated scenario"
        self.calls["manager"] += 1
        return {"tasks": [{"task_id": "write-artifact", "role": "software-engineer",
                           "objective": BRIEF["objective"], "requirement_ids": ["artifact"],
                           "tools": ["write_file", "read_file"]}], "review_required": False}

    def worker_response(self, payload: dict) -> dict:
        """Native tool-calling worker script: write_file, read_file, then final_result."""
        assert set(payload) == {"model", "messages", "stream", "tools", "tool_choice"}, "unexpected worker payload keys"
        assert payload["tool_choice"] == "required"
        tools = payload["tools"]
        assert all(set(tool) == {"type", "function"} and tool["type"] == "function"
                   and isinstance(tool["function"].get("parameters"), dict) for tool in tools)
        assert sorted(tool["function"]["name"] for tool in tools) == ["final_result", "read_file", "write_file"], \
            "worker offered tools other than its grant"
        messages = payload["messages"]
        assert "EXECUTABLE TOOLS" in messages[0]["content"], "unexpected worker system prompt"
        assert self.calls["manager"] == 1 and self.calls["worker"] < 3, "unexpected worker request"
        step = self.calls["worker"]
        roles = ["system", "user"] + ["assistant", "tool"] * step
        assert [message.get("role") for message in messages] == roles, "unexpected worker transcript"
        assert BRIEF["objective"] in messages[1]["content"], "unknown worker scenario"
        if step >= 1:
            self.assert_tool_round(messages[2], messages[3], "call_write", "write_file")
            written = json.loads(messages[3]["content"])
            assert written["artifact"]["sha256"] == ARTIFACT_SHA256 and written["artifact"]["size_bytes"] == 16
        if step >= 2:
            self.assert_tool_round(messages[4], messages[5], "call_read", "read_file")
            assert json.loads(messages[5]["content"])["output"] == ARTIFACT_BYTES.decode("utf-8")
        self.calls["worker"] += 1
        if step == 0:
            call = ("call_write", "write_file", {"path": "acceptance.txt", "content": ARTIFACT_BYTES.decode("utf-8")})
        elif step == 1:
            call = ("call_read", "read_file", {"path": "acceptance.txt"})
        else:
            call = ("call_final", "final_result", WORKER_OUTPUT)
        call_id, name, arguments = call
        return {
            "id": f"chatcmpl-fixture-worker-{step}",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "fixture-model",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }]},
            }],
        }

    @staticmethod
    def assert_tool_round(assistant: dict, result: dict, call_id: str, name: str) -> None:
        calls = assistant.get("tool_calls")
        assert isinstance(calls, list) and len(calls) == 1, "expected one native tool call per round"
        assert calls[0]["id"] == call_id and calls[0]["type"] == "function"
        assert calls[0]["function"]["name"] == name
        assert set(result) == {"role", "tool_call_id", "content"} and result["tool_call_id"] == call_id
        decoded = json.loads(result["content"])
        assert decoded["tool"] == name and decoded["ok"] is True, "tool result was not a success"


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
                self.send(200, state.response(payload, dict(self.headers)))
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
