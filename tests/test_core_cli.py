"""Resolved core authority replaces former deployment fallback semantics."""
from config_doctor_evidence import config_doctor_evidence
import itertools
import os
import pytest
import yaml
from click.testing import CliRunner
from agent_team.cli import cli, build_model_discovery, build_swarm_manager
from agent_team.config import resolve_configuration


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    for key in list(os.environ):
        if key.startswith(("AGENT_TEAM_","LLM_","PRINCIPAL_","MANAGER_","WORKER_","CURATOR_")):
            monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv("HOME",str(tmp_path/"home"))
    monkeypatch.setenv("LLM_BASE_URL","http://model/v1")


@pytest.mark.parametrize("state_source,workspace_source",list(itertools.product(("flag","yaml","env","default"),repeat=2)))
def test_per_field_authority(monkeypatch,tmp_path,state_source,workspace_source):
    path=tmp_path/"config.yaml"
    values={"state_dir":str(tmp_path/"yaml-state"),"workspace_dir":str(tmp_path/"yaml-work")}
    for key,source in (("state_dir",state_source),("workspace_dir",workspace_source)):
        if source not in {"flag","yaml"}: values.pop(key)
        if source in {"flag","yaml","env"}: monkeypatch.setenv("AGENT_TEAM_"+key.upper(),str(tmp_path/("env-"+key)))
    path.write_text(yaml.safe_dump({"runtime":values,"models":{"default":{"base_url":"http://yaml/v1"}}}))
    kwargs={key:tmp_path/("flag-"+key) for key,source in (("state_dir",state_source),("workspace_dir",workspace_source)) if source=="flag"}
    selection=resolve_configuration(path,**kwargs)
    state=tmp_path/("flag-state_dir" if state_source=="flag" else "yaml-state" if state_source=="yaml" else "env-state_dir") if state_source!="default" else tmp_path/"home/.agent-team"
    work=tmp_path/("flag-workspace_dir" if workspace_source=="flag" else "yaml-work" if workspace_source=="yaml" else "env-workspace_dir") if workspace_source!="default" else state/"workspace"
    assert selection.config.runtime.state_dir==state.resolve()
    assert selection.config.runtime.workspace_dir==work.resolve()


@pytest.mark.parametrize("selector",["flag","environment","canonical"])
def test_config_selection_and_named_missing(monkeypatch,tmp_path,selector):
    state=tmp_path/"state"; monkeypatch.setenv("AGENT_TEAM_STATE_DIR",str(state))
    path=tmp_path/"named.yaml" if selector!="canonical" else state/"config/config.yaml"
    kwargs={"config_file":path} if selector=="flag" else {}
    if selector=="environment": monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE",str(path))
    if selector!="canonical":
        with pytest.raises(ValueError,match="missing"): resolve_configuration(**kwargs)
    assert resolve_configuration(**kwargs,allow_missing_named=True).config_file==path


def test_config_flag_beats_environment(monkeypatch,tmp_path):
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE",str(tmp_path/"wrong"))
    path=tmp_path/"chosen"; path.write_text("{}")
    assert resolve_configuration(path).config_file==path


@pytest.mark.parametrize("primary,compatibility",[(True,True),(False,True),(False,False)])
def test_state_environment_compatibility(monkeypatch,tmp_path,primary,compatibility):
    if primary: monkeypatch.setenv("AGENT_TEAM_STATE_DIR",str(tmp_path/"primary"))
    if compatibility: monkeypatch.setenv("AGENT_TEAM_HOME",str(tmp_path/"compat"))
    expected=tmp_path/"primary" if primary else tmp_path/"compat/.agent-team" if compatibility else tmp_path/"home/.agent-team"
    assert resolve_configuration().config.runtime.state_dir==expected


@pytest.mark.parametrize("field,value,valid",[("workspace_dir",None,True),("state_dir",None,False),("state_dir","relative",False),("workspace_dir","relative",False)])
def test_yaml_path_null_and_absolute_rules(tmp_path,field,value,valid):
    path=tmp_path/"config"; path.write_text(yaml.safe_dump({"runtime":{field:value}}))
    if valid: assert resolve_configuration(path).config.runtime.workspace_dir==tmp_path/"home/.agent-team/workspace"
    else:
        with pytest.raises(ValueError): resolve_configuration(path)


def test_environment_state_escape_refused(monkeypatch,tmp_path):
    state=tmp_path/"state"; (state/"config").mkdir(parents=True)
    (state/"config/config.yaml").write_text(yaml.safe_dump({"runtime":{"state_dir":str(tmp_path/"other")}}))
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR",str(state))
    with pytest.raises(ValueError,match="conflicts"): resolve_configuration()
    assert resolve_configuration(state_dir=state).config.runtime.state_dir==state


@pytest.mark.parametrize("selector",["flag","environment"])
def test_named_config_inside_environment_state_cannot_escape(monkeypatch,tmp_path,selector):
    state=tmp_path/"state"; state.mkdir()
    path=state/"custom.yaml"; path.write_text(yaml.safe_dump({"runtime":{"state_dir":str(tmp_path/"other")}}))
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR",str(state))
    kwargs={"config_file":path} if selector=="flag" else {}
    if selector=="environment": monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE",str(path))
    with pytest.raises(ValueError,match="conflicts"): resolve_configuration(**kwargs)


