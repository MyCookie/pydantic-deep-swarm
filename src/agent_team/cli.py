"""CLI for the Agent Team runtime and swarm control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from .bootstrap import BootstrapError, Bootstrapper, write_bootstrap_report
from .config import get_config
from .control import (
    AgentTeamAPIClient,
    S6ServiceController,
    SwarmConfigReconciler,
    SwarmManager,
    OpenAIModelDiscovery,
)
from .observability import get_logger
from .pi_reconciler import PiAssetReconciler, PiReconcileError
from .provenance import (
    AssetProvenance,
    GeneratedBy,
    ProvenanceValidationError,
    ValidationResult,
    build_provenance_record,
    validate_provenance,
    write_provenance,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_swarm_manager() -> SwarmManager:
    """Build the default control plane; dependency injection remains available in tests."""
    root = Path(os.getenv("AGENT_TEAM_PROJECT_ROOT", str(_project_root())))
    model_url = os.getenv("LLM_BASE_URL", "http://model-service:8000/v1")
    api_url = (
        os.getenv("AGENT_TEAM_API_URL")
        or os.getenv("AGENT_TEAM_URL")
        or "http://localhost:8080"
    )
    source_config = Path(
        os.getenv("AGENT_TEAM_CONFIG_SOURCE", str(root / "src/agent_team/config.py"))
    )
    source_runfile = Path(
        os.getenv(
            "AGENT_TEAM_SOURCE_RUNFILE",
            str(root / "s6-service/agent-team/run"),
        )
    )
    configured_service_dir = os.getenv("AGENT_TEAM_SERVICE_DIR")
    if not configured_service_dir:
        raise ValueError("set AGENT_TEAM_SERVICE_DIR for the live s6 service")
    service_dir = Path(configured_service_dir)
    live_runfile = Path(
        os.getenv("AGENT_TEAM_LIVE_RUNFILE", str(service_dir / "run"))
    )
    return SwarmManager(
        OpenAIModelDiscovery(model_url, api_key=os.getenv("LLM_API_KEY", "")),
        AgentTeamAPIClient(
            api_url,
            api_token=os.getenv("AGENT_TEAM_API_TOKEN", ""),
        ),
        SwarmConfigReconciler(source_config, source_runfile, live_runfile),
        S6ServiceController(service_dir=service_dir),
    )


@click.group()
@click.version_option()
def cli():
    """Agent Team Runtime CLI."""
    pass


@cli.group()
def swarm():
    """Detect, reconcile, and verify the running agent swarm."""
    pass


@swarm.command("detect")
def swarm_detect():
    """Print the single model advertised by the configured endpoint."""
    try:
        click.echo(build_swarm_manager().discovery.detect_model())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@swarm.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def swarm_status(as_json: bool):
    """Inspect model drift across the endpoint, deployment, and API."""
    try:
        status = build_swarm_manager().inspect()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(status.model_dump_json(indent=2))
    else:
        click.echo(f"expected model: {status.expected_model}")
        click.echo(f"endpoint models: {', '.join(status.advertised_models) or '(none)'}")
        click.echo(f"Python config: {status.config_model}")
        click.echo(f"source runfile: {status.source_model}")
        click.echo(f"live runfile: {status.live_model}")
        click.echo(f"health ready: {status.health_ok}")
        click.echo(f"API ready: {status.ready_ok}")
        if status.drift:
            click.echo("drift:")
            for item in status.drift:
                click.echo(f"  - {item}")
        else:
            click.echo("drift: none")
    if not status.synchronized:
        raise click.exceptions.Exit(1)


@swarm.command("reconcile")
@click.option("--model", default=None, help="Explicit model; defaults to endpoint auto-detection.")
@click.option("--dry-run", is_flag=True, help="Plan changes without writing or restarting.")
@click.option("--no-restart", is_flag=True, help="Do not restart the supervised service.")
@click.option("--no-verify", is_flag=True, help="Skip post-restart verification.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def swarm_reconcile(
    model: str | None,
    dry_run: bool,
    no_restart: bool,
    no_verify: bool,
    as_json: bool,
):
    """Auto-detect, reconcile, restart, and verify the swarm."""
    try:
        result = build_swarm_manager().reconcile(
            model,
            restart=not no_restart,
            verify=not no_verify,
            dry_run=dry_run,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(result.model_dump_json(indent=2))
    else:
        click.echo(f"model: {result.model}")
        click.echo(f"changed files: {len(result.changed_files)}")
        click.echo(f"restarted: {result.restarted}")
        click.echo(f"verified: {result.verified}")
        if result.status.drift:
            click.echo("drift:")
            for item in result.status.drift:
                click.echo(f"  - {item}")
    if not dry_run and not no_verify and not result.verified:
        raise click.exceptions.Exit(1)


@cli.group()
def provenance():
    """Capture and validate non-mutating Git provenance records."""
    pass


def _parse_validation_spec(spec: str) -> ValidationResult:
    name, separator, status = spec.rpartition("=")
    if not separator or not name.strip() or status not in {"passed", "failed", "skipped"}:
        raise click.BadParameter(
            "expected NAME=passed|failed|skipped",
            param_hint="--validation",
        )
    return ValidationResult(name=name, status=status)


def _load_validation_evidence(paths: tuple[Path, ...]) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            entries = value if isinstance(value, list) else [value]
            if not entries or any(not isinstance(entry, dict) for entry in entries):
                raise ValueError("expected one validation object or a non-empty array")
            results.extend(ValidationResult.model_validate(entry) for entry in entries)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise click.BadParameter(
                f"invalid validation evidence file {path}: {exc}",
                param_hint="--validation-evidence",
            ) from exc
    return results


def _parse_asset_spec(spec: str) -> AssetProvenance:
    path, separator, digest = spec.rpartition("=")
    if not separator:
        path, digest = spec, None
    if not path.strip():
        raise click.BadParameter("expected PATH or PATH=SHA256", param_hint="--asset")
    try:
        return AssetProvenance(path=path, sha256=digest or None)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--asset") from exc


@provenance.command("capture")
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--record-id", required=True)
@click.option("--project-id", default=None)
@click.option("--agent", required=True)
@click.option("--model", default=None)
@click.option("--session-id", default=None)
@click.option("--task-id", default=None)
@click.option("--base-revision", default=None)
@click.option("--candidate-revision", default=None)
@click.option("--deployed-revision", default=None)
@click.option(
    "--validation",
    multiple=True,
    help="Validation observation in NAME=passed|failed|skipped form; repeatable.",
)
@click.option(
    "--validation-evidence",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON file containing complete ValidationResult evidence; repeatable.",
)
@click.option(
    "--asset",
    multiple=True,
    help="Generated asset in PATH or PATH=SHA256 form; repeatable.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the complete JSON record.")
def provenance_capture(
    repo_root: Path,
    output: Path,
    record_id: str,
    project_id: str | None,
    agent: str,
    model: str | None,
    session_id: str | None,
    task_id: str | None,
    base_revision: str | None,
    candidate_revision: str | None,
    deployed_revision: str | None,
    validation: tuple[str, ...],
    validation_evidence: tuple[Path, ...],
    asset: tuple[str, ...],
    as_json: bool,
):
    """Capture Git state and write one provenance record atomically."""
    try:
        record = build_provenance_record(
            repo_root,
            record_id=record_id,
            project_id=project_id,
            generated_by=GeneratedBy(
                agent=agent,
                model=model,
                session_id=session_id,
                task_id=task_id,
                tool="agent-team provenance capture",
            ),
            base_revision=base_revision,
            candidate_revision=candidate_revision,
            deployed_revision=deployed_revision,
            validation_results=[
                *[_parse_validation_spec(item) for item in validation],
                *_load_validation_evidence(validation_evidence),
            ],
            assets=[_parse_asset_spec(item) for item in asset],
        )
        write_provenance(output, record)
    except (ProvenanceValidationError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(record.model_dump_json(indent=2))
    else:
        click.echo(f"wrote provenance: {output.expanduser()}")
        click.echo(f"candidate revision: {record.candidate_revision}")
        click.echo("remote push: disabled unless explicitly authorized outside the recorder")


@provenance.command("validate")
@click.argument("record", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optionally verify the record belongs to this repository.",
)
@click.option(
    "--check-candidate",
    is_flag=True,
    help="For worktree candidates, require the current fingerprint to match.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the complete JSON record.")
def provenance_validate(record: Path, repo_root: Path | None, check_candidate: bool, as_json: bool):
    """Validate a provenance JSON record without changing Git state."""
    try:
        loaded = validate_provenance(
            record,
            repository=repo_root,
            check_candidate=check_candidate,
        )
    except (ProvenanceValidationError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(loaded.model_dump_json(indent=2))
    else:
        click.echo(f"valid provenance: {record.expanduser()}")
        click.echo(f"record: {loaded.record_id}")
        click.echo(f"candidate revision: {loaded.candidate_revision}")
        click.echo(f"deployed revision: {loaded.deployed_revision or '(not recorded)'}")
        click.echo(f"validations: {len(loaded.validation_results)}")
        click.echo("remote push: disabled unless explicitly authorized outside the recorder")


@cli.command("bootstrap")
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@click.option("--pi-binary", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--python", "python_executable", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--model-endpoint", default=None)
@click.option("--config", "config_file", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--state-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--service-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--s6-svstat", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option(
    "--ready-url",
    "--health-url",
    "health_url",
    default=None,
    help="Agent Team readiness URL; the legacy --health-url alias is accepted.",
)
@click.option("--expected-revision", default=None)
@click.option("--report", "report_path", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def bootstrap_command(
    repo_root: Path,
    pi_binary: Path,
    python_executable: Path | None,
    model_endpoint: str | None,
    config_file: Path | None,
    state_dir: Path | None,
    service_dir: Path | None,
    s6_svstat: Path | None,
    health_url: str | None,
    expected_revision: str | None,
    report_path: Path | None,
    as_json: bool,
):
    """Verify a cloned installation without installing tools or packages."""
    try:
        report = Bootstrapper(
            repo_root,
            pi_binary,
            python_executable=python_executable,
            model_endpoint=model_endpoint,
            config_file=config_file,
            state_dir=state_dir,
            service_dir=service_dir,
            s6_svstat=s6_svstat,
            health_url=health_url,
        ).run(expected_revision=expected_revision)
        if report_path is not None:
            write_bootstrap_report(report_path, report)
    except BootstrapError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(report.model_dump_json(indent=2))
    else:
        click.echo(f"repository: {report.repository_root}")
        click.echo(f"revision: {report.revision or '(none)'}")
        click.echo(f"Pi: {report.pi_version or '(unavailable)'}")
        click.echo(f"Python: {report.python_version or '(unavailable)'}")
        click.echo(f"model endpoint: {report.model_endpoint}")
        click.echo(f"state directory: {report.state_dir}")
        for check in report.checks:
            click.echo(f"{check.status}: {check.name} — {check.detail}")
        if report.missing_external_tools:
            click.echo("missing external tools: " + ", ".join(report.missing_external_tools))
        click.echo(f"ready: {report.ready}")
        if report_path is not None:
            click.echo(f"report: {report_path.expanduser()}")
    if not report.ready:
        raise click.exceptions.Exit(1)


@cli.group("pi")
def pi_commands():
    """Reconcile first-party Pi assets from trusted Git revisions."""
    pass


def _make_pi_reconciler(
    repo_root: Path,
    pi_binary: Path,
    scope: str,
    user_home: Path | None,
) -> PiAssetReconciler:
    return PiAssetReconciler(
        repo_root,
        pi_binary,
        scope=scope,
        user_home=user_home,
    )


def _print_pi_result(result) -> None:
    click.echo(f"revision: {result.revision}")
    click.echo(f"scope: {result.scope}")
    click.echo(f"package path: {result.package_path}")
    click.echo(f"changed: {result.changed}")
    click.echo(f"verified: {result.verified}")
    if result.previous_revision:
        click.echo(f"previous revision: {result.previous_revision}")
    if result.pi_version:
        click.echo(f"Pi version: {result.pi_version}")
    if result.drift:
        click.echo("drift:")
        for item in result.drift:
            click.echo(f"  - {item}")


@pi_commands.command("reconcile")
@click.argument("revision")
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@click.option("--pi-binary", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--scope",
    type=click.Choice(["project-local", "user-global"]),
    default="project-local",
    show_default=True,
)
@click.option("--user-home", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--approve", "approve_project", is_flag=True, help="Approve project-local Pi loading.")
@click.option("--dry-run", is_flag=True, help="Validate the revision without installing it.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def pi_reconcile(
    revision: str,
    repo_root: Path,
    pi_binary: Path,
    scope: str,
    user_home: Path | None,
    approve_project: bool,
    dry_run: bool,
    as_json: bool,
):
    """Reconcile REVISION, which must be a full trusted commit ID."""
    try:
        result = _make_pi_reconciler(repo_root, pi_binary, scope, user_home).reconcile(
            revision,
            approve_project=approve_project,
            dry_run=dry_run,
        )
    except PiReconcileError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(result.model_dump_json(indent=2))
    else:
        _print_pi_result(result)
    if not dry_run and not result.verified:
        raise click.exceptions.Exit(1)


@pi_commands.command("status")
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@click.option("--pi-binary", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--scope",
    type=click.Choice(["project-local", "user-global"]),
    default="project-local",
    show_default=True,
)
@click.option("--user-home", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def pi_status(
    repo_root: Path,
    pi_binary: Path,
    scope: str,
    user_home: Path | None,
    as_json: bool,
):
    """Report installed Pi state and owned-resource drift."""
    try:
        status = _make_pi_reconciler(repo_root, pi_binary, scope, user_home).status()
    except PiReconcileError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(status.model_dump_json(indent=2))
    else:
        click.echo(f"scope: {status.scope}")
        click.echo(f"settings path: {status.settings_path}")
        click.echo(f"managed root: {status.managed_root}")
        click.echo(f"installed revision: {status.installed_revision or '(none)'}")
        if status.drift:
            click.echo("drift:")
            for item in status.drift:
                click.echo(f"  - {item}")
        else:
            click.echo("drift: none")
    if status.drift:
        raise click.exceptions.Exit(1)


@pi_commands.command("rollback")
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@click.option("--pi-binary", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--scope",
    type=click.Choice(["project-local", "user-global"]),
    default="project-local",
    show_default=True,
)
@click.option("--user-home", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--approve", "approve_project", is_flag=True, help="Approve project-local Pi loading.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def pi_rollback(
    repo_root: Path,
    pi_binary: Path,
    scope: str,
    user_home: Path | None,
    approve_project: bool,
    as_json: bool,
):
    """Roll back to the previous known-good Pi revision."""
    try:
        result = _make_pi_reconciler(repo_root, pi_binary, scope, user_home).rollback(
            approve_project=approve_project,
        )
    except PiReconcileError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(result.model_dump_json(indent=2))
    else:
        _print_pi_result(result)
    if not result.verified:
        raise click.exceptions.Exit(1)


@cli.command()
def chat():
    """Start an interactive chat session."""
    config = get_config()
    logger = get_logger("agent-team-cli")

    click.echo("Agent Team Interactive Chat")
    click.echo(f"State directory: {config.runtime.state_dir}")
    click.echo("Type 'quit' or 'exit' to end the session\n")
    click.echo("Interactive chat not yet implemented.")
    click.echo("Use the HTTP API directly: POST /sessions")


@cli.command()
def status():
    """Check service status."""
    try:
        data = AgentTeamAPIClient().health()
        click.echo(f"Health: {data['status']}")
    except Exception as e:
        click.echo(f"Service not running: {e}")


@cli.command()
@click.argument("session_id")
def cancel(session_id: str):
    """Cancel work running in one Agent Team session."""
    try:
        data = AgentTeamAPIClient().cancel_session(session_id)
        click.echo(f"Session {session_id} cancelled: {data}")
    except Exception as e:
        click.echo(f"Failed to cancel session: {e}")


@cli.command()
def init():
    """Initialize agent team directories and config."""
    config = get_config()

    dirs = [
        config.runtime.state_dir,
        config.runtime.state_dir / "config",
        config.runtime.state_dir / "sessions",
        config.runtime.state_dir / "checkpoints",
        config.runtime.state_dir / "memory",
        config.runtime.state_dir / "knowledge",
        config.runtime.state_dir / "skills",
        config.runtime.state_dir / "projects",
        config.runtime.state_dir / "artifacts",
        config.runtime.state_dir / "logs",
    ]

    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
        click.echo(f"Created: {directory}")

    config_file = config.runtime.state_dir / "config" / "config.yaml"
    if not config_file.exists():
        config_content = """# Agent Team Configuration
