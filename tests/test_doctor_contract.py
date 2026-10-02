"""Doctor aggregation, scoped adapters, version gates and report nonmutation."""
from config_doctor_evidence import config_doctor_evidence
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from click.testing import CliRunner

from agent_team.cli import cli
from agent_team.doctor import SemVer, diagnose, aggregate, write_report


@pytest.fixture
def fixture(monkeypatch,tmp_path):
    for key in list(os.environ):
        if key.startswith(("AGENT_TEAM_","LLM_","PRINCIPAL_","MANAGER_","WORKER_","CURATOR_")):
            monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv("HOME",str(tmp_path/"home"))
    path=tmp_path/"config.yaml"
    path.write_text(yaml.safe_dump({"runtime":{"state_dir":str(tmp_path/"state"),"workspace_dir":str(tmp_path/"work")},"models":{"default":{"base_url":"http://model/v1","model":"chosen"}},"memory":{"enabled":False}}))
    monkeypatch.setattr("agent_team.doctor.resolve_role_models",lambda config,deadline=None:(config,{"http://model/v1":["chosen"]}))
    real_client=httpx.Client
    requests=[]
    def response(request): requests.append(request); return httpx.Response(200,json={"status":"ok","ready":True})
    monkeypatch.setattr("agent_team.doctor.httpx.Client",lambda **kwargs:real_client(transport=httpx.MockTransport(response)))
    return path,requests


@pytest.mark.parametrize("value",["0.0.0","1.0.0-alpha.2","1.0.0-alpha.10","1.0.0","12.0.0+build.01"])
def test_strict_semver_valid(value): assert SemVer(value).text==value


@pytest.mark.parametrize("value",["01.0.0","1.02.0","1.0.00","1.0.0-alpha.01","v1.0.0","1.0","1.0.0\n1.0.0","1.0.0 ","1.0.0-","1.0.0+","1.0.0-α"])
def test_strict_semver_invalid(value):
    with pytest.raises(ValueError): SemVer(value)


def test_semver_precedence():
    chain=["0.9.0","1.0.0-alpha","1.0.0-alpha.2","1.0.0-alpha.10","1.0.0-beta","1.0.0-beta.2","1.0.0","2.0.0"]
    assert all(SemVer(a)<SemVer(b) for a,b in zip(chain,chain[1:]))
    assert SemVer("1.0.0+aaa")==SemVer("1.0.0+bbb")


@pytest.mark.parametrize("failed,unverified",list(itertools.product((False,True),repeat=2)))
def test_failure_unverified_precedence(failed,unverified):
    report={"checks":[{"required":True,"status":"failed" if failed else "passed"},{"required":True,"status":"unverified" if unverified else "warning"}]}
    aggregate(report)
    assert report["exit_code"]==(1 if failed else 2 if unverified else 0)
    assert report["validation_complete"]==(not unverified)


@pytest.mark.parametrize("knowledge,expected",[("canonical",0),("needs_initialization",0),("migration_needed",0),("recovery_pending",0),("ambiguous",1),("invalid",1),("unsupported_schema",1),("unsafe_path",1),("orphan_sidecar",1),("recovery_required",1),("inspection_deferred",2),("inspection_unavailable",2)])
def test_enabled_knowledge_result_aggregation(monkeypatch,fixture,knowledge,expected):
    path,requests=fixture
    data=yaml.safe_load(path.read_text()); data["memory"]["enabled"]=True; path.write_text(yaml.safe_dump(data))
    result=SimpleNamespace(status=knowledge,as_dict=lambda:{"status":knowledge})
    inspection=SimpleNamespace(result=result,lease_status="occupied" if knowledge=="inspection_deferred" else "free",owner={"pid":123,"started_at":"safe"})
    monkeypatch.setattr("agent_team.memory.preparation.inspect_knowledge",lambda *a,**kw:inspection)
    before=path.read_bytes()
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],ready_url="http://unrelated/ready")
    assert report["exit_code"]==expected and path.read_bytes()==before
    assert report["runtime_observation"]["selected_runtime_correlation"]=="unknown"
    assert report["runtime_observation"]["endpoint_ready"] is True


