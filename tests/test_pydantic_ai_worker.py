"""Seam tests for the Pydantic AI worker loop (issue #14, Phase 1).

Seam W drives ``ToolAwareWorkerAgent.run`` with a scripted Pydantic AI
``FunctionModel``. Seam E runs a delegated brief through the engine against a
strict fake OpenAI-compatible HTTP server speaking native tool calls.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx2  # the OpenAI SDK's HTTP client; used only to route a test transport
import pytest
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_team.config import Config, MemoryConfig, ModelConfig, RuntimeConfig
from agent_team.contracts import (
    AcceptanceCriterion,
    CompletionReport,
    ManagerPlan,
    ProjectBrief,
    Requirement,
    WorkerOutput,
    WorkerTaskSpec,
)
from agent_team.engine import AgentTeamEngine
from agent_team.runtime.cancellation import WorkerTurnBudget
from agent_team.worker_tools import ToolAwareWorkerAgent, WorkerToolExecutor


class Script:
    """A FunctionModel whose responses are produced by one step per request."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[tuple[list, AgentInfo]] = []
        self.model = FunctionModel(self._respond)

    def _respond(self, messages, info: AgentInfo) -> ModelResponse:
        self.requests.append((list(messages), info))
        step = self.steps[len(self.requests) - 1]
        return step(messages, info)

    @property
    def calls(self) -> int:
        return len(self.requests)


def tool_returns(messages) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]


def final(payload: dict):
    def step(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    return step


def call(name: str, arguments: dict, count: int = 1):
    def step(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(name, dict(arguments), tool_call_id=f"{name}-{index}") for index in range(count)]
        )

    return step


@pytest.mark.asyncio
async def test_w1_native_write_file_records_artifact_and_returns_structured_output(tmp_path):
    def summarize(messages, info):
        returned = tool_returns(messages)
        assert [part.tool_name for part in returned] == ["write_file"]
        assert returned[0].content["ok"] is True
        assert returned[0].content["output"] == "wrote answer.txt"
        return final({"status": "complete", "summary": "wrote answer"})(messages, info)

    script = Script(call("write_file", {"path": "answer.txt", "content": "done\n"}), summarize)
    executor = WorkerToolExecutor(tmp_path)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor).run(
        "write the answer", response_format=WorkerOutput
    )

    assert isinstance(result, WorkerOutput)
    assert result.status == "complete"
    assert result.summary == "wrote answer"
    assert (tmp_path / "answer.txt").read_bytes() == b"done\n"
    assert [artifact.path for artifact in executor.artifacts] == ["answer.txt"]
    assert executor.artifacts[0].sha256 == "d117fa006ba9208500b2930ce69cbde436c647afa917cb7396a9bc9111a46dd2"
    assert executor.artifacts[0].verified is True
    assert script.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "offered"),
    [
        (["read_file"], ["read_file"]),
        (["write_file", "list_files"], ["list_files", "write_file"]),
        ([], []),
    ],
)
async def test_w2_only_granted_tools_are_offered_to_the_model(tmp_path, allowed, offered):
    seen: list[list[str]] = []

    def inspect_tools(messages, info):
        seen.append(sorted(tool.name for tool in info.function_tools))
        return final({"status": "complete", "summary": "inspected"})(messages, info)

    script = Script(inspect_tools)
    executor = WorkerToolExecutor(tmp_path, allowed_tools=allowed)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor).run(
        "report the tools", response_format=WorkerOutput
    )

    assert result.summary == "inspected"
    assert seen == [offered]


@pytest.mark.asyncio
async def test_w3_tool_call_batch_cannot_exceed_max_tool_calls(tmp_path):
    async def yield_to_other_tool_calls():
        # Without a sequential barrier, sibling calls in the batch would run here.
        await asyncio.sleep(0)

    script = Script(
        call("write_file", {"path": "batch.txt", "content": "x"}, count=3),
        final({"status": "complete", "summary": "must not be reached"}),
    )
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=1)
    budget = WorkerTurnBudget(max_turns=10, before_turn=yield_to_other_tool_calls)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor, turn_budget=budget).run(
        "finish the assignment", response_format=WorkerOutput
    )

    assert isinstance(result, WorkerOutput)
    assert result.status == "partial"
    assert result.unresolved == ["Worker tool-call limit of 1 reached before a final result."]
    assert script.calls == 1
    assert len(executor.results) == 1
    assert [artifact.path for artifact in result.artifacts] == ["batch.txt"]