runtime:
  state_dir: ~/.agent-team
  max_workers: 6
  max_concurrent_workers: 3
  nesting_depth: 0

models:
  principal:
    model: ${PRINCIPAL_MODEL}
    base_url: ${PRINCIPAL_BASE_URL:-${LLM_BASE_URL}}
  manager:
    model: ${MANAGER_MODEL}
    base_url: ${MANAGER_BASE_URL:-${LLM_BASE_URL}}
  worker:
    model: ${WORKER_MODEL}
    base_url: ${WORKER_BASE_URL:-${LLM_BASE_URL}}
  curator:
    model: ${CURATOR_MODEL:-${WORKER_MODEL}}
    base_url: ${CURATOR_BASE_URL:-${LLM_BASE_URL}}

memory:
  enabled: true
  shared_knowledge: true
  curator_enabled: false

tools:
  skills: false
  mcp: false

retention:
  max_session_messages: 200
  session_max_age_days: null
  project_max_age_days: null
  knowledge_max_age_days: null
  memory_max_age_days: null
  log_max_bytes: null
  log_backup_count: 5
"""
        config_file.write_text(config_content)
        click.echo(f"Created default config: {config_file}")

    click.echo("\nInitialization complete!")
    click.echo("\nSet environment variables:")
    click.echo("  export LLM_BASE_URL=${LLM_BASE_URL:?set LLM_BASE_URL to the OpenAI-compatible URL}")
    click.echo('  export LLM_API_KEY=""  # optional; only needed if the model service requires auth')
    click.echo('  export AGENT_TEAM_API_TOKEN=""  # optional; enables Agent Team HTTP Bearer auth')
    click.echo("  export PRINCIPAL_MODEL=model-name")
    click.echo("  export MANAGER_MODEL=model-name")
    click.echo("  export WORKER_MODEL=model-name")


def main() -> None:
    """Console-script entrypoint."""
    cli()


if __name__ == "__main__":
    main()