@pytest.mark.parametrize("as_json",[False,True],ids=["text","json"])
@pytest.mark.parametrize("enabled",[False,True],ids=["disabled_lease","enabled_knowledge"])
@pytest.mark.parametrize("error_type",[RuntimeError,AttributeError,TimeoutError],ids=["runtime","attribute","timeout"])
def test_cli_inspector_execution_errors_are_required_failures(monkeypatch,tmp_path,fixture,as_json,enabled,error_type):
    path,_=fixture
    data=yaml.safe_load(path.read_text()); data["memory"]["enabled"]=enabled; path.write_text(yaml.safe_dump(data))
    calls=[]
    def broken(*args,**kwargs):
        calls.append("inspection")
        raise error_type("diagnostic-secret-marker")
    if enabled:
        monkeypatch.setattr("agent_team.memory.preparation.inspect_knowledge",broken)
    else:
        monkeypatch.setattr("agent_team.persistence.RuntimeLeaseInspection",broken)
    destination=tmp_path/"diagnostic.json"
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--report",str(destination),*(["--json"] if as_json else [])])
    report=json.loads(destination.read_text())
    finding=next(c for c in report["checks"] if c["id"]==("knowledge" if enabled else "lease"))
    assert calls==["inspection"] and result.exit_code==report["exit_code"]==1
    assert (finding["status"],finding["reason"],finding["required"])==("failed","execution_error",True)
    assert report["outcome"]=="blocked" and report["validation_complete"] is True
    assert finding["detail"] and finding["remediation"] and finding["evidence"]=={}
    assert "diagnostic-secret-marker" not in result.output+destination.read_text()
    if as_json: assert json.loads(result.stdout)==report
    else: assert "execution_error" in result.stdout and "blocked (exit 1)" in result.stdout


@pytest.mark.parametrize("as_json",[False,True],ids=["text","json"])
@pytest.mark.parametrize("enabled",[False,True],ids=["disabled_lease","enabled_knowledge"])
def test_cli_budget_prevents_inspection_without_relabelling_configuration(monkeypatch,tmp_path,fixture,as_json,enabled):
    path,_=fixture
    data=yaml.safe_load(path.read_text()); data["memory"]["enabled"]=enabled; path.write_text(yaml.safe_dump(data))
    clock=[0.]
    monkeypatch.setattr("agent_team.doctor.time.monotonic",lambda:clock[0])
    def models(config,deadline=None):
        clock[0]=1.
        return config,{"http://model/v1":["chosen"]}
    monkeypatch.setattr("agent_team.doctor.resolve_role_models",models)
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append("inspection")
        raise AssertionError("inspection launched after deadline")
    monkeypatch.setattr("agent_team.memory.preparation.inspect_knowledge",forbidden)
    monkeypatch.setattr("agent_team.persistence.RuntimeLeaseInspection",forbidden)
    destination=tmp_path/"diagnostic.json"
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--timeout","1","--report",str(destination),*(["--json"] if as_json else [])])
    report=json.loads(destination.read_text())
    finding=next(c for c in report["checks"] if c["id"]==("knowledge" if enabled else "lease"))
    assert calls==[]
    assert result.exit_code==report["exit_code"]==(2 if enabled else 0)
    assert (finding["status"],finding["reason"],finding["required"])==("unverified" if enabled else "warning","deadline_exhausted",enabled)
    assert report["validation_complete"] is (not enabled)
    assert [(c["status"],c["reason"]) for c in report["checks"] if c["id"]=="configuration"]==[("passed","configuration_valid")]
    if as_json: assert json.loads(result.stdout)==report
    else: assert "deadline_exhausted" in result.stdout and f"(exit {report['exit_code']})" in result.stdout