@pytest.mark.asyncio
async def test_w3_tool_call_limit_spans_model_requests(tmp_path):
    script = Script(*(call("list_files", {"path": "."}) for _ in range(4)))
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=2)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor, max_turns=10).run(
        "finish the assignment", response_format=WorkerOutput
    )

    assert result.status == "partial"
    assert result.unresolved == ["Worker tool-call limit of 2 reached before a final result."]
    assert script.calls == 3
    assert len(executor.results) == 2


@pytest.mark.asyncio
async def test_w4_model_requests_and_tool_calls_consume_the_turn_budget(tmp_path):
    events: list[str] = []
    budget = WorkerTurnBudget(
        max_turns=3,
        worker_id="worker-7",
        before_turn=lambda: events.append("before"),
        on_turn=lambda kind: events.append(kind),
    )
    script = Script(*(call("write_file", {"path": "step.txt", "content": "partial"}) for _ in range(5)))
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=20)
    agent = ToolAwareWorkerAgent(script.model, "worker", executor, turn_budget=budget)

    result = await agent.run("finish the assignment", response_format=WorkerOutput)

    assert events == ["before", "model", "before", "tool", "before", "model", "before"]
    assert script.calls == 2
    assert len(executor.results) == 1
    assert (agent.turn_usage.model_turns, agent.turn_usage.tool_calls) == (2, 1)
    assert result.status == "partial"
    assert result.unresolved == ["Worker worker-7 exceeded turn limit of 3 (model turns=2, tool calls=1)"]
    assert [artifact.path for artifact in result.artifacts] == ["step.txt"]


def retry_prompts(messages) -> list[RetryPromptPart]:
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, RetryPromptPart)
    ]


@pytest.mark.asyncio
async def test_w5_invalid_structured_output_is_retried_once(tmp_path):
    def corrected(messages, info):
        assert len(retry_prompts(messages)) == 1
        return final({"status": "complete", "summary": "corrected"})(messages, info)

    script = Script(final({"status": "finished-ish"}), corrected)

    result = await ToolAwareWorkerAgent(script.model, "worker", WorkerToolExecutor(tmp_path)).run(
        "finish", response_format=WorkerOutput
    )

    assert result == WorkerOutput(status="complete", summary="corrected")
    assert script.calls == 2


@pytest.mark.asyncio
async def test_w5_invalid_structured_output_after_retry_raises(tmp_path):
    script = Script(final({"status": "finished-ish"}), final({"status": "still-wrong"}), final({}))

    with pytest.raises(UnexpectedModelBehavior):
        await ToolAwareWorkerAgent(script.model, "worker", WorkerToolExecutor(tmp_path)).run(
            "finish", response_format=WorkerOutput
        )

    assert script.calls == 2


CRITERIA = [{"id": "a1", "description": "tests pass", "verification_method": "pytest"}]
PASSED_A1 = {"criterion_id": "a1", "status": "passed", "evidence": ["pytest: 1 passed"], "verification_method": "pytest"}


@pytest.mark.asyncio
async def test_w6_missing_acceptance_ids_are_requested_once(tmp_path):
    def with_ids(messages, info):
        prompts = retry_prompts(messages)
        assert len(prompts) == 1
        assert "omitted required acceptance criterion IDs: a1" in str(prompts[0].content)
        return final({"status": "complete", "summary": "done", "acceptance_results": [PASSED_A1]})(messages, info)

    script = Script(final({"status": "complete", "summary": "done"}), with_ids)
    executor = WorkerToolExecutor(tmp_path, acceptance_criteria=CRITERIA)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor).run("finish", response_format=WorkerOutput)

    assert [item.criterion_id for item in result.acceptance_results] == ["a1"]
    assert result.acceptance_results[0].evidence == ["pytest: 1 passed"]
    assert script.calls == 2


