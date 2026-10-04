"""Real forced foreground owners, surviving confined children, and successors."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import time

import pytest

from agent_team.persistence import RuntimeAlreadyRunningError, RuntimeLease
from test_child_confinement import CHILD_PROGRAM, manifest as target_manifest
from test_foreground_contract import OWNER, api, configuration, endpoint, finite_case_deadline, owner


# Continue the same adversarial probes after the original owner dies and while
# the successor is actually listening. The scratch control file conveys only
# the successor's public PID/port; it grants no durable commit capability.
CONTINUOUS_CHILD = CHILD_PROGRAM[:CHILD_PROGRAM.index('count=0\nwhile True:')] + r'''
count=0
while True:
    count+=1
    peer=payload['owner_pid']
    try:
        with open('successor-target.json') as stream: peer=json.load(stream)['pid']
    except (OSError,ValueError): pass
    repeated={}
    def repeat(name,fn):
        try: fn(); repeated[name]='allowed'
        except (OSError,PermissionError): repeated[name]='denied'
    for name,path in payload['targets'].items():
        repeat('direct_'+name,lambda path=path: open(path,'ab').write(b'forbidden surviving child mutation'))
        alias=os.path.join(os.getcwd(),name+'-symlink')
        repeat('symlink_'+name,lambda alias=alias: open(alias,'ab').write(b'forbidden surviving alias mutation'))
        link=os.path.join(os.getcwd(),name+'-hardlink')
        repeat('hardlink_'+name,lambda path=path,link=link: os.link(path,link))
        if os.path.exists(link): repeat('hardlink_write_'+name,lambda link=link: open(link,'ab').write(b'forbidden surviving hardlink mutation'))
    repeat('successor_process_control',lambda:os.kill(peer,signal.SIGCONT))
    task=ctypes.c_uint()
    repeated['successor_task_port']='denied' if lib.task_for_pid(own,peer,ctypes.byref(task)) else 'allowed'
    def buffered_commit():
        body=json.dumps({'client_key':'old-child-buffered','metadata':{'generation':payload['generation']}}).encode()
        with socket.create_connection(('127.0.0.1',payload['port']),timeout=.1) as connection:
            request=b'POST /sessions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-api-token\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body
            connection.sendall(request)
            connection.recv(512)
    repeat('buffered_http_commit',buffered_commit)
    temporary='heartbeat-next'
    with open(temporary,'w') as stream: json.dump({'count':count,'peer_pid':peer,'attempts':repeated},stream)
    os.replace(temporary,'heartbeat')
    time.sleep(.02)
'''


INJECTION = r'''
import asyncio, hashlib, json, os, sys
from contextlib import asynccontextmanager
from pathlib import Path
import agent_team.app as boundary
original_lifespan=boundary.app.router.lifespan_context
scenario=os.environ['FIXTURE_CHILD_SCENARIO']
buffer_path=Path(os.environ['FIXTURE_CHILD_BUFFER'])
def emit(name,**fields):
    print(json.dumps({'event':'fixture_barrier','name':name,'pid':os.getpid(),**fields}),flush=True)
@asynccontextmanager
async def fixture_lifespan(app):
    async with original_lifespan(app):
        lease=boundary.runtime_lease; generation=lease.generation
        state=boundary.config.runtime.state_dir; workspace=boundary.config.runtime.workspace_dir
        if scenario=='owner':
            targets={'state':state/'child-state-marker.bin','store':state/'knowledge'/'knowledge.db',
                     'workspace':workspace/'child-project-marker.bin','artifact':workspace/'child-artifact-marker.bin'}
            assert targets['store'].is_file()
            for name,path in targets.items():
                if name!='store':
                    lease.check_generation(generation); path.write_bytes(b'preserved durable fixture value\n')
            payload={'targets':{name:str(path) for name,path in targets.items()},'inherited_fd':99,
                     'owner_pid':os.getpid(),'port':int(os.environ['FIXTURE_PORT']),
                     'unix_path':os.environ['FIXTURE_UNIX_PATH'],'shm_name':'/agent-team-forced-'+str(os.getpid()),
                     'generation':generation}
            writable=os.open(targets['state'],os.O_WRONLY)
            try: os.dup2(writable,99,inheritable=True)
            finally: os.close(writable)
            worker=await boundary.engine.registry.register_worker('software-engineer')
            task=await boundary.engine.registry.register_task(worker)
            try:
                child=await boundary.engine.process_manager.spawn(worker,task,[str(Path(sys.executable).resolve()),'-u','-c',CONTINUOUS_CHILD,json.dumps(payload)])
            finally: os.close(99)
            line=await asyncio.wait_for(child.process.stdout.readline(),3)
            if not line: raise AssertionError('required confined child failed to start')
            barrier=json.loads(line)
            assert all(value=='denied' for value in barrier['attempts'].values())
            info={'pid':child.pid,'group':child.process_group,'scratch':barrier['scratch'],
                  'generation':generation,'targets':payload['targets']}
            fd=os.open(buffer_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:
                json.dump(info,stream); stream.flush(); os.fsync(stream.fileno())
            safe=dict(barrier); safe['scratch']=safe['scratch'].replace(generation,'<generation>')
            emit('child',child=safe)
            async def resistant_close():
                emit('shutdown')
                while True:
                    try: await asyncio.sleep(.05)
                    except asyncio.CancelledError: pass
            boundary.engine.close=resistant_close
        else:
            info=json.loads(buffer_path.read_text()); old=info['generation']
            assert not boundary.engine.registry.snapshot_workers()
            store=boundary.session_store; original_guard=store.write_guard
            store.write_guard=lambda:lease.check_generation(old)
            try:
                try: store.create(client_key='old-buffered-generation'); old_rejected=False
                except RuntimeError: old_rejected=True
            finally: store.write_guard=original_guard
            assert old_rejected and store.get_by_client_key('old-buffered-generation') is None
            assert store.get_by_client_key('old-child-buffered') is None
            record=store.create(client_key='successor-current-generation')
            assert store.get(record['session_id'])['client_key']=='successor-current-generation'
            artifacts=boundary.engine.artifact_store; original_artifact_guard=artifacts.write_guard
            artifacts.write_guard=lambda:lease.check_generation(old)
            try:
                try: artifacts.write_manifest('old-buffered-generation',[]); old_artifact_rejected=False
                except RuntimeError: old_artifact_rejected=True
            finally: artifacts.write_guard=original_artifact_guard
            assert old_artifact_rejected and not (state/'artifacts'/'old-buffered-generation').exists()
            committed=artifacts.write_manifest('successor-current-generation',[])
            emit('successor_guard',old_rejected=old_rejected,current_committed=True,
                 old_artifact_rejected=old_artifact_rejected,current_artifact_committed=committed.is_file(),
                 session_id=record['session_id'],old_generation_digest=hashlib.sha256(old.encode()).hexdigest(),
                 new_generation_digest=hashlib.sha256(generation.encode()).hexdigest(),old_scratch_imported=False)
        yield
boundary.app.router.lifespan_context=fixture_lifespan
'''


PROGRAM = 'CONTINUOUS_CHILD='+repr(CONTINUOUS_CHILD)+'\n'+INJECTION+'\n'+OWNER


class ScratchFile:
    """Keep token-containing fixture paths out of pytest argument rendering."""
    def __init__(self,path): self.path=path
    def __repr__(self): return '<private generation scratch file>'
    def read_text(self): return self.path.read_text()
    def write_json(self,value):
        temporary=self.path.with_name(self.path.name+'-next')
        try:
            temporary.write_text(json.dumps(value)); os.replace(temporary,self.path)
        except OSError:
            raise AssertionError('controller could not publish the safe scratch peer barrier') from None


def heartbeat(path, *, minimum=0, peer=None, timeout=2):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try: value=json.loads(path.read_text())
        except (OSError,ValueError): value={}
        if value.get('count',0)>minimum and (peer is None or value.get('peer_pid')==peer):
            unexpected={name:result for name,result in value['attempts'].items() if result!='denied'}
            assert not unexpected,'Unexpected surviving-child capabilities: '+json.dumps(unexpected,sort_keys=True)
            return value
        time.sleep(.01)
    raise AssertionError('surviving child did not prove continuing scratch-only writes')


def durable_manifest(state,workspace):
    result={}
    for root,label in ((state,'state'),(workspace,'workspace')):
        if not root.exists(): continue
        for path in root.rglob('*'):
            relative=path.relative_to(root)
            if label=='state' and (relative.parts[0]=='scratch' or path.name in {'runtime.lock','runtime-owner.json'}): continue
            if path.is_file(): result[label+'/'+str(relative)]=target_manifest({'file':path})['file']
    return result


def kill_and_observe_orphan(pid,group):
    try: os.killpg(group,signal.SIGKILL)
    except ProcessLookupError: return {'pid':pid,'group':group,'already_gone':True,'exit_observed':True}
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        try: os.kill(pid,0)
        except ProcessLookupError:
            return {'pid':pid,'group':group,'controller_sent_sigkill':True,'exit_observed':True,
                    'reaper':'OS reparenting reaper; controller cannot waitpid a reparented Darwin child'}
        time.sleep(.01)
    raise AssertionError('controller could not observe confined orphan termination')


@pytest.mark.parametrize('mode',['shutdown-timeout','second-signal'])
@pytest.mark.required_platform('darwin')
def test_forced_foreground_preserves_durable_authority_with_surviving_child(tmp_path,request,mode):
    began=time.monotonic(); info=None; cleanup=None; owners=[]
    resources=ExitStack()
    try:
        channel=resources.enter_context(tempfile.TemporaryDirectory(prefix='at-forced-',dir=str(Path('/tmp').resolve())))
        unix=resources.enter_context(socket.socket(socket.AF_UNIX,socket.SOCK_STREAM))
        unix_path=Path(channel)/'s'; unix.bind(str(unix_path)); unix.listen(1)
        buffer=tmp_path/'generation-buffer.json'
        with endpoint() as (catalog,url):
            config,state=configuration(tmp_path,url,knowledge=True); workspace=tmp_path/'workspace'
            additions={'FIXTURE_CHILD_SCENARIO':'owner','FIXTURE_CHILD_BUFFER':str(buffer),'FIXTURE_UNIX_PATH':str(unix_path)}
            with owner(config,tmp_path/'owner-home',program=PROGRAM,env_additions=additions,startup=5,shutdown=5) as original:
                barrier=original.wait('fixture_barrier',barrier='child')
                info=json.loads(buffer.read_text()); targets={name:Path(path) for name,path in info['targets'].items()}
                assert buffer.stat().st_mode & 0o777==0o600
                original.wait('serve_ready')
                ticks=ScratchFile(Path(info['scratch'])/'heartbeat')
                control=ScratchFile(Path(info['scratch'])/'successor-target.json')
                first=heartbeat(ticks,peer=original.pid)
                os.kill(info['pid'],signal.SIGTERM)
                resistant=heartbeat(ticks,minimum=first['count'],peer=original.pid)
                before=durable_manifest(state,workspace); target_before=target_manifest(targets)
                metadata_digest=hashlib.sha256((state/'runtime-owner.json').read_bytes()).hexdigest()
                sent=[original.send(signal.SIGTERM)]
                original.wait('serve_stopping'); original.wait('fixture_barrier',barrier='shutdown')
                with pytest.raises(RuntimeAlreadyRunningError): RuntimeLease(state/'runtime.lock').acquire()
                assert hashlib.sha256((state/'runtime-owner.json').read_bytes()).hexdigest()==metadata_digest
                if mode=='second-signal': sent.append(original.send(signal.SIGINT))
                owner_proof=original.finish(1,timeout=8); owners.append(owner_proof)
                assert not any(item['event']=='serve_stopped' for item in original.events)
                assert any(item['event']=='serve_failed' and item['reason']=='shutdown_failed' for item in original.events)
                # A reaped/dead PID is no longer a live authority probe: some
                # kernels accept a no-op signal to a dying process. Switch to
                # the still-live controller before observing post-exit denial.
                control.write_json({'pid':os.getpid()})
                after_exit=heartbeat(ticks,minimum=resistant['count'],peer=os.getpid())
                assert durable_manifest(state,workspace)==before
                assert target_manifest(targets)==target_before
                forced_after=durable_manifest(state,workspace)
            successor_additions={**additions,'FIXTURE_CHILD_SCENARIO':'successor'}
            with owner(config,tmp_path/'successor-home',program=PROGRAM,env_additions=successor_additions,
                       port=original.port,startup=5,shutdown=5) as next_owner:
                guarded=next_owner.wait('fixture_barrier',barrier='successor_guard')
                next_owner.wait('serve_ready')
                assert guarded['old_rejected'] and guarded['old_artifact_rejected']
                assert guarded['current_committed'] and guarded['current_artifact_committed']
                assert guarded['old_generation_digest']!=guarded['new_generation_digest']
                control.write_json({'pid':next_owner.pid})
                successor_tick=heartbeat(ticks,minimum=after_exit['count'],peer=next_owner.pid)
                with api(next_owner) as client:
                    assert client.get('/ready').json()['ready'] is True
                    assert client.get('/sessions/'+guarded['session_id']).status_code==200
                assert target_manifest(targets)==target_before
                assert len(list((state/'sessions').glob('*.json')))==1
                assert not (state/'artifacts'/'old-buffered-generation').exists()
                next_owner.send(signal.SIGTERM); owners.append(next_owner.finish(143))
            final_targets=target_manifest(targets); assert final_targets==target_before
            requests=list(catalog.requests)
            assert len(requests)==2 and all(item['authorization'] is False for item in requests)
    finally:
        try:
            if info is not None: cleanup=kill_and_observe_orphan(info['pid'],info['group'])
        finally: resources.close()
    duration=time.monotonic()-began; assert duration<20
    request.node.user_properties.append(('lifecycle_evidence',{
        'schema_version':1,'id':'lifecycle.child.forced.'+mode,'duration_seconds':duration,'deadline_seconds':20,
        'expected':{'owner_exit':1,'successor_exit':143,'old_commits_rejected':True,'old_durable_effects':False},
        'observed':{'owners':owners,'signals':sent,'child':barrier['child'],'term_resistant':True,
                    'heartbeat_after_owner_exit':after_exit,'heartbeat_under_successor':successor_tick,
                    'successor_guard':guarded,'requests':requests},
        'before':before,'after':forced_after,'durable_targets_before':target_before,'durable_targets_after':final_targets,
        'cleanup':{'owners_reaped':[item['pid'] for item in owners],'confined_orphan':cleanup,
                   'owner_reaped_all_descendants':False},'proofs_passed':True,
        'native_bootstrap_profile_sha256':hashlib.sha256(Path('/System/Library/Sandbox/Profiles/dyld-support.sb').read_bytes()).hexdigest(),
        'platform_qualification':'Exact Darwin host; Apple private dyld bootstrap policy; OS reparenting reaper observed.'}))