@pytest.mark.parametrize("as_json",[False,True],ids=["text","json"])
@pytest.mark.parametrize("prior_failure",[False,True],ids=["no_prior_failure","prior_failure"])
@pytest.mark.parametrize("status,reason",[("inspection_unavailable","deadline_exceeded"),("inspection_unavailable","coherent_copy_unavailable"),("inspection_deferred","runtime_occupied")])
def test_cli_structured_inspection_unknown_preserves_failure_precedence(monkeypatch,tmp_path,fixture,as_json,prior_failure,status,reason):
    from agent_team.memory.preparation import KnowledgeInspection, KnowledgeResult
    path,_=fixture
    data=yaml.safe_load(path.read_text()); data["memory"]["enabled"]=True; path.write_text(yaml.safe_dump(data))
    if prior_failure:
        def models(*args,**kwargs): raise RuntimeError("diagnostic-secret-marker")
        monkeypatch.setattr("agent_team.doctor.resolve_role_models",models)
    calls=[]
    def inspect(*args,**kwargs):
        calls.append("inspection")
        return KnowledgeInspection(KnowledgeResult(status,reason=reason),"occupied" if status=="inspection_deferred" else "free")
    monkeypatch.setattr("agent_team.memory.preparation.inspect_knowledge",inspect)
    destination=tmp_path/"diagnostic.json"
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--report",str(destination),*(["--json"] if as_json else [])])
    report=json.loads(destination.read_text())
    finding=next(c for c in report["checks"] if c["id"]=="knowledge")
    assert calls==["inspection"] and result.exit_code==report["exit_code"]==(1 if prior_failure else 2)
    assert (finding["status"],finding["reason"],finding["required"])==("unverified",status,True)
    assert finding["evidence"]["reason"]==reason and report["validation_complete"] is False
    assert not any(c["reason"]=="execution_error" for c in report["checks"])
    assert "diagnostic-secret-marker" not in result.output+destination.read_text()
    if as_json: assert json.loads(result.stdout)==report
    else: assert status in result.stdout and f"(exit {report['exit_code']})" in result.stdout


@pytest.mark.parametrize("as_json",[False,True],ids=["text","json"])
def test_cli_disabled_expected_lease_unavailable_stays_optional(monkeypatch,tmp_path,fixture,as_json):
    path,_=fixture
    calls=[]
    class Unavailable:
        status="unavailable"
        owner=None
        def __init__(self,*args,**kwargs): calls.append("inspection")
        def __enter__(self): return self
        def __exit__(self,*args): pass
    monkeypatch.setattr("agent_team.persistence.RuntimeLeaseInspection",Unavailable)
    destination=tmp_path/"diagnostic.json"
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--report",str(destination),*(["--json"] if as_json else [])])
    report=json.loads(destination.read_text())
    finding=next(c for c in report["checks"] if c["id"]=="lease")
    assert calls==["inspection"] and result.exit_code==report["exit_code"]==0
    assert (finding["status"],finding["reason"],finding["required"])==("warning","lease_observation_unavailable",False)
    assert report["validation_complete"] is True and report["runtime_observation"]["lease_status"]=="unavailable"
    if as_json: assert json.loads(result.stdout)==report
    else: assert "lease_observation_unavailable" in result.stdout and "validated (exit 0)" in result.stdout


@pytest.mark.parametrize("as_json",[False,True],ids=["text","json"])
def test_cli_launched_knowledge_inspection_budget_returns_structured_unknown(monkeypatch,tmp_path,fixture,as_json):
    from agent_team.memory import preparation
    from agent_team.persistence import RuntimeLease
    path,_=fixture
    data=yaml.safe_load(path.read_text()); data["memory"]["enabled"]=True; path.write_text(yaml.safe_dump(data))
    state=path.parent/"state"
    with RuntimeLease(state/"runtime.lock"):
        pass
    clock=[0.]
    monkeypatch.setattr("agent_team.doctor.time.monotonic",lambda:clock[0])
    actual=preparation.inspect_knowledge
    calls=[]
    def inspect(*args,**kwargs):
        calls.append("inspection")
        clock[0]=1.
        return actual(*args,**kwargs)
    monkeypatch.setattr(preparation,"inspect_knowledge",inspect)
    destination=tmp_path/"diagnostic.json"
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--timeout","1","--report",str(destination),*(["--json"] if as_json else [])])
    report=json.loads(destination.read_text())
    finding=next(c for c in report["checks"] if c["id"]=="knowledge")
    assert calls==["inspection"] and result.exit_code==report["exit_code"]==2
    assert (finding["status"],finding["reason"],finding["required"])==("unverified","inspection_unavailable",True)
    assert finding["evidence"]["reason"]=="deadline_exceeded" and report["validation_complete"] is False
    assert report["runtime_observation"]["lease_status"]=="free"
    assert not (state/"knowledge").exists() and not (state/"runtime-owner.json").exists()
    if as_json: assert json.loads(result.stdout)==report
    else: assert "inspection_unavailable" in result.stdout and "unverified (exit 2)" in result.stdout


