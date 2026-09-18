"""Agent Team Runtime - Multi-agent orchestration."""

__version__ = "0.1.0"

from .bootstrap import BootstrapCheck, BootstrapError, BootstrapReport, Bootstrapper, write_bootstrap_report
from .config import Config, get_config
from .contracts import ProjectBrief, CompletionReport
from .observability import get_logger
from .pi_reconciler import PiAssetReconciler, PiReconcileError, PiReconcileResult, PiStatus
from .worker_factory import (
    AgentRole,
    create_worker_agent,
    get_available_roles,
    get_role_by_name,
    suggest_roles_for_task,
    PROFESSIONAL_ROLES,
)

__all__ = [
    "Config",
    "get_config",
    "BootstrapCheck",
    "BootstrapError",
    "BootstrapReport",
    "Bootstrapper",
    "write_bootstrap_report",
    "ProjectBrief",
    "CompletionReport",
    "get_logger",
    "PiAssetReconciler",
    "PiReconcileError",
    "PiReconcileResult",
    "PiStatus",
    "AgentRole",
    "create_worker_agent",
    "get_available_roles",
    "get_role_by_name",
    "suggest_roles_for_task",
    "PROFESSIONAL_ROLES",
]
