"""Seam S5: the standalone loopback model fixture rejects defective worker requests.

Each test captures the real worker's first request on a loopback listener, then
sends it, or a copy with one defect, to the fixture over real HTTP.
"""

from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading

import pytest

from agent_team.contracts import WorkerOutput
from agent_team.models.worker_model import create_worker_model
from agent_team.worker_tools import ToolAwareWorkerAgent, WorkerToolExecutor

spec = importlib.util.spec_from_file_location(
    "standalone_fixture", Path(__file__).resolve().parents[1] / "scripts/standalone_fixture.py",
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


async def capture_worker_request(workspace: Path) -> tuple[list[tuple[str, str]], dict]:
    """Return the header lines and JSON body of the real worker's first request."""
    captured = []

    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            captured.append((self.headers.items(), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({
                "id": "chatcmpl-capture", "object": "chat.completion", "created": 0, "model": "fixture-model",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_final", "type": "function",
                        "function": {"name": "final_result", "arguments": json.dumps(fixture.WORKER_OUTPUT)},
                    }]}}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        model = create_worker_model(
            f"http://127.0.0.1:{server.server_port}/v1", "fixture-model", "fixture-inference-key"
        )
        executor = WorkerToolExecutor(workspace, allowed_tools=["write_file", "read_file"])
        await ToolAwareWorkerAgent(model, "software-engineer", executor).run(
            fixture.BRIEF["objective"], response_format=WorkerOutput
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(captured) == 1
    return captured[0]


def send_to_fixture(headers: list[tuple[str, str]], payload: dict) -> tuple[int, list[str]]:
    """POST one worker request to a fresh fixture whose manager turn is done."""
    body = json.dumps(payload).encode()
    with fixture.model_server() as (state, base_url):
        state.calls["manager"] = 1
        connection = HTTPConnection(base_url.removeprefix("http://").removesuffix("/v1"), timeout=20)
        try:
            connection.putrequest("POST", "/v1/chat/completions", skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, str(len(body)) if name.lower() == "content-length" else value)
            connection.endheaders(body)
            status = connection.getresponse().status
        finally:
            connection.close()
        return status, list(state.errors)


@pytest.mark.asyncio
async def test_fixture_accepts_the_real_worker_request(tmp_path):
    headers, payload = await capture_worker_request(tmp_path)

    assert send_to_fixture(headers, payload) == (200, [])


@pytest.mark.asyncio
async def test_fixture_rejects_a_duplicated_authorization_header(tmp_path):
    headers, payload = await capture_worker_request(tmp_path)

    status, errors = send_to_fixture(headers + [("Authorization", "Bearer fixture-inference-key")], payload)

    assert (status, errors) == (500, ["unexpected worker headers"])


@pytest.mark.asyncio
async def test_fixture_rejects_final_result_parameters_that_are_not_the_worker_output_schema(tmp_path):
    headers, payload = await capture_worker_request(tmp_path)
    final_result = next(tool["function"] for tool in payload["tools"] if tool["function"]["name"] == "final_result")
    del final_result["parameters"]["properties"]["evidence"]

    status, errors = send_to_fixture(headers, payload)

    assert (status, errors) == (500, ["final_result parameters are not the WorkerOutput schema"])
