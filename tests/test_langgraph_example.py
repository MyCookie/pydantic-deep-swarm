"""Exercise the real example at graph invocation and its module CLI."""

import json
from pathlib import Path
import subprocess
import sys
import runpy

import pytest
from pydantic import ValidationError

from agent_team.contracts import ProjectBrief, WorkerTaskSpec, WorkerResult

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("text, count", [
    ("typed handoffs", 2),
    ("  one\ttwo\nthree  ", 3),
    ("hello, world!", 2),
])
def test_graph_returns_typed_handoffs_and_evidence_for_supplied_text(text, count):
    graph = runpy.run_path(str(ROOT / "examples/langgraph_swarm.py"))["graph"]
    output = graph.invoke({"text": text, "task": {"objective": "injected"}})

    assert isinstance(output["brief"], ProjectBrief)
    assert isinstance(output["task"], WorkerTaskSpec)
    assert isinstance(output["result"], WorkerResult)
    assert output["brief"].relevant_context == [text]
    assert output["task"].relevant_context == [text]
    assert output["task"].requirement_ids == ["word-count"]
    assert output["task"].tools == []
    assert output["result"].model_dump(include={
        "task_id", "role", "status", "summary", "requirement_results", "evidence",
    }) == {
        "task_id": "count-words",
        "role": "word-counter",
        "status": "complete",
        "summary": f"Word count: {count}",
        "requirement_results": [{
            "requirement_id": "word-count", "status": "satisfied",
            "evidence": [f"Counted {count} whitespace-separated words."], "notes": None,
        }],
        "evidence": [f"Counted {count} whitespace-separated words."],
    }


@pytest.mark.parametrize("payload", [
    {"text": ""}, {"text": " \t\n"}, {}, {"text": None}, {"text": 42},
])
def test_graph_rejects_empty_or_malformed_user_text(payload):
    graph = runpy.run_path(str(ROOT / "examples/langgraph_swarm.py"))["graph"]

    with pytest.raises(ValidationError):
        graph.invoke(payload)


def test_module_cli_prints_the_typed_worker_result_as_json():
    completed = subprocess.run(
        [sys.executable, "-m", "examples.langgraph_swarm", "one two three"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=20,
    )

    result = WorkerResult.model_validate(json.loads(completed.stdout))
    assert (result.status, result.summary, result.task_id) == (
        "complete", "Word count: 3", "count-words",
    )
