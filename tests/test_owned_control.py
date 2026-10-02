"""Owned YAML reconciliation and verification contract cross-products."""
from config_doctor_evidence import config_doctor_evidence
import itertools
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from agent_team.config import resolve_configuration
from agent_team.control.configuration import atomic_write, OwnedYamlReconciler, ConfigReconcileError
from agent_team.control.discovery import OpenAIModelDiscovery, ModelDiscoveryError, resolve_role_models
from agent_team.control.manager import OwnedSwarmManager


@pytest.fixture(autouse=True)
def isolate(monkeypatch,tmp_path):
    for key in list(os.environ):
        if key.startswith(("AGENT_TEAM_","LLM_","PRINCIPAL_","MANAGER_","WORKER_","CURATOR_")):
            monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv("HOME",str(tmp_path/"home"))


def selection(tmp_path,model="auto",*,bootstrap=False):
    path=tmp_path/"config.yaml"
    if not bootstrap:
        path.write_text(yaml.safe_dump({"models":{"default":{"model":model,"base_url":"http://model/v1"}},"unrelated":{"preserve":"${UNKNOWN:-value}"}}))
    return resolve_configuration(path,allow_missing_named=True)


class Discovery:
    calls=[]
    def __init__(self,endpoint,**kwargs): self.base_url=endpoint
    def list_models(self):
        self.calls.append(self.base_url)
        return ["chosen"]
    def close(self): pass


class API:
    def __init__(self,available=True,drift=False,healthy=True,ready=True):
        self.available=available; self.drift=drift; self.healthy=healthy; self.is_ready=ready; self.calls=[]
    def models(self):
        self.calls.append("models")
        if not self.available: raise RuntimeError("sentinel secret must not leak")
        return {r:{"model":"old" if self.drift else "chosen","base_url":"http://model/v1"} for r in ("principal","manager","worker","curator")}
    def health(self): self.calls.append("health"); return {"status":"ok" if self.healthy else "bad"}
    def ready(self): self.calls.append("ready"); return {"status":"ok","ready":self.is_ready}


class Restart:
    def __init__(self,api,fail=False): self.calls=0; self.api=api; self.fail=fail
    def restart(self):
        self.calls+=1
        if self.fail: raise RuntimeError("secret must not leak")
        self.api.drift=False


@pytest.mark.parametrize("changed,verify,restart,capability,available,drift",list(itertools.product((False,True),repeat=6)))
def test_activation_cross_product(tmp_path,changed,verify,restart,capability,available,drift):
    chosen=selection(tmp_path,"auto" if changed else "chosen")
    original=chosen.config_file.read_bytes()
    api=API(available,drift); service=Restart(api) if capability else None
    manager=OwnedSwarmManager(chosen,api,service,discovery_factory=Discovery,verification_attempts=1)
    result=manager.reconcile(restart=restart,verify=verify)
    assert result.changed_files==([str(chosen.config_file)] if changed else [])
    if not changed: assert chosen.config_file.read_bytes()==original
    assert yaml.safe_load(chosen.config_file.read_bytes())["unrelated"]["preserve"]=="${UNKNOWN:-value}"
    if not verify:
        assert api.calls==[] and result.outcome=="verification_skipped" and result.exit_code==0
        assert result.restart_required==(True if changed else None) and not result.verified
        assert bool(service and service.calls)==bool(changed and restart and capability)
    elif not available:
        assert result.outcome=="verification_unavailable" and result.exit_code==1
        assert result.restart_required==(True if changed else None)
    elif drift and not (capability and restart):
        assert result.outcome=="restart_required" and result.exit_code==0 and result.restart_required is True
    else:
        assert result.outcome=="synchronized" and result.verified and result.restart_required is False


