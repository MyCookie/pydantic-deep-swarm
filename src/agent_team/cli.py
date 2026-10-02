"""CLI for the Agent Team runtime and swarm control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import yaml
from .redaction import redact_sensitive_data, redact_sensitive_text

from .bootstrap import BootstrapError, Bootstrapper, write_bootstrap_report
from .config import get_config, resolve_configuration
from .control import (
    AgentTeamAPIClient,
    S6ServiceController,
    SwarmConfigReconciler,
    SwarmManager,
    OpenAIModelDiscovery,
)
from .observability import get_logger
from .control.configuration import extract_runfile_model
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


def build_model_discovery(**selectors) -> OpenAIModelDiscovery:
    selection = resolve_configuration(**selectors)
    path = selection.config_file
    data = yaml.safe_load(path.read_text()) if selection.config_source == "yaml" else {}
    default = (data or {}).get("models", {}).get("default") or {}
    endpoint = default.get("base_url") if selection.config_source == "yaml" else os.getenv("LLM_BASE_URL")
    if endpoint is not None:
        from .config import Config
        endpoint = Config._expand_env_vars(endpoint)
    if selection.config_source == "yaml":
        endpoint = endpoint or selection.config.models["principal"].base_url
    if not endpoint:
        raise ValueError("common discovery endpoint is required")
    return OpenAIModelDiscovery(endpoint)


def build_swarm_manager(*, supervisor: str | None = None, **selectors):
    from .control.manager import OwnedSwarmManager
    selection = resolve_configuration(**selectors)
    selected = supervisor or os.getenv("AGENT_TEAM_SUPERVISOR", "none")
    if selected not in {"none", "s6"}:
        raise ValueError("supervisor must be none or s6")
    service = None
    if selected == "s6":
        directory = os.getenv("AGENT_TEAM_SERVICE_DIR")
        if not directory:
            raise ValueError("AGENT_TEAM_SERVICE_DIR is required for the s6 adapter")
        service = S6ServiceController(service_dir=Path(directory))
    api_url = os.getenv("AGENT_TEAM_API_URL") or os.getenv("AGENT_TEAM_URL") or "http://localhost:8080"
    return OwnedSwarmManager(selection, AgentTeamAPIClient(api_url,api_token=os.getenv("AGENT_TEAM_API_TOKEN", "")), service,
                             discovery_factory=OpenAIModelDiscovery,
                             state_dir=selectors.get("state_dir"),workspace_dir=selectors.get("workspace_dir"))


def config_options(function):
    function = click.option("--workspace-dir",type=click.Path(path_type=Path))(function)
    function = click.option("--state-dir",type=click.Path(path_type=Path))(function)
    return click.option("--config","config_file",type=click.Path(path_type=Path))(function)


def _port_argument(value):
    try:
        return int(value)
    except (TypeError,ValueError):
        return value


def _deadline_argument(value):
    try:
        return float(value)
    except (TypeError,ValueError):
        return value


_PORT_TYPE=click.types.FuncParamType(_port_argument)
_PORT_TYPE.name="integer"
_DEADLINE_TYPE=click.types.FuncParamType(_deadline_argument)
_DEADLINE_TYPE.name="seconds"


@click.group()
@click.version_option()
def cli():
    """Agent Team Runtime CLI."""
    pass


@cli.command("serve")
@config_options
@click.option("--host", envvar="AGENT_TEAM_HOST",default="localhost")
@click.option("--port",envvar="AGENT_TEAM_PORT",default=8080,type=_PORT_TYPE)
@click.option("--startup-timeout",envvar="AGENT_TEAM_STARTUP_TIMEOUT",default=60.,type=_DEADLINE_TYPE)
@click.option("--shutdown-timeout",envvar="AGENT_TEAM_SHUTDOWN_TIMEOUT",default=30.,type=_DEADLINE_TYPE)
def serve(**options):
    """Run the sole leased foreground runtime."""
    from .foreground import run_serve
    raise click.exceptions.Exit(run_serve(**options))


@cli.command("doctor")
@config_options
@click.option("--repo-root",type=click.Path(path_type=Path))
@click.option("--expected-revision")
@click.option("--json","as_json",is_flag=True)
@click.option("--report",type=click.Path(path_type=Path))
@click.option("--timeout",default=30.,type=float)
@click.option("--ready-url")
@click.option("--with-pi",is_flag=True)
@click.option("--supervisor",type=click.Choice(["none","s6"]),default="none")
@click.option("--pi-binary",type=click.Path(path_type=Path))
@click.option("--service-dir",type=click.Path(path_type=Path))
@click.option("--s6-svstat",type=click.Path(path_type=Path))
def doctor(as_json,report,**options):
    """Inspect standalone configuration without changing managed state."""
    import math
    from .doctor import diagnose, write_report, aggregate
    if options["pi_binary"] and not options["with_pi"]:
        raise click.UsageError("--pi-binary requires --with-pi")
    if (options["service_dir"] or options["s6_svstat"]) and options["supervisor"]!="s6":
        raise click.UsageError("s6 inputs require --supervisor=s6")
    if options["timeout"]<=0 or not math.isfinite(options["timeout"]):
        raise click.UsageError("--timeout must be positive and finite")
    result=diagnose(**options)
    if report:
        try:
            write_report(result,report,inspected_inputs=(options.get("pi_binary"),options.get("s6_svstat"),options.get("service_dir")))
        except Exception as exc:
            result["checks"].append(dict(id="report",scope="core",required=True,status="failed",
                reason="report_write_failed",detail="Report could not be durably written",remediation="Use a safe existing external parent",
                observed_at=result["generated_at"],evidence={"commit_state":getattr(exc,"commit_state","not_committed")}))
            aggregate(result)
            click.echo("report_write_failed",err=True)
    if as_json:
        click.echo(json.dumps(redact_sensitive_data(result)))
    else:
        safe=redact_sensitive_data(result)
        click.echo(f'{safe["outcome"]} (exit {safe["exit_code"]})')
        for name in ("config_file","config_source","state_dir","workspace_dir"):
            click.echo(f'{name}: {safe[name]}')
        click.echo("selected scopes: " + json.dumps(safe["selected_scopes"],sort_keys=True))
        observation=safe["runtime_observation"]
        click.echo(f'lease: {observation["lease_status"]}; owner PID: {observation["owner_pid"]}; started: {observation["owner_started_at"]}')
        click.echo(f'readiness URL: {observation["readiness_url"]}; endpoint_ready: {observation["endpoint_ready"]}; reason: {observation["readiness_reason"]}')
        click.echo(f'selected runtime correlation: {observation["selected_runtime_correlation"]}')
        for check in safe["checks"]:
            click.echo(f'{check["id"]}: {check["status"]} ({check["reason"]})')
            if check["detail"]:
                click.echo(f'  detail: {check["detail"]}')
            if check["remediation"]:
                click.echo(f'  remediation: {check["remediation"]}')
    raise click.exceptions.Exit(result["exit_code"])


@cli.group()
def swarm():
    """Discover models, persist owned configuration, and verify activation."""


@swarm.command("detect")
@config_options
def swarm_detect(**selectors):
    try:
        discovery=build_model_discovery(**selectors)
        try:
            click.echo(redact_sensitive_text(discovery.detect_model()))
        finally:
            close=getattr(discovery,"close",None)
            if close:
                close()
    except Exception as exc:
        raise click.ClickException(getattr(exc,"reason","configuration_or_discovery_error")) from exc


@swarm.command("status")
@config_options
@click.option("--json","as_json",is_flag=True)
def swarm_status(as_json,**selectors):
    try:
        status=build_swarm_manager(**selectors).inspect()
        data=status.model_dump()
    except Exception:
        data={"effective_models":{},"drift":["configuration_error"],"supervisor_state":"not_configured"}
        status=None
    data=redact_sensitive_data(data)
    click.echo(json.dumps(data) if as_json else "\n".join(f"{key}: {value}" for key,value in data.items()))
    if status is None or not status.synchronized:
        raise click.exceptions.Exit(1)


@swarm.command("reconcile")
@config_options
@click.option("--model")
@click.option("--dry-run",is_flag=True)
@click.option("--no-restart",is_flag=True)
@click.option("--no-verify",is_flag=True)
@click.option("--json","as_json",is_flag=True)
def swarm_reconcile(model,dry_run,no_restart,no_verify,as_json,**selectors):
    try:
        result=build_swarm_manager(**selectors).reconcile(model,restart=not no_restart,verify=not no_verify,dry_run=dry_run)
    except Exception as exc:
        data={"outcome":"validation_failed","exit_code":1,"errors":[getattr(exc,"reason","configuration_or_selection_error")],"verified":False}
        click.echo(json.dumps(data) if as_json else "validation_failed",err=not as_json)
        raise click.exceptions.Exit(1) from exc
    click.echo(json.dumps(redact_sensitive_data(result.model_dump())) if as_json else f"{result.outcome}: changed={len(result.changed_files)} verified={result.verified}")
    raise click.exceptions.Exit(result.exit_code)


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
@config_options
@click.option("--overwrite-config",is_flag=True)
def init(config_file,state_dir,workspace_dir,overwrite_config):
    """Create external directories and persist an exact canonical configuration."""
    from .control.configuration import atomic_write, reject_symlinks
    from .runtime_boundary import ensure_external_runtime_paths
    from .config import Config
    import uuid
    try:
        raw_path=config_file or os.getenv("AGENT_TEAM_CONFIG_FILE")
        if raw_path:
            reject_symlinks(Path(raw_path).expanduser().absolute())
        selection=resolve_configuration(config_file,state_dir=state_dir,workspace_dir=workspace_dir,allow_missing_named=True)
        path=selection.config_file
        ensure_external_runtime_paths(path)
        reject_symlinks(path)
        old=path.read_bytes() if path.exists() else None
        if old is not None and not overwrite_config:
            persisted=Config._expand_env_vars(yaml.safe_load(old) or {}).get("runtime") or {}
            for key,value in (("state_dir",state_dir),("workspace_dir",workspace_dir)):
                if value is not None and (persisted.get(key) is None or Path(persisted[key]).expanduser().resolve()!=Path(value).expanduser().resolve()):
                    raise ValueError("existing config does not persist explicit "+key+"; use --overwrite-config")
        config=selection.config
        for directory in (config.runtime.state_dir,config.runtime.workspace_dir,*(config.runtime.state_dir/name for name in
                          ("config","sessions","checkpoints","memory","knowledge","skills","projects","artifacts","logs"))):
            reject_symlinks(directory)
            directory.mkdir(parents=True,exist_ok=True)
        if old is None or overwrite_config:
            path.parent.mkdir(parents=True,exist_ok=True)
            if old is not None:
                backup=path.with_name(path.name+".backup-"+uuid.uuid4().hex)
                atomic_write(backup,old,expected=None,mode=path.stat().st_mode & 0o777)
            content=("# Agent Team Configuration\n"+yaml.safe_dump(config.model_dump(mode="json"),sort_keys=False)).encode()
            atomic_write(path,content,expected=old)
        click.echo(f"config: {redact_sensitive_text(path)}")
        click.echo(f"state: {redact_sensitive_text(config.runtime.state_dir)}")
        click.echo(f"workspace: {redact_sensitive_text(config.runtime.workspace_dir)}")
        click.echo("Initialization complete")
    except Exception as exc:
        raise click.ClickException(str(exc) if type(exc) is ValueError else "initialization failed; preserve existing configuration and check external paths") from exc



def main() -> None:
    """Console-script entrypoint."""
    cli()


if __name__ == "__main__":
    main()
