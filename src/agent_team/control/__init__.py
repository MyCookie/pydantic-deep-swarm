"""Programmatic control plane for the Agent Team swarm."""

from .api import AgentTeamAPIClient, AgentTeamAPIError
from .configuration import (
    SwarmConfigReconciler,
    ConfigReconcileError,
    extract_config_model,
    extract_runfile_model,
)
from .discovery import ModelDiscoveryError, OpenAIModelDiscovery, VLLMModelDiscovery
from .manager import SwarmManager, SwarmReconcileResult, SwarmStatus
from .service import S6ServiceController, ServiceControlError

__all__ = [
    "AgentTeamAPIClient",
    "AgentTeamAPIError",
    "ConfigReconcileError",
    "SwarmConfigReconciler",
    "ModelDiscoveryError",
    "OpenAIModelDiscovery",
    "VLLMModelDiscovery",
    "SwarmManager",
    "SwarmReconcileResult",
    "SwarmStatus",
    "S6ServiceController",
    "ServiceControlError",
    "extract_config_model",
    "extract_runfile_model",
]