@pytest.mark.parametrize("health,ready",[(False,True),(True,False),(False,False)])
def test_unhealthy_observations_never_synchronized(tmp_path,health,ready):
    manager=OwnedSwarmManager(selection(tmp_path,"chosen"),API(healthy=health,ready=ready),discovery_factory=Discovery)
    result=manager.reconcile(restart=False)
    assert result.exit_code==1 and result.outcome=="verification_failed" and not result.verified


@pytest.mark.parametrize("verify",[False,True])
def test_restart_failure_retains_committed_yaml(tmp_path,verify):
    chosen=selection(tmp_path); api=API(); service=Restart(api,fail=True)
    result=OwnedSwarmManager(chosen,api,service,discovery_factory=Discovery).reconcile(verify=verify)
    assert result.exit_code==1 and result.outcome=="restart_failed" and result.commit_state=="committed"
    assert yaml.safe_load(chosen.config_file.read_bytes())["models"]["principal"]["model"]=="chosen"


@pytest.mark.parametrize("existing",[False,True])
def test_dry_run_no_files_or_runtime_probes(monkeypatch,tmp_path,existing):
    if not existing:
        monkeypatch.setenv("LLM_BASE_URL","http://model/v1")
    chosen=selection(tmp_path,bootstrap=not existing); api=API(); service=Restart(api)
    before={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result=OwnedSwarmManager(chosen,api,service,discovery_factory=Discovery).reconcile(dry_run=True)
    after={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert result.outcome=="planned" and not result.verified and api.calls==[] and service.calls==0 and before==after


@pytest.mark.parametrize("payload,reason",[({"data":[]},"model_not_found"),({"data":[{"id":"a"},{"id":"b"}]},"ambiguous_model"),({"data":[{"id":""}]},"invalid_model_response"),({"data":[{"id":"auto"}]},"invalid_model_response"),({"data":{}},"invalid_model_response"),({"data":[{}]},"invalid_model_response")])
def test_catalog_rejections(payload,reason):
    probe=OpenAIModelDiscovery("http://model/v1",client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload))))
    with pytest.raises(ModelDiscoveryError) as error: probe.detect_model()
    assert error.value.reason==reason


def test_duplicate_singleton_discovery_has_no_credentials():
    requests=[]
    def respond(request): requests.append(request); return httpx.Response(200,json={"data":[{"id":"chosen"},{"id":"chosen"}]})
    probe=OpenAIModelDiscovery("http://model/v1",api_key="inference-secret",client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert probe.detect_model()=="chosen" and "authorization" not in requests[0].headers


@pytest.mark.parametrize("url_credentials,header_credentials,client_auth",list(itertools.product((False,True),repeat=3)))
def test_discovery_excludes_all_injected_credentials(url_credentials,header_credentials,client_auth):
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={"data":[{"id":"chosen"}]})
    endpoint="http://url-user:fixture-password@model/v1" if url_credentials else "http://model/v1"
    headers={"Authorization":"Bearer fixture-password","X-API-Key":"fixture-password"} if header_credentials else {}
    with httpx.Client(transport=httpx.MockTransport(respond),headers=headers,
                      cookies={"control":"fixture-password"},
                      auth=("client-user","fixture-password") if client_auth else None) as client:
        probe=OpenAIModelDiscovery(endpoint,client=client,api_key="fixture-password",timeout=1.25)
        assert probe.detect_model()=="chosen"
        assert probe.base_url==endpoint
        assert len(requests)==1
        assert requests[0].method=="GET" and requests[0].url.path=="/v1/models"
        assert "authorization" not in requests[0].headers
        assert "x-api-key" not in requests[0].headers and "cookie" not in requests[0].headers
        assert requests[0].extensions["timeout"]==httpx.Timeout(1.25).as_dict()


