"""Real pipe transfers enforce scope, bounds, deadlines and process teardown."""
import os
from pathlib import Path
import sys
import time
import pytest
from tofu_environments.contracts import Actor,EnvironmentError,seal
from tofu_environments.repository import MemoryRepository
from tofu_meituan_environments.hope_artifacts import HopeArtifactReader,owned_artifact_uri
pytestmark=pytest.mark.unit
OP='job-'+'a'*32

@pytest.fixture
def setup(tmp_path):
    a=Actor(7,'tenant','/project'); r={'kind':'hope_run_id','id':'7'};repo=MemoryRepository()
    plan={'scope':a.scope,'project_root':a.project_root,'spec':{'outputs':['nested/result.bin'],'limits':{'result_bytes':32}}}
    record={'operation_id':OP,'plan':plan,'plan_hash':seal(plan),'reference':r,'phase':'succeeded'};repo.put(a,OP,record,0)
    script=tmp_path/'cli.py';script.write_text("import sys;sys.stdout.buffer.write(b'abc')")
    reader=HopeArtifactReader(a,repo,root_uri='viewfs://cluster/owned',command=[sys.executable,str(script)],environment=dict(os.environ))
    return a,r,repo,record,script,reader

def test_actual_subprocess_transfers_exact_file(setup):
    a,r,repo,record,script,reader=setup
    assert b''.join(reader(a,r,OP,'nested/result.bin',deadline=time.monotonic()+5,maximum_bytes=3))==b'abc'

@pytest.mark.parametrize('bound',[2,4])
def test_actual_transfer_size_disagreement_rejected(setup,bound):
    a,r,repo,record,script,reader=setup
    with pytest.raises(EnvironmentError) as e:list(reader(a,r,OP,'nested/result.bin',deadline=time.monotonic()+5,maximum_bytes=bound))
    assert e.value.code=='hope_output_changed'

def test_error_stderr_never_exposed(setup):
    a,r,repo,record,script,reader=setup;script.write_text("import sys;sys.stderr.write('SECRET');sys.exit(1)")
    with pytest.raises(EnvironmentError) as e:list(reader(a,r,OP,'nested/result.bin',deadline=time.monotonic()+5,maximum_bytes=3))
    assert e.value.code=='hope_artifact_read_failed' and 'SECRET' not in str(e.value)

def test_deadline_stops_silent_cli(setup):
    a,r,repo,record,script,reader=setup;script.write_text('import time;time.sleep(30)')
    start=time.monotonic()
    with pytest.raises(EnvironmentError) as e:list(reader(a,r,OP,'nested/result.bin',deadline=start+.2,maximum_bytes=3))
    assert e.value.code=='hope_output_timeout' and time.monotonic()-start<3

def test_consumer_close_reclaims_process(setup,tmp_path):
    a,r,repo,record,script,reader=setup;pidfile=tmp_path/'pid'
    script.write_text(f"import os,sys,time;open({str(pidfile)!r},'w').write(str(os.getpid()));sys.stdout.buffer.write(b'abc');sys.stdout.flush();time.sleep(30)")
    stream=reader(a,r,OP,'nested/result.bin',deadline=time.monotonic()+5,maximum_bytes=3)
    assert next(stream)==b'abc';pid=int(pidfile.read_text());stream.close()
    with pytest.raises(ProcessLookupError):os.kill(pid,0)

@pytest.mark.parametrize('change',['actor','reference','phase','seal','name','budget'])
def test_invalid_binding_does_not_launch_cli(setup,tmp_path,change):
    a,r,repo,record,script,reader=setup;marker=tmp_path/'started'
    script.write_text(f"open({str(marker)!r},'w').write('started')")
    name='nested/result.bin';maximum=3
    if change=='actor':a=Actor(8,'tenant','/project')
    if change=='reference':r={'kind':'hope_run_id','id':'foreign'}
    if change=='phase':record['phase']='running'
    if change=='seal':record['plan_hash']='bad'
    if change=='name':name='../secret'
    if change=='budget':maximum=33
    if change in ('phase','seal'):repo.put(a,OP,record,1)
    with pytest.raises(EnvironmentError):list(reader(a,r,OP,name,deadline=time.monotonic()+5,maximum_bytes=maximum))
    assert not marker.exists()

