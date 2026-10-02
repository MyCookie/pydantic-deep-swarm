"""Nonmutating standalone diagnostics with explicitly selected adapter scopes."""
from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import total_ordering
from pathlib import Path

import httpx

from .config import active_roles, resolve_configuration
from .control.configuration import atomic_write, reject_symlinks
from .control.discovery import resolve_role_models, safe_endpoint
from .subprocess_env import sanitized_subprocess_env
from .redaction import redact_sensitive_data


@total_ordering
class SemVer:
    """Strict SemVer 2.0 precedence; build metadata never changes ordering."""
    pattern = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z")

    def __init__(self, value):
        match = self.pattern.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise ValueError("invalid SemVer")
        self.text=value
        self.core=tuple(int(x) for x in match.groups()[:3])
        self.pre=tuple(match[4].split(".")) if match[4] else ()
        if any(x.isdigit() and len(x)>1 and x.startswith("0") for x in self.pre):
            raise ValueError("numeric prerelease identifier has leading zeros")

    def __eq__(self, other):
        return isinstance(other,SemVer) and self.core==other.core and self.pre==other.pre

    def __lt__(self, other):
        if self.core!=other.core:
            return self.core<other.core
        if not self.pre or not other.pre:
            return bool(self.pre) and not other.pre
        for left,right in zip(self.pre,other.pre):
            if left==right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left)<int(right)
            if left.isdigit()!=right.isdigit():
                return left.isdigit()
            return left<right
        return len(self.pre)<len(other.pre)


def _now():
    return datetime.now(timezone.utc).isoformat()