def test_disabled_knowledge_never_inspected(monkeypatch,fixture):
    path,requests=fixture
    def forbidden(*args,**kwargs): raise AssertionError("knowledge touched")
    monkeypatch.setattr("agent_team.memory.preparation.inspect_knowledge",forbidden)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
    assert report["exit_code"]==0 and report["report_kind"]=="agent-team.doctor"
    assert next(c for c in report["checks"] if c["id"]=="knowledge")["reason"]=="disabled"
    assert not (path.parent/"state").exists()


@pytest.mark.parametrize("flags",[["--pi-binary","/missing"],["--service-dir","/missing"],["--s6-svstat","/missing"],["--timeout","nan"],["--timeout","0"],["--supervisor","unknown"]])
def test_doctor_usage_errors(flags): assert CliRunner().invoke(cli,["doctor",*flags]).exit_code==2


@pytest.mark.parametrize("output,code,expected",[("pi 1.0.0\n",0,"pi_version_compatible"),("2.0.0+build",0,"pi_version_compatible"),("0.9.0",0,"pi_version_unsupported"),("1.0.0-alpha.2",0,"pi_version_unsupported"),("",0,"pi_version_invalid"),("version 1.0.0",0,"pi_version_invalid"),("v1.0.0",0,"pi_version_invalid"),("1.0.0\nextra",0,"pi_version_invalid"),("1.0.0",1,"pi_version_probe_failed")])
def test_pi_version_probe_contract(monkeypatch,tmp_path,fixture,output,code,expected):
    path,_=fixture
    binary=tmp_path/"pi"; binary.write_text("#!/bin/sh\nexit 0\n"); binary.chmod(0o700)
    monkeypatch.setattr("agent_team.pi_reconciler._validate_package",lambda p:{"minimum_pi_version":"0.5.0","assets":[{"path":"optional","minimum_pi_version":"1.0.0"}]})
    original=__import__("subprocess").run
    def run(argv,**kwargs):
        if argv[0]==str(binary): return SimpleNamespace(returncode=code,stdout=output,stderr="1.0.0")
        return original(argv,**kwargs)
    monkeypatch.setattr("agent_team.doctor.subprocess.run",run)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],with_pi=True,pi_binary=binary)
    finding=next(c for c in report["checks"] if c["id"]=="pi_version")
    assert finding["reason"]==expected and report["exit_code"]==(0 if expected=="pi_version_compatible" else 1)


@pytest.mark.parametrize("minimum",["01.0.0","1.0.0-alpha.01"," 1.0.0","1.0.0\n"])
def test_source_minima_never_coerced(monkeypatch,fixture,minimum):
    path,_=fixture
    monkeypatch.setattr("agent_team.pi_reconciler._validate_package",lambda p:{"minimum_pi_version":"0.0.0","assets":[{"path":"test","minimum_pi_version":minimum}]})
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],with_pi=True,pi_binary="/definitely/missing/pi")
    assert any(c["reason"]=="pi_assets_invalid" for c in report["checks"]) and report["exit_code"]==1


@pytest.mark.parametrize("up,ready",list(itertools.product((False,True),repeat=2)))
def test_explicit_s6_requires_service_and_ready(monkeypatch,tmp_path,fixture,up,ready):
    path,_=fixture
    binary=tmp_path/"svstat"; binary.write_text("#! /bin/sh\n"); binary.chmod(0o700)
    service=tmp_path/"service"; service.mkdir()
    original=__import__("subprocess").run
    def run(argv,**kwargs):
        if argv[0]==str(binary): return SimpleNamespace(returncode=0,stdout="up (pid 1)" if up else "down",stderr="")
        return original(argv,**kwargs)
    monkeypatch.setattr("agent_team.doctor.subprocess.run",run)
    real_client=httpx.Client
    # fixture already injects a safe Client; override response via its request path.
    class Client:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def get(self,*a,**kw): return SimpleNamespace(status_code=200,json=lambda:{"status":"ok","ready":ready})
    monkeypatch.setattr("agent_team.doctor.httpx.Client",lambda **kw:Client())
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],supervisor="s6",service_dir=service,s6_svstat=binary)
    assert report["exit_code"]==(0 if up and ready else 1)
    assert next(c for c in report["checks"] if c["id"]=="pi")["status"]=="not_selected"


