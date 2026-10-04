# LangGraph swarm example

[langgraph_swarm.py](langgraph_swarm.py) implements the delegated path with a
real LangGraph graph:

```text
START → Principal → Manager → Worker → END
```

From the repository root, install the locked development dependencies and run:

```sh
uv sync --frozen
uv run --frozen python -m examples.langgraph_swarm "one two three"
```

The command prints a JSON `WorkerResult` with `"summary": "Word count: 3"` and
requirement evidence. It counts whitespace-separated words; punctuation stays
attached to each word. Empty or whitespace-only text is rejected. No model, API
key, network request, or Agent Team runtime startup is needed. LangGraph belongs
to the development dependency group; this example is run from the checkout and
is not installed with the Agent Team wheel.

The Principal creates the repository's `ProjectBrief`; the Manager translates
it into a `WorkerTaskSpec`; the Worker returns a `WorkerResult` with requirement
IDs and evidence. Pydantic validates each node's incoming handoff. The graph
accepts only the text channel, so callers cannot seed the brief or worker task.
You can inspect the typed handoffs through its public result:

```python
from examples.langgraph_swarm import graph

output = graph.invoke({"text": "typed handoffs"})
print(output["brief"].objective)
print(output["task"].requirement_ids)
print(output["result"].summary)  # Word count: 2
```

Node input schemas describe which state each node reads. They do not provide a
security boundary or process confinement. This teaching example omits production
persistence, scheduling, tools, and inference.

Run the required behavioral tests with:

```sh
uv run --frozen python scripts/run_isolated_tests.py tests/test_langgraph_example.py
```

The production coverage ratchet measures `agent_team`. Measure this example
separately; a pytest-only run excludes its CLI subprocess unless subprocess
coverage is explicitly enabled.