@pytest.mark.asyncio
async def test_w6_acceptance_ids_still_missing_after_retry_return_output_as_is(tmp_path):
    script = Script(
        final({"status": "complete", "summary": "first"}),
        final({"status": "complete", "summary": "second"}),
        final({"status": "complete", "summary": "third", "acceptance_results": [PASSED_A1]}),
    )
    executor = WorkerToolExecutor(tmp_path, acceptance_criteria=CRITERIA)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor).run("finish", response_format=WorkerOutput)

    assert result == WorkerOutput(status="complete", summary="second")
    assert script.calls == 2


@pytest.mark.asyncio
async def test_w7_without_response_format_returns_plain_text(tmp_path):
    def answer(messages, info):
        assert info.allow_text_output is True
        assert info.output_tools == []
        return ModelResponse(parts=[TextPart("plain answer")])

    script = Script(call("read_file", {"path": "missing.txt"}), answer)

    result = await ToolAwareWorkerAgent(script.model, "worker", WorkerToolExecutor(tmp_path)).run("answer")

    assert result == "plain answer"
    assert script.calls == 2


# --- Seam E: the engine against a strict fake OpenAI-compatible server ------


@dataclass
class Reply:
    """A non-completion HTTP answer from the fake server."""

    status: int
    body: dict
    headers: dict = field(default_factory=dict)