def test_report_output_is_external_atomic_and_collision_safe(tmp_path,fixture):
    path,_=fixture
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
    target=tmp_path/"report.json"; write_report(report,target)
    assert json.loads(target.read_bytes())==report
    before=path.read_bytes()
    with pytest.raises(ValueError): write_report(report,path)
    assert path.read_bytes()==before
    with pytest.raises(ValueError): write_report(report,tmp_path/"state/report")
    with pytest.raises(ValueError): write_report(report,tmp_path/"missing/report")
    link=tmp_path/"report-link"; link.symlink_to(target)
    with pytest.raises(Exception): write_report(report,link)


def test_cli_json_report_failure_has_final_exit_one(monkeypatch,tmp_path,fixture):
    path,_=fixture
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--json","--report",str(tmp_path/"missing/report")])
    assert result.exit_code==1
    report=json.loads(result.stdout)
    assert report["exit_code"]==1 and report["outcome"]=="blocked"
    assert "report_write_failed" in result.stderr


@pytest.mark.parametrize("scope",["pi","s6","both"])
def test_selected_missing_optional_prerequisites_fail(fixture,scope):
    path,_=fixture
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],with_pi=scope in {"pi","both"},
                    supervisor="s6" if scope in {"s6","both"} else "none",pi_binary="/definitely/missing/pi",s6_svstat="/definitely/missing/svstat")
    assert report["exit_code"]==1
    assert report["selected_scopes"]=={"core":True,"pi":scope in {"pi","both"},"s6":scope in {"s6","both"}}


@pytest.mark.parametrize("fault",["permission","timeout","nonexecutable"])
def test_pi_binary_probe_errors(monkeypatch,tmp_path,fixture,fault):
    import subprocess
    path,_=fixture
    binary=tmp_path/"pi"; binary.write_text("#! /bin/sh\n"); binary.chmod(0o600 if fault=="nonexecutable" else 0o700)
    monkeypatch.setattr("agent_team.pi_reconciler._validate_package",lambda p:{"minimum_pi_version":"0.0.0","assets":[]})
    original=subprocess.run
    def run(argv,**kwargs):
        if argv[0]==str(binary):
            raise subprocess.TimeoutExpired(argv,5) if fault=="timeout" else PermissionError("secret exception")
        return original(argv,**kwargs)
    monkeypatch.setattr("agent_team.doctor.subprocess.run",run)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],with_pi=True,pi_binary=binary)
    assert report["exit_code"]==1
    assert any(c["reason"]==("pi_binary_unavailable" if fault=="nonexecutable" else "pi_version_probe_failed") for c in report["checks"])
    assert "secret exception" not in json.dumps(report)


def test_total_deadline_required_unknown_blocks(monkeypatch,fixture):
    path,_=fixture
    calls=0
    def clock():
        nonlocal calls
        calls+=1
        return 0 if calls==1 else 100
    monkeypatch.setattr("agent_team.doctor.time.monotonic",clock)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],timeout=1)
    assert report["exit_code"]==2 and not report["validation_complete"]
    assert any(c["reason"]=="deadline_exhausted" for c in report["checks"])


def test_configuration_failure_beats_unknown_inspection(fixture,tmp_path):
    path,_=fixture; path.write_text("runtime: {state_dir: null}")
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
    assert report["exit_code"]==1 and report["state_dir"] is None


@pytest.mark.parametrize("endpoint","http://user:sentinel-password@model/v1?sentinel-query#secret-fragment http://model/v1".split(),ids=["credential_url","ordinary_url"])
def test_safe_endpoint_and_readiness_auth_roles(monkeypatch,fixture,endpoint):
    path,requests=fixture
    data=yaml.safe_load(path.read_text()); data["models"]["default"]["base_url"]=endpoint; path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("LLM_API_KEY","inference-sentinel"); monkeypatch.setenv("AGENT_TEAM_API_TOKEN","api-sentinel")
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],ready_url="http://ready/health")
    text=json.dumps(report)
    assert "sentinel-password" not in text and "sentinel-query" not in text and "secret-fragment" not in text
    assert requests[-1].url.path=="/ready" and requests[-1].headers["authorization"]=="Bearer api-sentinel"
    assert "inference-sentinel" not in str(requests[-1].headers)


