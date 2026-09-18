"""HTTP client for the Agent Team runtime's control endpoints."""

from __future__ import annotations

import os
from typing import Any

import httpx


class AgentTeamAPIError(RuntimeError):
    """Raised when the Agent Team API cannot be inspected."""


class AgentTeamAPIClient:
    """Small synchronous client used by CLI and reconciliation tooling."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 5.0,
        api_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("AGENT_TEAM_URL", "http://localhost:8080")).rstrip("/")
        self._api_token = (
            os.getenv("AGENT_TEAM_API_TOKEN", "")
            if api_token is None
            else api_token
        )
        self._client = client or httpx.Client(timeout=timeout)

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _request(self, method: str, path: str) -> dict[str, Any]:
        try:
            headers = (
                {"Authorization": f"Bearer {self._api_token}"}
                if self._api_token
                else None
            )
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentTeamAPIError(
                f"{method} {self.base_url}{path} failed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise AgentTeamAPIError(
                f"{method} {self.base_url}{path} returned a non-object JSON value"
            )
        return payload

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def ready(self) -> dict[str, Any]:
        return self._get("/ready")

    def models(self) -> dict[str, Any]:
        payload = self._get("/models")
        models = payload.get("models")
        if not isinstance(models, dict):
            raise AgentTeamAPIError("Agent Team /models response has no object-valued 'models'")
        return models

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/cancel")
