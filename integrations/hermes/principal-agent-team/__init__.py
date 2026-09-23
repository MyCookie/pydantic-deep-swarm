"""Optional session-aware Agent Team typed delegation adapter.

The plugin does not intercept gateway dispatch or bypass host authorization. It
registers one session-aware tool that sends a typed ProjectBrief directly to the
Agent Team Manager/workers endpoint, avoiding a second Principal intake. The
invoking application's identity and user-facing routing remain outside the
Agent Team architecture boundary.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx


# Retained as configuration identity for existing adapter deployments.
PRINCIPAL_ROOM_ID = os.getenv("AGENT_TEAM_PRINCIPAL_ROOM_ID", "")
DEFAULT_AGENT_TEAM_URL = "http://127.0.0.1:8080"
logger = logging.getLogger("hermes.principal-agent-team")

WORKER_EXECUTION_BOUNDARY = (
    "Agent Team workers have no terminal or Hermes-native tool bridge. "
    "The Hermes Principal must execute GitHub CLI operations before or after delegation and pass "
    "the resulting data through relevant_context."
)


PROJECT_BRIEF_SCHEMA = {
    "name": "delegate_to_agent_team",
    "description": (
        "Delegate an actionable project through the typed Principal-authored ProjectBrief. "
        "Use this for substantial work after collecting the objective, requirements, "
        "constraints, acceptance criteria, permitted/prohibited actions, and desired output. "
        "The Agent Team returns only operational status, a concise summary, verified artifacts, "
        "and unresolved items; it does not run a second Principal intake."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "objective": {"type": "string", "description": "The user-approved project objective."},
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "required": {"type": "boolean"},
                    },
                    "required": ["id", "description"],
                },
            },
            "constraints": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "verification_method": {"type": ["string", "null"]},
                    },
                    "required": ["id", "description"],
                },
            },
            "relevant_context": {"type": "array", "items": {"type": "string"}},
            "context_refs": {"type": "array", "items": {"type": "string"}},
            "permitted_actions": {"type": "array", "items": {"type": "string"}},
            "prohibited_actions": {"type": "array", "items": {"type": "string"}},
            "desired_output": {"type": "string"},
            "unresolved_ambiguities": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["objective", "desired_output"],
    },
}


def _resolve_github_cli_path(configured_path: str = "") -> str:
    """Return an executable ``gh`` path without relying only on the process PATH."""
    candidates: list[str] = []
    if configured_path.strip():
        candidates.append(configured_path.strip())
    if discovered := shutil.which("gh"):
        candidates.append(discovered)
    if homebrew_prefix := os.getenv("HOMEBREW_PREFIX", "").strip():
        candidates.append(str(Path(homebrew_prefix).expanduser() / "bin" / "gh"))
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    return ""


def _github_cli_guidance(github_cli_path: str) -> str:
    guidance = WORKER_EXECUTION_BOUNDARY
    if github_cli_path:
        guidance += (
            f" The GitHub CLI resolved to {github_cli_path}; plain gh may be absent from PATH, "
            "so invoke this exact absolute path."
        )
    return guidance


def _prepare_brief(params: dict[str, Any], github_cli_path: str) -> dict[str, Any]:
    """Copy a brief and make the Principal/worker execution boundary explicit."""
    brief = deepcopy(params)
    relevant_context = brief.get("relevant_context")
    if not isinstance(relevant_context, list):
        relevant_context = []
    guidance = _github_cli_guidance(github_cli_path)
    if guidance not in relevant_context:
        relevant_context.append(guidance)
    brief["relevant_context"] = relevant_context
    return brief


def should_route(platform: Any, room_id: Any) -> bool:
    """Compatibility helper; no gateway hook uses this function anymore."""
    platform_name = getattr(platform, "value", platform)
    return str(platform_name or "").lower() == "matrix" and str(room_id or "") == PRINCIPAL_ROOM_ID


async def _post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    api_token: str = "",
) -> dict[str, Any]:
    timeout = httpx.Timeout(connect=5.0, read=1800.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
        response = await client.post(path, json=payload, headers=headers)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Agent Team returned a non-object response")
        return value


def _compact_result(result: dict[str, Any]) -> str:
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    artifacts = [
        {
            "path": item.get("path"),
            "description": item.get("description"),
            "sha256": item.get("sha256"),
            "size_bytes": item.get("size_bytes"),
            "verified": True,
            "verification_method": item.get("verification_method"),
        }
        for item in report.get("artifacts", [])
        if isinstance(item, dict) and item.get("verified") is True
    ]
    payload = {
        "status": result.get("status") or report.get("status") or "failed",
        "project_id": result.get("project_id"),
        "summary": report.get("summary") or result.get("response") or "No summary returned.",
        "artifacts": artifacts,
        "unresolved_items": list(report.get("unresolved_items") or [])[:8],
    }
    return json.dumps(payload, ensure_ascii=False)


async def _delegate(
    params: dict[str, Any],
    base_url: str,
    *,
    session_id: str | None,
    api_token: str = "",
    github_cli_path: str = "",
) -> str:
    """Validate the typed handoff and call the direct delegation endpoint."""
    if not session_id:
        return json.dumps({
            "status": "failed",
            "summary": "No Hermes session id was supplied; delegation was not started.",
            "artifacts": [],
            "unresolved_items": ["Missing session_id"],
        })
    if not isinstance(params, dict) or not str(params.get("objective") or "").strip():
        return json.dumps({
            "status": "failed",
            "summary": "The delegation brief requires a non-empty objective.",
            "artifacts": [],
            "unresolved_items": ["Invalid ProjectBrief"],
        })
    if not str(params.get("desired_output") or "").strip():
        return json.dumps({
            "status": "failed",
            "summary": "The delegation brief requires a desired_output.",
            "artifacts": [],
            "unresolved_items": ["Invalid ProjectBrief"],
        })

    client_key = f"hermes:{session_id}"
    session = await _post(
        base_url,
        "/sessions",
        {
            "client_key": client_key,
            "metadata": {"hermes_session_id": session_id, "integration": "principal-tool"},
        },
        api_token=api_token,
    )
    agent_session_id = session.get("session_id")
    if not agent_session_id:
        raise RuntimeError("Agent Team did not return a session id")
    result = await _post(
        base_url,
        f"/sessions/{agent_session_id}/delegations",
        {"brief": _prepare_brief(params, github_cli_path)},
        api_token=api_token,
    )
    return _compact_result(result)


def register(ctx: Any) -> None:
    """Register the typed delegation tool; do not register gateway interception."""
    configured_url = ctx.get_config("agent_team_url", None)
    base_url = str(configured_url or os.getenv("AGENT_TEAM_URL") or DEFAULT_AGENT_TEAM_URL).strip()
    api_token = os.getenv("AGENT_TEAM_API_TOKEN", "")
    github_cli_path = _resolve_github_cli_path(str(ctx.get_config("github_cli_path", "") or ""))
    tool_schema = deepcopy(PROJECT_BRIEF_SCHEMA)
    tool_schema["description"] = f"{tool_schema['description']} {_github_cli_guidance(github_cli_path)}"

    async def handle_delegate(params: dict[str, Any], **kwargs: Any) -> str:
        return await _delegate(
            params,
            base_url,
            session_id=kwargs.get("session_id"),
            api_token=api_token,
            github_cli_path=github_cli_path,
        )

    ctx.register_tool(
        name="delegate_to_agent_team",
        toolset="agent_team",
        schema=tool_schema,
        handler=handle_delegate,
        is_async=True,
        description=(
            "Submit a typed Principal-authored brief directly to the Agent Team Manager/workers. "
            + _github_cli_guidance(github_cli_path)
        ),
    )