@pytest.mark.parametrize("site",["stage","directory"])
def test_doctor_report_atomic_failure_state(monkeypatch,tmp_path,fixture,site):
    path,_=fixture; report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
    target=tmp_path/"report.json"; target.write_bytes(b"previous")
    original=os.fsync; calls=0
    def fault(fd):
        nonlocal calls
        calls+=1
        if calls==(1 if site=="stage" else 2): raise OSError("failure")
        return original(fd)
    monkeypatch.setattr(os,"fsync",fault)
    with pytest.raises(Exception) as error: write_report(report,target)
    assert error.value.commit_state==("not_committed" if site=="stage" else "uncertain")
    if site=="stage": assert target.read_bytes()==b"previous"
    else: assert json.loads(target.read_bytes())==report


def test_report_collision_with_optional_inputs(tmp_path,fixture):
    path,_=fixture; report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
    binary=tmp_path/"pi"; binary.write_bytes(b"original binary")
    with pytest.raises(ValueError): write_report(report,binary,inspected_inputs=[binary])
    assert binary.read_bytes()==b"original binary"


@pytest.mark.parametrize("scope",["core","s6"])
def test_readiness_auth_failure_observation_only_unless_s6(monkeypatch,tmp_path,fixture,scope):
    path,_=fixture
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,*args,**kwargs): return SimpleNamespace(status_code=401,json=lambda:{"detail":"Authentication required"})
    monkeypatch.setattr("agent_team.doctor.httpx.Client",lambda **kwargs:Client())
    binary=tmp_path/"svstat"; binary.write_text("#! /bin/sh\n"); binary.chmod(0o700)
    service=tmp_path/"service"; service.mkdir()
    import subprocess
    original=subprocess.run
    def run(argv,**kwargs):
        if argv[0]==str(binary): return SimpleNamespace(returncode=0,stdout="up (pid 1)",stderr="")
        return original(argv,**kwargs)
    monkeypatch.setattr("agent_team.doctor.subprocess.run",run)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],supervisor="s6" if scope=="s6" else "none",service_dir=service if scope=="s6" else None,s6_svstat=binary if scope=="s6" else None)
    assert report["runtime_observation"]["endpoint_ready"] is False
    assert report["exit_code"]==(1 if scope=="s6" else 0)


def test_dependency_absence_and_revision_mismatch(fixture,monkeypatch):
    import importlib.metadata
    path,_=fixture
    original=importlib.metadata.version
    def absent(name):
        if name=="pydantic": raise importlib.metadata.PackageNotFoundError(name)
        return original(name)
    monkeypatch.setattr("agent_team.doctor.importlib.metadata.version",absent)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],expected_revision="0"*40)
    assert report["exit_code"]==1
    assert {"dependency_missing","revision_mismatch"}<={c["reason"] for c in report["checks"]}


def test_unwritable_path_has_no_write_probe(fixture):
    path,_=fixture
    state=path.parent/"state"; state.mkdir(); state.chmod(0o500)
    try:
        report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
        check=next(c for c in report["checks"] if c["id"]=="state_path")
        assert check["status"]=="failed" and check["evidence"]["write_probe"] is False and list(state.iterdir())==[]
    finally: state.chmod(0o700)


def test_doctor_subprocess_credentials_scrubbed(monkeypatch,tmp_path,fixture):
    import subprocess
    path,_=fixture
    credential_names=("EXAMPLE_SECRET_TOKEN","FIXTURE_GH_PAT","FIXTURE_ACCESS_KEY_ID")
    for key in credential_names:
        monkeypatch.setenv(key,"fresh-fixture-sentinel")
    original=subprocess.run; observed=[]
    def run(argv,**kwargs):
        observed.append(kwargs.get("env",{}))
        return original(argv,**kwargs)
    monkeypatch.setattr("agent_team.doctor.subprocess.run",run)
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1])
    assert report["exit_code"]==0 and observed and all(all(key not in env for key in credential_names) for env in observed)


def test_cli_and_report_scrub_known_secret_in_structured_evidence(monkeypatch,tmp_path,fixture):
    path,_=fixture
    monkeypatch.setenv("EXAMPLE_SECRET_TOKEN","chosen")
    destination=tmp_path/"diagnostic.json"
    result=CliRunner().invoke(cli,["doctor","--config",str(path),"--json","--report",str(destination)])
    assert result.exit_code==0
    assert "chosen" not in result.stdout and "chosen" not in destination.read_text()
    assert json.loads(result.stdout)==json.loads(destination.read_text())


