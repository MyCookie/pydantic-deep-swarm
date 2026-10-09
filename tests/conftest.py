"""Require native platform proofs on their host and identify foreign exclusions."""
import sys
from pathlib import Path
import runpy

import pytest

NATIVE_PROOFS = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/platform_proofs.py")
)["NATIVE_PROOFS"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "required_platform(platform): native proof required on this host platform"
    )


def pytest_collection_modifyitems(items):
    for item in items:
        marker = item.get_closest_marker("required_platform")
        if (marker is not None and NATIVE_PROOFS.get(item.nodeid) == marker.args[0]
                and marker.args[0] != sys.platform):
            item.add_marker(pytest.mark.skip(reason=f"native proof requires {marker.args[0]}"))


@pytest.fixture
def offered_tool_names():
    """Return an async helper naming the tools the worker loop offers its model."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from agent_team.worker_tools import ToolAwareWorkerAgent

    async def offered(executor) -> list[str]:
        seen: list[str] = []

        def respond(messages, info):
            seen.extend(sorted(tool.name for tool in info.function_tools))
            return ModelResponse(parts=[TextPart("done")])

        await ToolAwareWorkerAgent(FunctionModel(respond), "worker", executor).run("list the tools")
        return seen

    return offered
