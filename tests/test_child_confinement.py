"""Host-executed child capability and tracked-process safety evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import subprocess
import tempfile
from contextlib import ExitStack
from functools import wraps

import pytest

from agent_team.persistence import RuntimeLease, SessionStore
from agent_team.runtime.lifecycle import ProcessHandle, ProcessManager, TaskRegistry, WorkerState


# This program imports no production modules or fixture files. Its only output
# channel is a diagnostic pipe. It ignores TERM and keeps writing old scratch.
CHILD_PROGRAM = r'''
import ctypes, hashlib, json, os, signal, socket, sys, time
payload=json.loads(sys.argv[1]); attempts={}; signal.signal(signal.SIGTERM, signal.SIG_IGN)
def attempt(name, fn):
    try: fn(); attempts[name]='allowed'
    except (OSError, PermissionError): attempts[name]='denied'
for name,path in payload['targets'].items():
    def write(path=path):
        with open(path,'ab') as stream: stream.write(b'forbidden durable mutation')
    attempt('direct_'+name,write)
    alias=os.path.join(os.getcwd(),name+'-symlink')
    try: os.symlink(path,alias)
    except OSError: attempts['symlink_'+name]='denied'
    else: attempt('symlink_'+name,lambda alias=alias: open(alias,'ab').write(b'forbidden alias mutation'))
    link=os.path.join(os.getcwd(),name+'-hardlink')
    attempt('hardlink_'+name,lambda path=path,link=link: os.link(path,link))
    if os.path.exists(link): attempt('hardlink_write_'+name,lambda link=link: open(link,'ab').write(b'forbidden hardlink mutation'))
attempt('inherited_fd',lambda: os.write(payload['inherited_fd'],b'forbidden inherited mutation'))
attempt('stdin_write',lambda: os.write(0,b'forbidden stdin mutation'))
attempt('owner_signal',lambda: os.kill(payload['owner_pid'],signal.SIGCONT))
attempt('network',lambda: socket.create_connection(('127.0.0.1',payload['port']),timeout=.2))
attempt('unix_ipc',lambda: socket.socket(socket.AF_UNIX,socket.SOCK_STREAM).connect(payload['unix_path']))
attempt('anonymous_fd_ipc',lambda: socket.socketpair())
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
task=ctypes.c_uint(); own=lib.mach_task_self()
attempts['task_for_pid']='denied' if lib.task_for_pid(own,payload['owner_pid'],ctypes.byref(task)) else 'allowed'
bootstrap=ctypes.c_uint.in_dll(lib,'bootstrap_port').value; port=ctypes.c_uint()
attempts['mach_lookup']='denied' if lib.bootstrap_look_up(bootstrap,b'com.apple.cfprefsd.daemon',ctypes.byref(port)) else 'allowed'
lib.shm_open.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_uint]
attempts['posix_shm']='denied' if lib.shm_open(payload['shm_name'].encode(),os.O_CREAT|os.O_RDWR,0o600)<0 else 'allowed'
print(json.dumps({'event':'child_barrier','pid':os.getpid(),'group':os.getpgrp(),'scratch':os.getcwd(),'attempts':attempts,'buffered_generation_digest':hashlib.sha256(payload['generation'].encode()).hexdigest()}),flush=True)
count=0
while True:
    count+=1
    with open('heartbeat','w') as stream: stream.write(str(count))
    time.sleep(.02)
'''


def manifest(paths):
    return {name: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'inode': path.stat().st_ino, 'links': path.stat().st_nlink,
                   'mode': path.stat().st_mode & 0o777}
            for name, path in paths.items()}


def record(request, case_id, began, before, after, **observed):
    duration=time.monotonic()-began
    assert duration < 20, 'required process case exceeded its complete outer deadline'
    request.node.user_properties.append(('lifecycle_evidence', {
        'schema_version': 1, 'id': case_id, 'duration_seconds': duration,
        'deadline_seconds': 20, 'expected': {'durable_mutation': False},
        'observed': observed, 'before': before, 'after': after,
        'cleanup': {'controller_reaped': True}, 'proofs_passed': True,
    }))


def bounded_process_case(function):
    """Reserve four seconds of the 20-second outer budget for fixture teardown."""
    @wraps(function)
    async def run(*args, **kwargs):
        task=asyncio.create_task(function(*args, **kwargs))
        completed,_=await asyncio.wait({task},timeout=16)
        if task not in completed:
            task.cancel()
            try: await asyncio.wait_for(task,timeout=4)
            except (asyncio.CancelledError,TimeoutError): pass
            pytest.fail('required child process case exceeded its bounded execution deadline')
        return task.result()
    return run


@pytest.mark.asyncio
@bounded_process_case
async def test_darwin_detached_child_capability_boundary(tmp_path, request):
    """Required on the claimed Darwin platform; absence is a failure, not skip."""
    began=time.monotonic()
    assert sys.platform == 'darwin', 'required host confinement proof is unverified on this platform'
    assert Path('/usr/bin/sandbox-exec').is_file(), 'required sandbox capability absent'
    state=tmp_path/'state'; workspace=tmp_path/'workspace'; state.mkdir(); workspace.mkdir()
    targets={'state':state/'state.bin','store':state/'knowledge.db',
             'workspace':workspace/'project.txt','artifact':workspace/'artifact.txt'}
    for path in targets.values(): path.write_bytes(b'preserved durable value\n')
    before=manifest(targets)
    resources=ExitStack(); handle=None
    try:
        lease=resources.enter_context(RuntimeLease(state/'runtime.lock'))
        registry=TaskRegistry(); worker=await registry.register_worker('software-engineer')
        task=await registry.register_task(worker)
        manager=ProcessManager(registry,lease=lease,scratch_root=state/'scratch')
        inherited=os.open(targets['state'],os.O_WRONLY)
        try: os.dup2(inherited,99,inheritable=True)
        finally: os.close(inherited)
        resources.callback(os.close,99)
        listener=resources.enter_context(socket.socket()); listener.bind(('127.0.0.1',0)); listener.listen(1)
        unix=resources.enter_context(socket.socket(socket.AF_UNIX,socket.SOCK_STREAM))
        # Darwin sockaddr_un has a short path limit; fixture ownership and
        # cleanup are preserved in a deliberately short external directory.
        channel_dir=resources.enter_context(tempfile.TemporaryDirectory(prefix='at-ipc-',dir='/private/tmp'))
        unix_path=Path(channel_dir)/'s'; unix.bind(str(unix_path)); unix.listen(1)
        payload={'targets':{name:str(path) for name,path in targets.items()},
                 'inherited_fd':99,'owner_pid':os.getpid(),'port':listener.getsockname()[1],
                 'unix_path':str(unix_path),'shm_name':'/agent-team-test-'+str(os.getpid()),
                 'generation':lease.generation}
        child_interpreter=str(Path(sys.executable).resolve())
        handle=await manager.spawn(worker,task,[child_interpreter,'-u','-c',CHILD_PROGRAM,json.dumps(payload)])
        assert handle.command == '[confined child]'
        line=await asyncio.wait_for(handle.process.stdout.readline(),5)
        if not line:
            error=(await handle.process.stderr.read()).decode(errors='replace')
            pytest.fail('required deny-default child did not launch: '+error[:500])
        barrier=json.loads(line)
        assert barrier['event']=='child_barrier'
        assert barrier['pid']==barrier['group']==handle.pid
        assert barrier['pid']!=os.getpid()
        unexpected={name:result for name,result in barrier['attempts'].items() if result!='denied'}
        assert not unexpected, 'Unexpected child capabilities: '+json.dumps(unexpected,sort_keys=True)
        heartbeat=Path(barrier['scratch'])/'heartbeat'
        deadline=time.monotonic()+2
        while not heartbeat.exists() and time.monotonic()<deadline: await asyncio.sleep(.01)
        first=heartbeat.read_text(); os.kill(handle.pid,signal.SIGTERM)
        while heartbeat.read_text()==first and time.monotonic()<deadline: await asyncio.sleep(.01)
        assert heartbeat.read_text()!=first, 'TERM-resistant detached child must continue scratch writes'
        assert handle.process.returncode is None
        assert manifest(targets)==before
        await manager.terminate_all_workers(hard=True)
        assert handle.process.returncode is not None and handle._reaped
        after=manifest(targets); assert after==before
        safe_barrier=dict(barrier)
        safe_barrier['scratch']=safe_barrier['scratch'].replace(lease.generation,'<generation>')
        record(request,'lifecycle.child.positive_capabilities',began,before,after,
               barrier=safe_barrier,term_resistant=True,child_exit=handle.process.returncode,
               no_commit_channel=True,allowed_profile='deny_default',
               allowed_mode='resolved_native_Darwin_interpreter',child_interpreter=child_interpreter,
               native_bootstrap_profile={
                   'path':'/System/Library/Sandbox/Profiles/dyld-support.sb',
                   'sha256':hashlib.sha256(Path('/System/Library/Sandbox/Profiles/dyld-support.sb').read_bytes()).hexdigest(),
                   'qualification':'Apple private bootstrap policy; exact tested host version only'})
    finally:
        try:
            if handle is not None and handle.process.returncode is None:
                handle.kill()
                await asyncio.wait_for(handle.reap(hard=True),timeout=3)
        finally: resources.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',[False,True])
async def test_failed_reap_retains_owned_handle(tmp_path, request, terminal):
    began=time.monotonic(); registry=TaskRegistry(); worker=await registry.register_worker('worker')
    await registry.register_task(worker)
    if terminal: registry._workers[worker].state=WorkerState.COMPLETE
    handle=ProcessHandle(worker,'task',123,123,__import__('datetime').datetime.now(),'fixture')
    async def fail(*,hard=True): raise PermissionError('fixture reap failure')
    handle.reap=fail; registry._workers[worker].child_processes.append(handle)
    with pytest.raises(RuntimeError,match='child_reap_failed'): await registry.cancel_worker(worker,hard=True)
    assert registry._workers[worker].child_processes==[handle]
    assert not handle._reaped
    record(request,'lifecycle.child.failed_reap.'+('terminal' if terminal else 'active'),began,{}, {},
           failure_propagated=True,failed_handle_preserved=True)


@pytest.mark.asyncio
async def test_terminate_all_preserves_failed_handle(request):
    began=time.monotonic(); registry=TaskRegistry(); worker=await registry.register_worker('worker')
    handle=ProcessHandle(worker,'task',123,123,__import__('datetime').datetime.now(),'fixture')
    async def fail(*,hard=True): raise PermissionError('fixture reap failure')
    handle.reap=fail; registry._workers[worker].child_processes.append(handle)
    manager=ProcessManager(registry)
    with pytest.raises(RuntimeError,match='child_reap_failed'): await manager.terminate_all_workers()
    assert registry._workers[worker].child_processes==[handle]
    record(request,'lifecycle.child.failed_reap.manager',began,{}, {},failure_propagated=True,failed_handle_preserved=True)


@pytest.mark.asyncio
async def test_live_process_cannot_be_unregistered(request):
    began=time.monotonic(); registry=TaskRegistry(); worker=await registry.register_worker('worker')
    process=type('LiveProcess',(),{'returncode':None})()
    handle=ProcessHandle(worker,'task',123,123,__import__('datetime').datetime.now(),'fixture',process=process)
    registry._workers[worker].child_processes.append(handle)
    with pytest.raises(RuntimeError,match='child_not_reaped'): await ProcessManager(registry).release_process(handle,'cancelled')
    assert registry._workers[worker].child_processes==[handle] and not handle._reaped
    record(request,'lifecycle.child.live_unregister_refused',began,{}, {},live_handle_preserved=True)


@pytest.mark.asyncio
async def test_uncontained_modes_refused(tmp_path, monkeypatch, request):
    began=time.monotonic(); state=tmp_path/'state'; state.mkdir(); lease=RuntimeLease(state/'runtime.lock').acquire()
    registry=TaskRegistry(); worker=await registry.register_worker('worker'); task=await registry.register_task(worker)
    manager=ProcessManager(registry,lease=lease,scratch_root=state/'scratch')
    try:
        monkeypatch.setattr(sys,'platform','unsupported-fixture')
        with pytest.raises(RuntimeError,match='child_confinement_unavailable'): await manager.spawn(worker,task,[sys.executable,'-c','pass'])
        with pytest.raises(RuntimeError,match='uncontained_child_refused'): await manager.track_process(worker,task,object(),['fixture'])
        assert not registry._workers[worker].child_processes
        record(request,'lifecycle.child.unsupported_refusal',began,{}, {},no_child_started=True)
    finally: lease.release()


@pytest.mark.asyncio
async def test_executable_alias_mode_refused(tmp_path, request):
    began=time.monotonic(); alias=tmp_path/'python-alias'; alias.symlink_to(Path(sys.executable).resolve())
    state=tmp_path/'state'; state.mkdir(); preserved=state/'preserved'; preserved.write_bytes(b'preserved')
    before=manifest({'durable':preserved})
    with RuntimeLease(state/'runtime.lock') as lease:
        registry=TaskRegistry(); worker=await registry.register_worker('worker'); task=await registry.register_task(worker)
        manager=ProcessManager(registry,lease=lease,scratch_root=state/'scratch')
        with pytest.raises(RuntimeError,match='executable aliases are unsupported'):
            await manager.spawn(worker,task,[str(alias),'-c','pass'])
        assert not (state/'scratch').exists() and not registry._workers[worker].child_processes
    after=manifest({'durable':preserved}); assert before==after
    record(request,'lifecycle.child.executable_alias_refusal',began,before,after,no_child_started=True,no_argv_rewrite=True)


def test_successor_production_mutation_rejects_buffered_generation(tmp_path, request):
    began=time.monotonic(); state=tmp_path/'state'; state.mkdir()
    lease=RuntimeLease(state/'runtime.lock').acquire(); old=lease.generation
    lease.release()
    program=r'''
import hashlib,json,sys
from pathlib import Path
from agent_team.persistence import RuntimeLease,SessionStore
state=Path(sys.argv[1]); old=sys.argv[2]; lease=RuntimeLease(state/'runtime.lock').acquire()
try:
    current=lease.generation
    store=SessionStore(state/'sessions',write_guard=lambda:lease.check_generation(current))
    store.write_guard=lambda:lease.check_generation(old)
    try: store.create(client_key='old-buffered'); rejected=False
    except RuntimeError: rejected=True
    assert rejected and not list((state/'sessions').glob('*.json'))
    store.write_guard=lambda:lease.check_generation(current)
    record=store.create(client_key='successor-current')
    assert store.get(record['session_id'])['client_key']=='successor-current'
    print(json.dumps({'pid':__import__('os').getpid(),'old_rejected':rejected,'current_committed':True,'session_count':len(list((state/'sessions').glob('*.json'))),'old_generation_digest':hashlib.sha256(old.encode()).hexdigest(),'new_generation_digest':hashlib.sha256(current.encode()).hexdigest()}))
finally: lease.release()
'''
    result=subprocess.run([sys.executable,'-c',program,str(state),old],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    observed=json.loads(result.stdout)
    assert observed['pid']!=os.getpid() and observed['old_rejected'] and observed['current_committed']
    assert observed['old_generation_digest']!=observed['new_generation_digest']
    assert observed['session_count']==1
    record(request,'lifecycle.child.successor_production_guard',began,{},
           {'sessions':manifest({'session':next((state/'sessions').glob('*.json'))})},
           successor=observed,production_mutation='SessionStore.create')