@pytest.mark.parametrize('name',['../x','a/../x','a//b','./x','/x','a\\b','a\nx'])
def test_unsafe_paths_fail(name):
    with pytest.raises(EnvironmentError):owned_artifact_uri(Actor(7),OP,name,'viewfs://cluster/owned')

def test_namespaces_bind_owner_tenant_project_and_operation():
    actors=[Actor(7,'a','/one'),Actor(8,'a','/one'),Actor(7,'b','/one'),Actor(7,'a','/two')]
    assert len({owned_artifact_uri(a,OP,'result','viewfs://cluster/owned') for a in actors})==4
    assert owned_artifact_uri(actors[0],OP,'result','viewfs://cluster/owned')!=owned_artifact_uri(actors[0],'job-'+'b'*32,'result','viewfs://cluster/owned')

def test_pipe_reader_composes_with_worker_integrity_and_generic_collection(setup,tmp_path):
    import hashlib
    import json
    from tofu_environments.jobs import JobService
    from tofu_meituan_environments.hope_outputs import HopeOutputPort
    a,r,repo,record,script,reader=setup
    record['plan']['provider']='hope';record['plan']['spec']['limits']['runtime_seconds']=60
    record['plan_hash']=seal(record['plan']);repo.put(a,OP,record,1)
    receipt={'state':'succeeded','exit_code':0,'error':None,'elapsed_seconds':1,'stdout':'','stderr':'',
             'outputs':[{'name':'nested/result.bin','bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}
    output=HopeOutputPort(a,repo,lambda *args,**kw:{'state':'succeeded','exit_code':0},
                          lambda *args,**kw:'TOFU_JOB_RESULT='+json.dumps(receipt),reader,timeout_seconds=5)
    class Provider:
        def read_job_output(self,actor,ref,operation,name):return output(actor,ref,operation,name)
    service=JobService(repo,lambda *args:Provider())
    collected=service.collect(a,OP,tmp_path)
    assert (Path(collected['directory'])/'nested/result.bin').read_bytes()==b'abc'
    assert collected['outputs']==receipt['outputs']
    script.write_text("import sys;sys.stdout.buffer.write(b'abd')")
    before=set(tmp_path.iterdir())
    with pytest.raises(EnvironmentError):service.collect(a,OP,tmp_path)
    assert set(tmp_path.iterdir())==before
    assert repo.get(a,OP)['value']['outputs']==receipt['outputs']



def test_reserved_receipt_read_before_output_admission_is_sealed_and_bounded(setup):
    from tofu_meituan_environments.hope_publish import RECEIPT_CONTRACT_FILE,receipt_contract
    a,r,repo,record,script,reader=setup
    record['phase']='running'
    record['plan']['admission']={'include':[RECEIPT_CONTRACT_FILE]}
    record['plan_hash']=seal(record['plan']);repo.put(a,OP,record,1)
    expected=owned_artifact_uri(a,OP,receipt_contract()['receipt_name'],reader.root_uri)
    script.write_text(f'import sys;assert sys.argv[-1]=={expected!r};sys.stdout.buffer.write(b"{{}}")')
    assert reader.worker_receipt(a,r,OP,deadline=time.monotonic()+5)==b'{}'
    with pytest.raises(EnvironmentError):list(reader(a,r,OP,'nested/result.bin',deadline=time.monotonic()+5,maximum_bytes=3))
    with pytest.raises(EnvironmentError):reader.worker_receipt(Actor(8,'tenant','/project'),r,OP,deadline=time.monotonic()+5)
    script.write_text('import sys;sys.stdout.buffer.write(b"x"*1048577)')
    with pytest.raises(EnvironmentError) as error:reader.worker_receipt(a,r,OP,deadline=time.monotonic()+5)
    assert error.value.code=='hope_output_changed'