@pytest.mark.parametrize("source",["flag","environment"])
def test_relative_paths_follow_invocation(monkeypatch,tmp_path,source):
    monkeypatch.chdir(tmp_path)
    kwargs={"state_dir":"relative","workspace_dir":"work"} if source=="flag" else {}
    if source=="environment":
        monkeypatch.setenv("AGENT_TEAM_STATE_DIR","relative"); monkeypatch.setenv("AGENT_TEAM_WORKSPACE_DIR","work")
    config=resolve_configuration(**kwargs).config
    assert config.runtime.state_dir==tmp_path/"relative" and config.runtime.workspace_dir==tmp_path/"work"


def test_init_named_exact_idempotent_and_backup(tmp_path):
    path=tmp_path/"named.yaml"; state=tmp_path/"state"; work=tmp_path/"work"
    args=["init","--config",str(path),"--state-dir",str(state),"--workspace-dir",str(work)]
    runner=CliRunner(); result=runner.invoke(cli,args)
    assert result.exit_code==0,result.output
    original=path.read_bytes()
    assert yaml.safe_load(original)["runtime"]["state_dir"]==str(state)
    assert not (state/"knowledge/knowledge.db").exists()
    assert runner.invoke(cli,args).exit_code==0 and path.read_bytes()==original
    mismatch=runner.invoke(cli,[*args[:-2],"--workspace-dir",str(tmp_path/"new-work")])
    assert mismatch.exit_code==1 and path.read_bytes()==original
    assert runner.invoke(cli,[*args[:-2],"--workspace-dir",str(tmp_path/"new-work"),"--overwrite-config"]).exit_code==0
    backups=list(tmp_path.glob("named.yaml.backup-*"))
    assert len(backups)==1 and backups[0].read_bytes()==original


def test_init_refuses_omitted_explicit_path_and_symlink(tmp_path):
    path=tmp_path/"config"; path.write_text("{}")
    result=CliRunner().invoke(cli,["init","--config",str(path),"--state-dir",str(tmp_path/"state")])
    assert result.exit_code==1 and "persist explicit" in result.output
    link=tmp_path/"link"; link.symlink_to(path)
    assert CliRunner().invoke(cli,["init","--config",str(link)]).exit_code==1


def test_checkout_paths_and_config_mutation_refused(monkeypatch,tmp_path):
    monkeypatch.setenv("AGENT_TEAM_PROJECT_ROOT",str(tmp_path))
    with pytest.raises(ValueError): resolve_configuration(state_dir=tmp_path/"state")
    config=tmp_path/"readable-config"; config.write_text("{}")
    outside=tmp_path.parent/(tmp_path.name+"-state")
    assert resolve_configuration(config,state_dir=outside).config_file==config
    assert CliRunner().invoke(cli,["init","--config",str(config),"--state-dir",str(outside)]).exit_code==1


def test_detect_uses_common_endpoint_and_allows_distinct_roles(monkeypatch,tmp_path):
    path=tmp_path/"config"; path.write_text("models:\n  default:\n    base_url: http://one/v1\n  worker:\n    base_url: http://two/v1\n")
    monkeypatch.setenv("AGENT_TEAM_CONFIG_FILE",str(path))
    discovery=build_model_discovery(); assert discovery.base_url=="http://one/v1"; discovery.close()


def test_environment_detect_uses_common_not_principal_override(monkeypatch):
    monkeypatch.setenv("PRINCIPAL_BASE_URL","http://principal/v1")
    discovery=build_model_discovery()
    assert discovery.base_url=="http://model/v1"
    discovery.close()


@pytest.mark.parametrize("raw",["[1,2]","runtime: []","runtime: null","models: []","models: {unexpected: {}}","runtime: {state_dir: ${BROKEN}"])
def test_malformed_selected_configuration_fails_closed(tmp_path,raw):
    path=tmp_path/"config"; path.write_text(raw)
    with pytest.raises(Exception): resolve_configuration(path)


def test_nonregular_or_dangling_canonical_config_never_falls_back(monkeypatch,tmp_path):
    state=tmp_path/"state"; config=state/"config/config.yaml"; config.parent.mkdir(parents=True)
    monkeypatch.setenv("AGENT_TEAM_STATE_DIR",str(state))
    config.symlink_to(tmp_path/"missing")
    with pytest.raises(ValueError,match="dangling"): resolve_configuration()
    config.unlink(); os.mkfifo(config)
    with pytest.raises(ValueError,match="regular"): resolve_configuration()


def test_optional_adapter_inputs_not_implicit(monkeypatch,tmp_path):
    monkeypatch.setenv("AGENT_TEAM_SERVICE_DIR",str(tmp_path/"absent"))
    assert build_swarm_manager().service is None
    result=CliRunner().invoke(cli,["doctor","--pi-binary",str(tmp_path/"pi")])
    assert result.exit_code==2 and "requires --with-pi" in result.output


def test_serve_delegates_frozen_options(monkeypatch,tmp_path):
    calls=[]
    monkeypatch.setattr("agent_team.foreground.run_serve",lambda **kwargs:calls.append(kwargs) or 0)
    result=CliRunner().invoke(cli,["serve","--config",str(tmp_path/"missing"),"--state-dir",str(tmp_path/"state"),"--startup-timeout","5"])
    assert result.exit_code==0,result.output
    assert calls[0]["config_file"]==tmp_path/"missing" and calls[0]["startup_timeout"]==5


@pytest.mark.parametrize("options",[["--port","not-an-integer"],["--port","0"],["--startup-timeout","not-a-number"],["--shutdown-timeout","nan"]])
def test_serve_invalid_semantics_are_preflight_failures(options):
    import json
    result=CliRunner().invoke(cli,["serve",*options])
    assert result.exit_code==1
    event=json.loads(result.stderr)
    assert event["event"]=="serve_failed" and event["reason"]=="config_error" and event["phase"]=="preflight"