def test_role_inheritance_and_yaml_environment_authority(monkeypatch,tmp_path):
    path=tmp_path/"config"
    path.write_text("models:\n  default:\n    model: shared\n    base_url: http://shared/v1\n  worker:\n    model: execution\n    base_url: http://execution/v1\n  curator:\n    model: null\n")
    monkeypatch.setenv("LLM_MODEL","ignored"); monkeypatch.setenv("LLM_BASE_URL","http://ignored")
    config=resolve_configuration(path).config
    assert config.models["principal"].model=="shared"
    assert config.models["curator"].model=="execution" and config.models["curator"].base_url=="http://execution/v1"
    with pytest.raises(ValueError): OwnedSwarmManager(resolve_configuration(path),API())._resolve("override")


@pytest.mark.parametrize("field",["model","base_url"])
def test_explicit_empty_role_is_invalid(tmp_path,field):
    path=tmp_path/"config"; path.write_text(yaml.safe_dump({"models":{"worker":{field:""}}}))
    with pytest.raises(ValueError): resolve_configuration(path)


@pytest.mark.parametrize("operation",["stage","directory"])
def test_atomic_write_faults_do_not_claim_rollback(monkeypatch,tmp_path,operation):
    path=tmp_path/"config"; path.write_bytes(b"old")
    fsync=os.fsync; calls=0
    def fault(fd):
        nonlocal calls
        calls+=1
        if calls==(1 if operation=="stage" else 2): raise OSError("fault")
        fsync(fd)
    monkeypatch.setattr(os,"fsync",fault)
    with pytest.raises(ConfigReconcileError) as error: atomic_write(path,b"new",expected=b"old")
    assert error.value.commit_state==("not_committed" if operation=="stage" else "uncertain")
    assert path.read_bytes()==(b"old" if operation=="stage" else b"new")
    assert list(tmp_path.iterdir())==[path]


def test_concurrent_edit_and_symlink_refused(tmp_path):
    path=tmp_path/"config"; path.write_bytes(b"external edit")
    with pytest.raises(ConfigReconcileError): atomic_write(path,b"replacement",expected=b"old")
    assert path.read_bytes()==b"external edit"
    link=tmp_path/"link"; link.symlink_to(path)
    with pytest.raises(ConfigReconcileError): atomic_write(link,b"replacement",expected=b"external edit")


def test_distinct_explicit_models_among_many_preserve_bytes(tmp_path):
    path=tmp_path/"config.yaml"
    path.write_text("models:\n  default:\n    base_url: http://model/v1\n    model: principal\n  manager:\n    model: manager\n  worker:\n    model: worker\n")
    chosen=resolve_configuration(path); original=path.read_bytes()
    class Many(Discovery):
        def list_models(self): return ["principal","manager","worker","extra"]
    api=API()
    api.models=lambda:{r:{"model":m.model,"base_url":m.base_url} for r,m in chosen.config.models.items()}
    result=OwnedSwarmManager(chosen,api,discovery_factory=Many).reconcile(restart=False)
    assert result.verified and path.read_bytes()==original and result.changed_files==[]


@pytest.mark.parametrize("fault",["endpoint","missing_role"])
def test_status_compares_id_and_endpoint_and_missing_roles(tmp_path,fault):
    api=API(); original=api.models
    def wrong():
        result=original()
        if fault=="endpoint": result["worker"]["base_url"]="http://other/v1"
        else: result.pop("worker")
        return result
    api.models=wrong
    status=OwnedSwarmManager(selection(tmp_path,"chosen"),api,discovery_factory=Discovery).inspect()
    assert not status.synchronized and status.model_drift and status.source_model is None and status.live_model is None


def test_auto_concretization_does_not_follow_replacement(tmp_path):
    chosen=selection(tmp_path); manager=OwnedSwarmManager(chosen,API(),discovery_factory=Discovery)
    assert manager.reconcile(verify=False).changed_files
    original=chosen.config_file.read_bytes()
    class Changed(Discovery):
        def list_models(self): return ["replacement"]
    with pytest.raises(ModelDiscoveryError):
        OwnedSwarmManager(resolve_configuration(chosen.config_file),API(),discovery_factory=Changed).reconcile(verify=False)
    assert chosen.config_file.read_bytes()==original