def diagnose(*, config_file=None,state_dir=None,workspace_dir=None,repo_root=None,expected_revision=None,
             timeout=30.,ready_url=None,with_pi=False,supervisor="none",pi_binary=None,
             service_dir=None,s6_svstat=None):
    if not math.isfinite(timeout) or timeout<=0:
        raise ValueError("timeout must be positive and finite")
    deadline=time.monotonic()+timeout
    root=Path(repo_root or os.getenv("AGENT_TEAM_PROJECT_ROOT") or Path(__file__).resolve().parents[2]).expanduser().resolve()
    report=dict(report_kind="agent-team.doctor",schema_version=1,generated_at=_now(),repository_root=str(root),revision=None,
                expected_revision=expected_revision,config_file=None,config_source=None,state_dir=None,workspace_dir=None,
                selected_scopes={"core":True,"pi":with_pi,"s6":supervisor=="s6"},checks=[],
                runtime_observation=dict(lease_status="unavailable",owner_pid=None,owner_started_at=None,
                readiness_url=None,endpoint_ready=None,readiness_reason="not_observed",selected_runtime_correlation="unknown"))

    def add(id,status,reason,*,scope="core",required=True,detail="",remediation="",evidence=None):
        report["checks"].append(dict(id=id,scope=scope,required=required,status=status,reason=reason,detail=detail,
                                    remediation=remediation,observed_at=_now(),evidence=evidence or {}))

    def remaining():
        value=deadline-time.monotonic()
        if value<=0:
            raise TimeoutError("diagnostic deadline exhausted")
        return value

    def command(argv):
        return subprocess.run(argv,capture_output=True,text=True,timeout=min(5.,remaining()),env=sanitized_subprocess_env(),check=False)

    try:
        result=command(["git","-C",str(root),"rev-parse","HEAD"])
        revision=result.stdout.strip()
        if result.returncode or not re.fullmatch("[0-9a-f]{40,64}",revision):
            add("repository_revision","failed","repository_revision_unavailable")
        else:
            report["revision"]=revision
            add("repository_revision","failed" if expected_revision and revision!=expected_revision else "passed",
                "revision_mismatch" if expected_revision and revision!=expected_revision else "revision_verified")
            dirty=command(["git","-C",str(root),"status","--porcelain"]).stdout.strip()
            if dirty:
                add("repository_dirty","warning","dirty_checkout",required=False)
        layout=("pyproject.toml","src/agent_team/config.py","src/agent_team/app.py")
        add("repository_layout","passed" if all((root/p).is_file() for p in layout) else "failed","core_layout",evidence={"required_paths":list(layout)})
    except TimeoutError:
        add("repository_revision","unverified","deadline_exhausted")
    except Exception:
        add("repository_revision","failed","repository_probe_failed")
    dependencies={}
    try:
        for name in ("pydantic","pydantic-ai","httpx","fastapi","uvicorn","click","aiosqlite","pyyaml"):
            remaining()
            dependencies[name]=importlib.metadata.version(name)
        compatible=sys.version_info>=(3,11)
        add("dependencies","passed" if compatible else "failed","dependencies_available" if compatible else "python_unsupported",evidence={"python":sys.version.split()[0],"versions":dependencies})
    except TimeoutError:
        add("dependencies","unverified","deadline_exhausted")
    except importlib.metadata.PackageNotFoundError:
        add("dependencies","failed","dependency_missing")
    selection=None
    try:
        remaining()
        selection=resolve_configuration(config_file,state_dir=state_dir,workspace_dir=workspace_dir)
        config=selection.config
        from .runtime_boundary import ensure_external_runtime_paths
        ensure_external_runtime_paths(config.runtime.state_dir,workspace_dir=config.runtime.workspace_dir,repository_root=root)
        report.update(config_file=str(selection.config_file),config_source=selection.config_source,
                      state_dir=str(config.runtime.state_dir),workspace_dir=str(config.runtime.workspace_dir))
        add("configuration","passed","configuration_valid",evidence={"source":selection.config_source})
        for name,path in (("state",config.runtime.state_dir),("workspace",config.runtime.workspace_dir)):
            existing=path
            while not existing.exists() and existing!=existing.parent:
                existing=existing.parent
            mode=existing.stat().st_mode
            ok=existing.is_dir() and bool(mode & 0o222) and bool(mode & 0o111) and os.access(existing,os.R_OK|os.W_OK|os.X_OK)
            add(name+"_path","passed" if ok else "failed","path_accessible" if ok else "path_not_accessible",
                evidence={"path":str(path),"exists":path.exists(),"existing_parent":str(existing),"write_probe":False})
        try:
            effective,catalogs=resolve_role_models(config,deadline=deadline)
            add("models","passed","models_resolved",evidence={"roles":{r:{"configured":config.models[r].model,"resolved":effective.models[r].model,"endpoint":safe_endpoint(config.models[r].base_url or "")} for r in active_roles(config)},"catalogs":{safe_endpoint(k):v for k,v in catalogs.items()}})
        except TimeoutError:
            add("models","unverified","deadline_exhausted")
        except Exception as exc:
            add("models","failed",getattr(exc,"reason","model_unavailable"))
        if config.memory.enabled and config.memory.shared_knowledge:
            try:
                from .memory.preparation import inspect_knowledge
                inspection=inspect_knowledge(config.runtime.state_dir,deadline=deadline)
                finding=inspection.result
                report["runtime_observation"]["lease_status"]=inspection.lease_status
                owner=getattr(inspection,"owner",None) or {}
                report["runtime_observation"].update(owner_pid=owner.get("pid"),owner_started_at=owner.get("started_at"))
                status="unverified" if finding.status in {"inspection_deferred","inspection_unavailable"} else "passed" if finding.status in {"canonical","needs_initialization","migration_needed","recovery_pending"} else "failed"
                add("knowledge",status,finding.status,evidence=finding.as_dict(),remediation="Stop writers and preserve all database bundles and recovery evidence before offline repair." if status!="passed" else "")
            except Exception:
                add("knowledge","unverified","inspection_unavailable")
        else:
            add("knowledge","not_selected","disabled",required=False)
            try:
                from .persistence import RuntimeLeaseInspection
                with RuntimeLeaseInspection(config.runtime.state_dir/"runtime.lock") as observation:
                    report["runtime_observation"]["lease_status"]=observation.status
                    owner=observation.owner or {}
                    report["runtime_observation"].update(owner_pid=owner.get("pid"),owner_started_at=owner.get("started_at"))
                if observation.status=="unavailable":
                    add("lease","warning","lease_observation_unavailable",required=False)
            except Exception:
                add("lease","warning","lease_observation_unavailable",required=False)
    except TimeoutError:
        add("configuration","unverified","deadline_exhausted")
    except Exception:
        add("configuration","failed","configuration_error",remediation="Correct the selected configuration and external paths.")
    host=os.getenv("AGENT_TEAM_HOST","localhost")
    if ":" in host and not host.startswith("["):
        host=f"[{host}]"
    url=ready_url or f"http://{host}:{os.getenv('AGENT_TEAM_PORT','8080')}/ready"
    if url.rstrip("/").endswith("/health"):
        url=url.rstrip("/")[:-7]+"/ready"
    report["runtime_observation"]["readiness_url"]=safe_endpoint(url)
    try:
        headers={"Authorization":"Bearer "+os.environ["AGENT_TEAM_API_TOKEN"]} if os.getenv("AGENT_TEAM_API_TOKEN") else {}
        with httpx.Client(timeout=min(5.,remaining()),trust_env=False) as client:
            response=client.get(url,headers=headers)
            body=response.json()
            ready=response.status_code==200 and isinstance(body,dict) and body.get("status")=="ok" and body.get("ready") is True
        report["runtime_observation"].update(endpoint_ready=ready,readiness_reason="endpoint_ready" if ready else "endpoint_not_ready")
    except Exception:
        report["runtime_observation"].update(endpoint_ready=None,readiness_reason="endpoint_unavailable")
    if not with_pi:
        add("pi","not_selected","not_selected",scope="pi",required=False)
    else:
        minima=[]
        floor=None
        try:
            remaining()
            from .pi_reconciler import _validate_package
            manifest=_validate_package(root/"pi")
            remaining()
            minima=[{"path":"manifest.json","version":manifest["minimum_pi_version"]}]+[{"path":a["path"],"version":a["minimum_pi_version"]} for a in manifest["assets"]]
            floor=max(SemVer(a["version"]) for a in minima)
            add("pi_assets","passed","pi_assets_valid",scope="pi",evidence={"minima":minima,"floor":floor.text})
        except TimeoutError:
            add("pi_assets","unverified","deadline_exhausted",scope="pi")
        except Exception:
            add("pi_assets","failed","pi_assets_invalid",scope="pi")
        binary=Path(pi_binary).expanduser().resolve() if pi_binary else Path(shutil.which("pi") or "")
        if not binary.is_file() or not os.access(binary,os.X_OK):
            add("pi_binary","failed","pi_binary_unavailable",scope="pi",evidence={"path":str(binary.absolute())})
        else:
            add("pi_binary","passed","pi_binary_available",scope="pi",evidence={"path":str(binary.absolute())})
            try:
                result=command([str(binary),"--version"])
                if result.returncode:
                    add("pi_version","failed","pi_version_probe_failed",scope="pi")
                else:
                    raw=result.stdout.strip()
                    if raw.startswith("pi "):
                        raw=raw[3:]
                    try:
                        installed=SemVer(raw)
                        ok=floor is not None and installed>=floor
                        add("pi_version","unverified" if floor is None else "passed" if ok else "failed","pi_floor_unavailable" if floor is None else "pi_version_compatible" if ok else "pi_version_unsupported",scope="pi",evidence={"installed":installed.text,"floor":floor.text if floor else None})
                    except ValueError:
                        add("pi_version","failed","pi_version_invalid",scope="pi")
            except TimeoutError:
                add("pi_version","unverified","deadline_exhausted",scope="pi")
            except subprocess.TimeoutExpired:
                add("pi_version","failed","pi_version_probe_failed",scope="pi",detail="The launched version probe exceeded its allotted time.",remediation="Check the selected executable and retry with enough total diagnostic time.")
            except Exception:
                add("pi_version","failed","pi_version_probe_failed",scope="pi")
    if supervisor!="s6":
        add("s6","not_selected","not_selected",scope="s6",required=False)
    else:
        binary=Path(s6_svstat).expanduser().resolve() if s6_svstat else Path(shutil.which("s6-svstat") or "")
        directory=Path(service_dir or os.getenv("AGENT_TEAM_SERVICE_DIR") or "").expanduser().resolve()
        if not binary.is_file() or not os.access(binary,os.X_OK) or not directory.is_dir() or not service_dir and not os.getenv("AGENT_TEAM_SERVICE_DIR"):
            add("s6","failed","s6_prerequisite_unavailable",scope="s6",evidence={"binary":str(binary.absolute()),"service_dir":str(directory)})
        else:
            try:
                result=command([str(binary),str(directory)])
                up=result.returncode==0 and result.stdout.strip().startswith("up ")
                ready=report["runtime_observation"]["endpoint_ready"] is True
                add("s6","passed" if up and ready else "failed","s6_ready" if up and ready else "s6_not_ready",scope="s6",evidence={"binary":str(binary.absolute()),"service_dir":str(directory),"selected_runtime_correlation":"unknown"})
            except TimeoutError:
                add("s6","unverified","deadline_exhausted",scope="s6")
            except subprocess.TimeoutExpired:
                add("s6","failed","s6_status_probe_failed",scope="s6",detail="The launched status probe exceeded its allotted time.",remediation="Check the selected status executable/service and retry with enough total diagnostic time.")
            except Exception:
                add("s6","failed","s6_status_probe_failed",scope="s6")
    aggregate(report)
    return report


def aggregate(report):
    required=[c for c in report["checks"] if c["required"]]
    failed=any(c["status"]=="failed" for c in required)
    unknown=any(c["status"]=="unverified" for c in required)
    report.update(outcome="blocked" if failed else "unverified" if unknown else "validated",exit_code=1 if failed else 2 if unknown else 0,validation_complete=not unknown)


def write_report(report,path,*,inspected_inputs=()):
    path=Path(path).expanduser().absolute()
    reject_symlinks(path)
    for name in ("repository_root","state_dir","workspace_dir"):
        root=report.get(name)
        if root and path.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError("doctor report must be outside source and managed state/workspace")
    if not path.parent.is_dir():
        raise ValueError("doctor report parent must already exist")
    for value in (report.get("config_file"),*inspected_inputs):
        if value and (path.resolve()==Path(value).expanduser().resolve() or Path(value).is_dir() and path.resolve().is_relative_to(Path(value).resolve())):
            raise ValueError("doctor report collides with an inspected input")
    expected=path.read_bytes() if path.exists() else None
    atomic_write(path,(json.dumps(redact_sensitive_data(report),indent=2)+"\n").encode(),expected=expected)