class FakeOpenAIServer:
    """Loopback chat-completions server answering from a strict request script."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.errors: list[str] = []

    def __enter__(self):
        server_state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    assert self.path == "/v1/chat/completions", f"unexpected path {self.path}"
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    server_state.requests.append({"headers": dict(self.headers), "body_length": len(raw)})
                    payload = json.loads(raw)
                    server_state.requests[-1]["payload"] = payload
                    index = len(server_state.requests) - 1
                    assert index < len(server_state.steps), "unexpected extra completion request"
                    message = server_state.steps[index](payload)
                    if isinstance(message, Reply):
                        self.reply(message.status, message.body, message.headers)
                        return
                    status, body = 200, {
                        "id": f"chatcmpl-{index}",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": payload["model"],
                        "choices": [{
                            "index": 0,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                            "message": message,
                        }],
                    }
                except Exception as error:  # surfaced through self.errors
                    server_state.errors.append(f"{type(error).__name__}: {error}")
                    status, body = 500, {"error": "fake server assertion failed"}
                self.reply(status, body)

            def do_GET(self):
                # A followed 302 arrives as GET; record it so tests can see where it went.
                server_state.requests.append({"method": "GET", "headers": dict(self.headers)})
                server_state.errors.append(f"unexpected GET {self.path}")
                self.reply(404, {"error": "fake server only serves POST"})

            def reply(self, status, body, headers=None):
                encoded = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up first, e.g. after its timeout

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.netloc = f"127.0.0.1:{self._server.server_port}"
        self.base_url = f"http://{self.netloc}/v1"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def native_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


def tool_names(payload: dict) -> list[str]:
    return sorted(tool["function"]["name"] for tool in payload.get("tools", []))


ARTIFACT_BYTES = b"standalone gate\n"
ARTIFACT_SHA256 = "a708b4e69f16df1936fcab90033eec606052015046e8cec350a06b8cad6533d2"


def delegated_engine(tmp_path, worker_base_url: str, tools: list[str]) -> AgentTeamEngine:
    config = Config(
        runtime=RuntimeConfig(
            state_dir=tmp_path / "state",
            workspace_dir=tmp_path / "workspace",
            max_workers=1,
            max_concurrent_workers=1,
            worker_timeout_seconds=30,
            team_timeout_seconds=30,
        ),
        memory=MemoryConfig(enabled=False, shared_knowledge=False, curator_enabled=False),
        models={
            "principal": ModelConfig(model="principal-model", base_url="http://127.0.0.1:9/v1"),
            "manager": ModelConfig(model="manager-model", base_url="http://127.0.0.1:9/v1"),
            "worker": ModelConfig(model="worker-model", base_url=worker_base_url),
        },
    )
    engine = AgentTeamEngine(config)

    class Principal:
        async def run(self, prompt, response_format=None):
            return "rendered"

    class Manager:
        async def run(self, prompt, response_format=None):
            assert response_format is ManagerPlan
            return ManagerPlan(tasks=[WorkerTaskSpec(
                task_id="write-artifact",
                role="software-engineer",
                objective="Write acceptance.txt",
                requirement_ids=["artifact"],
                tools=tools,
            )])

    engine.principal = Principal()
    engine.manager = Manager()
    return engine


DELEGATED_BRIEF = ProjectBrief(
    objective="Write acceptance.txt containing standalone gate followed by LF",
    requirements=[Requirement(id="artifact", description="Create the exact acceptance artifact")],
    acceptance_criteria=[AcceptanceCriterion(
        id="exact-bytes", description="Verify artifact bytes", verification_method="runtime digest"
    )],
    desired_output="A complete report with verified acceptance.txt",
)

COMPLETE_OUTPUT = {
    "status": "complete",
    "summary": "Created exact artifact",
    "requirement_results": [{"requirement_id": "artifact", "status": "satisfied",
                             "evidence": ["write_file wrote acceptance.txt"]}],
    "acceptance_results": [{"criterion_id": "exact-bytes", "status": "passed",
                            "evidence": ["runtime SHA-256 recorded"], "verification_method": "runtime digest"}],
}


@pytest.mark.asyncio
async def test_e1_delegated_brief_uses_native_tool_calls_and_verifies_artifact_bytes(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    def first(payload):
        assert payload["model"] == "worker-model"
        assert tool_names(payload) == ["final_result", "read_file", "write_file"]
        assert payload["messages"][0]["role"] == "system"
        assert "EXECUTABLE TOOLS" in payload["messages"][0]["content"]
        return native_call("call_write", "write_file", {
            "path": "acceptance.txt", "content": ARTIFACT_BYTES.decode("utf-8"),
        })

    def second(payload):
        assistant = [message for message in payload["messages"] if message["role"] == "assistant"]
        assert assistant[-1]["tool_calls"][0]["id"] == "call_write"
        results = [message for message in payload["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["call_write"]
        returned = json.loads(results[0]["content"])
        assert returned["tool"] == "write_file" and returned["ok"] is True
        assert returned["artifact"]["sha256"] == ARTIFACT_SHA256
        return native_call("call_final", "final_result", COMPLETE_OUTPUT)

    with FakeOpenAIServer(first, second) as server:
        engine = delegated_engine(tmp_path, server.base_url, ["write_file", "read_file"])
        try:
            result = await engine.run_delegated_brief(DELEGATED_BRIEF, session_id="native")
        finally:
            await engine.close()

    assert server.errors == []
    assert len(server.requests) == 2
    report = result.report
    assert isinstance(report, CompletionReport)
    assert report.status == "complete"
    assert [(item.criterion_id, item.status) for item in report.acceptance_results] == [("exact-bytes", "passed")]
    assert len(report.artifacts) == 1
    artifact = report.artifacts[0]
    assert (artifact.path, artifact.sha256, artifact.size_bytes, artifact.verified) == (
        "acceptance.txt", ARTIFACT_SHA256, 16, True,
    )
    assert (tmp_path / "workspace" / "acceptance.txt").read_bytes() == ARTIFACT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(("llm_api_key", "expected_authorization"), [(None, None), ("fixture-key", "Bearer fixture-key")])
async def test_e2_worker_bearer_auth_only_with_llm_api_key_and_no_openai_env_influence(
    tmp_path, monkeypatch, llm_api_key, expected_authorization
):
    if llm_api_key is None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_API_KEY", llm_api_key)
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "env-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "env-project")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "\n".join([
        "X-Env-Header: leaked",
        "Host: evil.example",
        "Accept: text/html",
        "Accept-Encoding: br",
        "Content-Type: text/plain",
        "Content-Length: 3",
        "User-Agent: env-agent",
        "Connection: keep-alive",
    ]))

    def only_final(payload):
        return native_call("call_final", "final_result", COMPLETE_OUTPUT)

    with FakeOpenAIServer(only_final) as server:
        engine = delegated_engine(tmp_path, server.base_url, ["read_file"])
        try:
            result = await engine.run_delegated_brief(DELEGATED_BRIEF, session_id="auth")
        finally:
            await engine.close()

    assert server.errors == []
    assert len(server.requests) == 1
    headers = {name.lower(): value for name, value in server.requests[0]["headers"].items()}
    expected = {
        "host": server.netloc,
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "content-length": str(server.requests[0]["body_length"]),
        "user-agent": "agent-team-worker",
        "connection": "close",
    }
    if expected_authorization is not None:
        expected["authorization"] = expected_authorization
    assert headers == expected
    assert result.report.acceptance_results[0].status == "passed"


@pytest.mark.asyncio
async def test_w5_engine_marks_worker_failed_when_output_stays_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    invalid = native_call("call_final", "final_result", {"status": "finished-ish"})

    with FakeOpenAIServer(lambda payload: invalid, lambda payload: invalid) as server:
        engine = delegated_engine(tmp_path, server.base_url, ["read_file"])
        try:
            result = await engine.run_delegated_brief(DELEGATED_BRIEF, session_id="invalid")
        finally:
            await engine.close()

    assert server.errors == []
    assert len(server.requests) == 2
    assert result.report.status != "complete"
    assert "One or more workers did not complete." in result.report.unresolved_items


@pytest.mark.asyncio
async def test_w4_model_turn_at_the_limit_returns_partial_for_unknown_tool_calls(tmp_path):
    script = Script(*(call(f"unknown_tool_{index}", {}) for index in range(6)))
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=20)
    agent = ToolAwareWorkerAgent(script.model, "worker", executor, max_turns=3)

    result = await agent.run("finish the assignment", response_format=WorkerOutput)

    assert isinstance(result, WorkerOutput)
    assert result.status == "partial"
    assert result.unresolved == ["Worker worker exceeded turn limit of 3 (model turns=3, tool calls=0)"]
    assert script.calls == 3
    assert (agent.turn_usage.model_turns, agent.turn_usage.tool_calls) == (3, 0)
    assert executor.results == []


@pytest.mark.asyncio
async def test_w4_repeated_invalid_call_to_one_tool_raises_without_running_it(tmp_path):
    invalid = call("write_file", {"path": "missing-content.txt"})
    script = Script(invalid, invalid, final({"status": "complete", "summary": "must not be reached"}))
    executor = WorkerToolExecutor(tmp_path)
    agent = ToolAwareWorkerAgent(script.model, "worker", executor, max_turns=10)

    with pytest.raises(UnexpectedModelBehavior):
        await agent.run("finish the assignment", response_format=WorkerOutput)

    assert script.calls == 2
    assert len(retry_prompts(script.requests[1][0])) == 1
    assert executor.results == []
    assert (agent.turn_usage.model_turns, agent.turn_usage.tool_calls) == (2, 0)
    assert not (tmp_path / "missing-content.txt").exists()


def answer_after_timeout(payload):
    time.sleep(2.0)
    return native_call("call_final", "final_result", COMPLETE_OUTPUT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step",
    [
        pytest.param(lambda payload: Reply(503, {"error": {"message": "overloaded"}}), id="503-not-retried"),
        pytest.param(answer_after_timeout, id="timeout-not-retried"),
    ],
)
async def test_e_worker_transport_failure_is_one_request_and_fails_the_worker(tmp_path, monkeypatch, step):
    import agent_team.models.worker_model as worker_model

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # Shrink only the timeout so the slow answer is cut off deterministically.
    monkeypatch.setattr(worker_model, "REQUEST_TIMEOUT_SECONDS", 0.5)

    with FakeOpenAIServer(step) as server:
        engine = delegated_engine(tmp_path, server.base_url, ["read_file"])
        try:
            result = await engine.run_delegated_brief(DELEGATED_BRIEF, session_id="transport")
        finally:
            await engine.close()

    assert server.errors == []
    assert len(server.requests) == 1
    assert {name.lower(): value for name, value in server.requests[0]["headers"].items()}["connection"] == "close"
    assert result.report.status != "complete"
    assert "One or more workers did not complete." in result.report.unresolved_items


@pytest.mark.asyncio
async def test_w7_without_response_format_a_limit_returns_the_partial_summary_text(tmp_path):
    script = Script(call("list_files", {"path": "."}), lambda messages, info: ModelResponse(parts=[TextPart("late")]))
    executor = WorkerToolExecutor(tmp_path, max_tool_calls=0)

    result = await ToolAwareWorkerAgent(script.model, "worker", executor).run("answer")

    assert result == (
        "Worker stopped before producing a final result: "
        "Worker tool-call limit of 0 reached before a final result."
    )
    assert script.calls == 1
    assert executor.results == []


def following_redirects(monkeypatch):
    """Patch the worker client to follow redirects, as a hostile configuration would."""
    import agent_team.models.worker_model as worker_model

    real = worker_model.openai

    def follow(**kwargs):
        return real.DefaultAsyncHttpxClient(**{**kwargs, "follow_redirects": True})

    monkeypatch.setattr(
        worker_model, "openai", SimpleNamespace(DefaultAsyncHttpxClient=follow, AsyncOpenAI=real.AsyncOpenAI)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("follow", [False, True], ids=["redirect-refused", "followed-redirect-gets-no-key"])
async def test_e_worker_key_never_reaches_a_redirect_target(tmp_path, monkeypatch, follow):
    monkeypatch.setenv("LLM_API_KEY", "worker-secret")
    if follow:
        following_redirects(monkeypatch)

    with FakeOpenAIServer() as other_host:
        redirect = Reply(302, {}, {"Location": f"{other_host.base_url}/chat/completions"})
        with FakeOpenAIServer(lambda payload: redirect) as server:
            engine = delegated_engine(tmp_path, server.base_url, ["read_file"])
            try:
                result = await engine.run_delegated_brief(DELEGATED_BRIEF, session_id="redirect")
            finally:
                await engine.close()

    assert server.errors == []
    assert len(server.requests) == 1
    assert server.requests[0]["headers"]["Authorization"] == "Bearer worker-secret"
    if follow:
        # The redirect really was followed, but the key stayed with the configured origin.
        assert len(other_host.requests) == 1
        assert "authorization" not in {name.lower() for name in other_host.requests[0]["headers"]}
    else:
        assert other_host.requests == []
    assert result.report.status != "complete"
    assert "One or more workers did not complete." in result.report.unresolved_items


class ToFakeServer(httpx2.AsyncBaseTransport):
    """Deliver every request to the fake server, keeping the URL the client built.

    The worker hook runs before the transport, so it sees the configured
    host-only URL; only the socket address changes.
    """

    def __init__(self, netloc: str):
        self.netloc = netloc.encode("ascii")
        self.inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        request.url = request.url.copy_with(netloc=self.netloc)
        return await self.inner.handle_async_request(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    ["http://worker.test/v1", "http://worker.test:80/v1", "HTTP://Worker.Test:80/v1/"],
)
async def test_e2_bearer_auth_reaches_the_configured_origin_however_its_default_port_is_written(
    tmp_path, monkeypatch, base_url
):
    import agent_team.models.worker_model as worker_model

    monkeypatch.setenv("LLM_API_KEY", "worker-secret")
    real = worker_model.openai

    with FakeOpenAIServer(lambda payload: native_call("call_final", "final_result", COMPLETE_OUTPUT)) as server:
        def via_fake_server(**kwargs):
            return real.DefaultAsyncHttpxClient(**kwargs, transport=ToFakeServer(server.netloc))

        monkeypatch.setattr(
            worker_model, "openai",
            SimpleNamespace(DefaultAsyncHttpxClient=via_fake_server, AsyncOpenAI=real.AsyncOpenAI),
        )
        engine = delegated_engine(tmp_path, base_url, ["read_file"])
        try:
            result = await engine.run_delegated_brief(DELEGATED_BRIEF, session_id="origin")
        finally:
            await engine.close()

    assert server.errors == []
    assert len(server.requests) == 1
    assert server.requests[0]["headers"]["Authorization"] == "Bearer worker-secret"
    assert result.report.acceptance_results[0].status == "passed"