def test_environment_bootstrap_persists_only_owned_configuration(monkeypatch,tmp_path):
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR",str(tmp_path/"state")); monkeypatch.setenv("AGENT_TEAM_WORKSPACE_DIR",str(tmp_path/"workspace"))
    monkeypatch.setenv("LLM_BASE_URL","http://model/v1"); monkeypatch.setenv("LLM_API_KEY","must-not-persist")
    chosen=resolve_configuration()
    result=OwnedSwarmManager(chosen,API(),discovery_factory=Discovery).reconcile(verify=False)
    assert result.outcome=="verification_skipped"
    data=yaml.safe_load(chosen.config_file.read_bytes())
    assert data["runtime"]=={"state_dir":str(tmp_path/"state"),"workspace_dir":str(tmp_path/"workspace")}
    assert set(data)=={"runtime","models"} and b"must-not-persist" not in chosen.config_file.read_bytes()


def test_reconciliation_refuses_symlink_named_selector(tmp_path):
    chosen=selection(tmp_path); link=tmp_path/"link.yaml"; link.symlink_to(chosen.config_file)
    original=chosen.config_file.read_bytes()
    manager=OwnedSwarmManager(resolve_configuration(link),API(),discovery_factory=Discovery)
    result=manager.reconcile(verify=False)
    assert result.exit_code==1 and chosen.config_file.read_bytes()==original