def test_config_directory_is_diagnostic_not_usage_error(tmp_path):
    result=CliRunner().invoke(cli,["doctor","--config",str(tmp_path),"--json"])
    assert result.exit_code==1
    assert json.loads(result.stdout)["outcome"]=="blocked"


@pytest.mark.parametrize("scope",["pi","s6"])
@pytest.mark.parametrize("launched",[False,True],ids=["budget_before_launch","launched_timeout"])
@pytest.mark.parametrize("prior_failure",[False,True],ids=["no_prior_failure","prior_failure"])
def test_short_budget_probe_timeout_precedence(monkeypatch,tmp_path,fixture,scope,launched,prior_failure):
    import subprocess
    path,_=fixture
    binary=tmp_path/"probe"; binary.write_text("#!/bin/sh\nexit 0\n"); binary.chmod(0o700)
    service=tmp_path/"service"; service.mkdir()
    clock=[0.]
    monkeypatch.setattr("agent_team.doctor.time.monotonic",lambda:clock[0])
    monkeypatch.setattr("agent_team.pi_reconciler._validate_package",lambda p:{"minimum_pi_version":"0.0.0","assets":[]})
    original=subprocess.run; launched_probes=[]
    def run(argv,**kwargs):
        if argv[0]==str(binary):
            launched_probes.append(kwargs["timeout"])
            clock[0]=1.
            raise subprocess.TimeoutExpired(argv,kwargs["timeout"])
        return original(argv,**kwargs)
    monkeypatch.setattr("agent_team.doctor.subprocess.run",run)
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,*args,**kwargs):
            if not launched: clock[0]=1.
            return SimpleNamespace(status_code=200,json=lambda:{"status":"ok","ready":True})
    monkeypatch.setattr("agent_team.doctor.httpx.Client",lambda **kwargs:Client())
    report=diagnose(config_file=path,repo_root=Path(__file__).resolve().parents[1],timeout=1.,
        expected_revision="0"*40 if prior_failure else None,with_pi=scope=="pi",pi_binary=binary if scope=="pi" else None,
        supervisor="s6" if scope=="s6" else "none",service_dir=service if scope=="s6" else None,s6_svstat=binary if scope=="s6" else None)
    finding=next(c for c in report["checks"] if c["id"]==("pi_version" if scope=="pi" else "s6"))
    assert launched_probes==([1.] if launched else [])
    assert finding["status"]==("failed" if launched else "unverified")
    assert finding["reason"]==("pi_version_probe_failed" if scope=="pi" else "s6_status_probe_failed") if launched else finding["reason"]=="deadline_exhausted"
    assert report["exit_code"]==(1 if launched or prior_failure else 2)


def test_text_includes_safe_scopes_observations_details_and_remediation(monkeypatch,tmp_path):
    import agent_team.doctor as module
    monkeypatch.setenv("EXAMPLE_SECRET_TOKEN","secret-sentinel")
    report=dict(outcome="unverified",exit_code=2,config_file="external/config",config_source="yaml",state_dir="external/state",workspace_dir="external/work",
        selected_scopes={"core":True,"pi":True,"s6":False},runtime_observation=dict(lease_status="occupied",owner_pid=123,owner_started_at="2026-10-02T00:00:00+00:00",
        readiness_url="http://ready/ready",endpoint_ready=True,readiness_reason="endpoint_ready",selected_runtime_correlation="unknown"),
        checks=[dict(id="knowledge",status="unverified",reason="inspection_deferred",detail="Owner holds lease secret-sentinel",remediation="Stop writers safely secret-sentinel")])
    monkeypatch.setattr(module,"diagnose",lambda **kwargs:report)
    result=CliRunner().invoke(cli,["doctor","--with-pi"])
    assert result.exit_code==2 and "secret-sentinel" not in result.stdout
    for text in ('"pi": true','"s6": false',"lease: occupied","owner PID: 123","readiness URL: http://ready/ready","endpoint_ready: True","reason: endpoint_ready","selected runtime correlation: unknown","detail: Owner holds lease","remediation: Stop writers safely"):
        assert text in result.stdout