def test_cross_process_reconciliation_lock_contention(tmp_path):
    chosen=selection(tmp_path); original=chosen.config_file.read_bytes()
    lock=chosen.config_file.with_name(chosen.config_file.name+".reconcile.lock")
    program="import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
    child=subprocess.Popen([sys.executable,"-c",program,str(lock)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={"HOME":str(tmp_path),"PATH":os.defpath})
    try:
        import select
        assert select.select([child.stdout],[],[],5)[0],"lock child never reached barrier"
        assert child.stdout.readline().strip()=="locked"
        result=OwnedSwarmManager(chosen,API(),discovery_factory=Discovery).reconcile(verify=False)
        assert result.exit_code==1 and result.commit_state=="not_committed" and chosen.config_file.read_bytes()==original
    finally:
        child.communicate("release\n",timeout=5)
    assert child.returncode==0


def test_external_edit_detected_under_lock(monkeypatch,tmp_path):
    chosen=selection(tmp_path)
    original_render=OwnedYamlReconciler.render
    external=b"models:\n  default:\n    model: changed\n    base_url: http://model/v1\n"
    def edit(self,models):
        candidate=original_render(self,models)
        self.path.write_bytes(external)
        return candidate
    monkeypatch.setattr(OwnedYamlReconciler,"render",edit)
    result=OwnedSwarmManager(chosen,API(),discovery_factory=Discovery).reconcile(verify=False)
    assert result.exit_code==1 and chosen.config_file.read_bytes()==external


def test_persistence_failure_never_restarts(monkeypatch,tmp_path):
    chosen=selection(tmp_path); original=chosen.config_file.read_bytes(); api=API(); service=Restart(api)
    monkeypatch.setattr(os,"replace",lambda *a:(_ for _ in ()).throw(PermissionError("fault")))
    result=OwnedSwarmManager(chosen,api,service,discovery_factory=Discovery).reconcile()
    assert result.exit_code==1 and result.outcome=="persistence_failed" and service.calls==0 and chosen.config_file.read_bytes()==original


@pytest.mark.parametrize("changed",[False,True])
def test_initial_api_outage_changed_can_restart_unchanged_cannot(tmp_path,changed):
    api=API(available=False)
    class Recover(Restart):
        def restart(self): super().restart(); self.api.available=True
    service=Recover(api)
    result=OwnedSwarmManager(selection(tmp_path,"auto" if changed else "chosen"),api,service,discovery_factory=Discovery,verification_attempts=2,verification_interval=0).reconcile()
    assert service.calls==(1 if changed else 0)
    assert result.outcome==("synchronized" if changed else "verification_unavailable")


def test_discovery_once_per_endpoint_trailing_slash_only(monkeypatch,tmp_path):
    path=tmp_path/"config"
    path.write_text("models:\n  default:\n    model: auto\n    base_url: http://model/v1\n  manager:\n    base_url: http://model/v1/\n  worker:\n    base_url: http://second/v1\n")
    config=resolve_configuration(path).config; Discovery.calls=[]
    monkeypatch.setattr("agent_team.control.discovery.OpenAIModelDiscovery",Discovery)
    # Supply the shared singleton policy on the fake class too.
    monkeypatch.setattr(Discovery,"require_single",staticmethod(lambda ids:ids[0]),raising=False)
    resolved,catalogs=resolve_role_models(config)
    assert Discovery.calls==["http://model/v1","http://second/v1"]
    assert resolved.models["curator"].model=="chosen" and len(catalogs)==2


def test_bounded_eventual_convergence(tmp_path):
    api=API(); observed=0
    original=api.models
    def delayed():
        nonlocal observed
        observed+=1; api.drift=observed<4
        return original()
    api.models=delayed
    service=Restart(api)
    sleeps=[]
    result=OwnedSwarmManager(selection(tmp_path),api,service,discovery_factory=Discovery,
        verification_attempts=4,verification_interval=.01,sleep=sleeps.append).reconcile()
    assert result.verified and observed==4 and len(sleeps)==2


def test_unchanged_reconciliation_owns_lock_without_replacement(monkeypatch,tmp_path):
    chosen=selection(tmp_path,"chosen"); original=chosen.config_file.read_bytes()
    def forbidden(*a,**kw): raise AssertionError("no-op must not stage or replace YAML")
    monkeypatch.setattr("agent_team.control.configuration.atomic_write",forbidden)
    result=OwnedSwarmManager(chosen,API(),discovery_factory=Discovery).reconcile()
    assert result.verified and chosen.config_file.read_bytes()==original
    assert chosen.config_file.with_name("config.yaml.reconcile.lock").is_file()


@pytest.mark.parametrize("enabled,shared,curator",list(itertools.product((False,True),repeat=3)))
def test_only_active_curator_endpoint_is_probed(monkeypatch,tmp_path,enabled,shared,curator):
    path=tmp_path/"config"
    path.write_text(yaml.safe_dump({"models":{"default":{"base_url":"http://model/v1","model":"chosen"},"curator":{"base_url":"http://curator/v1","model":"chosen"}},"memory":{"enabled":enabled,"shared_knowledge":shared,"curator_enabled":curator}}))
    config=resolve_configuration(path).config; Discovery.calls=[]
    monkeypatch.setattr("agent_team.control.discovery.OpenAIModelDiscovery",Discovery)
    monkeypatch.setattr(Discovery,"require_single",staticmethod(lambda ids:ids[0]),raising=False)
    resolve_role_models(config)
    assert Discovery.calls==(["http://model/v1","http://curator/v1"] if enabled and shared and curator else ["http://model/v1"])


def test_placeholder_environment_change_prevents_stale_commit(monkeypatch,tmp_path):
    monkeypatch.setenv("SELECTED_ENDPOINT","http://model/v1")
    path=tmp_path/"config.yaml"; path.write_text("models:\n  default:\n    model: auto\n    base_url: ${SELECTED_ENDPOINT}\n")
    chosen=resolve_configuration(path); original=path.read_bytes()
    class ChangeEnvironment(Discovery):
        def list_models(self):
            monkeypatch.setenv("SELECTED_ENDPOINT","http://changed/v1")
            return ["chosen"]
    result=OwnedSwarmManager(chosen,API(),discovery_factory=ChangeEnvironment).reconcile(verify=False)
    assert result.exit_code==1 and path.read_bytes()==original
