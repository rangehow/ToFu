"""Sealed worker publication succeeds only after once-only verified file upload."""
import hashlib
import json
from pathlib import Path as Path
import sys
import time
import pytest
from tofu_environments.job_runner import run_job
from tofu_meituan_environments.hope_publish import execute,publish
pytestmark=pytest.mark.unit

@pytest.fixture
def publication(tmp_path):
    root=tmp_path/'work';root.mkdir();remote=tmp_path/'remote';remote.mkdir()
    cli=tmp_path/'hadoop.py'
    cli.write_text('''import pathlib,sys,shutil,urllib.parse
root=pathlib.Path(sys.argv[1]);args=sys.argv[2:];assert args[0]=='fs'
def path(uri):return root/urllib.parse.urlsplit(uri).path.lstrip('/')
if args[1]=='-mkdir':
 p=path(args[-1]);p.mkdir(parents='-p' in args,exist_ok='-p' in args)
elif args[1]=='-chmod':path(args[-1]).chmod(int(args[2],8))
elif args[1]=='-put':
 with open(args[2],'rb') as src,path(args[3]).open('xb') as dst:shutil.copyfileobj(src,dst)
elif args[1]=='-cat':sys.stdout.buffer.write(path(args[2]).read_bytes())
else:raise ValueError('unsupported')
''')
    config={'command':[sys.executable,str(cli),str(remote)],'namespace':'viewfs://cluster/namespace',
            'outputs':[{'name':'nested/result.bin','uri':'viewfs://cluster/namespace/nested/result.bin'}]}
    spec={'command':[sys.executable,'-c',"from pathlib import Path;Path('nested').mkdir();Path('nested/result.bin').write_bytes(b'abc')"],
          'outputs':['nested/result.bin'],'limits':{'runtime_seconds':5,'result_bytes':4096}}
    return root,remote,config,spec

def test_real_worker_and_publication_commands_complete(publication):
    root,remote,config,spec=publication
    result=execute(spec,config,root,run_job)
    assert result['state']=='succeeded' and result['exit_code']==0
    assert (remote/'namespace/nested/result.bin').read_bytes()==b'abc'
    assert (remote/'namespace').stat().st_mode&0o777==0o700
    assert json.loads((root/'tofu-publication-intent.json').read_text())==config
    assert result['outputs'][0]['sha256']==hashlib.sha256(b'abc').hexdigest()

def test_existing_namespace_is_not_overwritten_or_replayed(publication):
    root,remote,config,spec=publication;(remote/'namespace').mkdir();sentinel=remote/'namespace/sentinel';sentinel.write_bytes(b'keep')
    result=execute(spec,config,root,run_job)
    assert result['state']=='failed' and result['error']=='publication_failed_or_unknown'
    assert sentinel.read_bytes()==b'keep' and not (remote/'namespace/nested/result.bin').exists()
    with pytest.raises(FileExistsError):publish(result|{'state':'succeeded'},root,config,time.monotonic()+5)

def test_changed_file_fails_before_remote_namespace_creation(publication):
    root,remote,config,spec=publication;result=run_job(spec,root);(root/'nested/result.bin').write_bytes(b'abd')
    with pytest.raises(ValueError,match='changed'):publish(result,root,config,time.monotonic()+5)
    assert not (remote/'namespace').exists()

def test_failed_workload_never_publishes(publication):
    root,remote,config,spec=publication;spec['command']=[sys.executable,'-c','raise SystemExit(3)']
    result=execute(spec,config,root,run_job)
    assert result['state']=='failed' and result['exit_code']==3
    assert not (root/'tofu-publication-intent.json').exists() and not (remote/'namespace').exists()

def test_publication_silent_command_obeys_remaining_deadline(publication):
    root,remote,config,spec=publication;result=run_job(spec,root)
    config['command']=[sys.executable,'-c','import time;time.sleep(30)']
    start=time.monotonic()
    with pytest.raises(Exception):publish(result,root,config,start+.2)
    assert time.monotonic()-start<3


def test_pinned_parallel_upload_routes_snapshot_with_shared_deadline(publication,monkeypatch):
    import tofu_meituan_environments.hope_publish as module
    root,remote,config,spec=publication;result=run_job(spec,root)
    jar=root/'tofu-parallel-upload.jar';jar.write_bytes(b'pinned')
    config['parallel_upload']={'jar':jar.name,'sha256':hashlib.sha256(jar.read_bytes()).hexdigest(),'threshold_bytes':3,'lanes':4}
    calls=[];deadline=time.monotonic()+5
    monkeypatch.setattr(module,'_call',lambda command,bound:calls.append((command,bound)))
    publish(result,root,config,deadline)
    command,bound=calls[-1]
    assert command[len(config['command']):][:4]==['jar',str(jar),'CheckpointFileTransport','upload']
    assert command[-3:]==['3',hashlib.sha256(b'abc').hexdigest(),'4'] and bound==deadline
    assert all(b==deadline for _,b in calls)


def test_changed_upload_artifact_fails_before_intent_or_external_write(publication,monkeypatch):
    import tofu_meituan_environments.hope_publish as module
    root,remote,config,spec=publication;result=run_job(spec,root)
    jar=root/'tofu-parallel-upload.jar';jar.write_bytes(b'changed')
    config['parallel_upload']={'jar':jar.name,'sha256':hashlib.sha256(b'original').hexdigest(),'threshold_bytes':1,'lanes':4}
    monkeypatch.setattr(module,'_call',lambda *args:pytest.fail('External write before artifact admission'))
    with pytest.raises(ValueError,match='changed'):publish(result,root,config,time.monotonic()+5)
    assert not (root/'tofu-publication-intent.json').exists()


def test_parallel_failure_retains_intent_without_CLI_fallback(publication,monkeypatch):
    import tofu_meituan_environments.hope_publish as module
    root,remote,config,spec=publication
    jar=root/'tofu-parallel-upload.jar';jar.write_bytes(b'pinned')
    config['parallel_upload']={'jar':jar.name,'sha256':hashlib.sha256(jar.read_bytes()).hexdigest(),'threshold_bytes':1,'lanes':4}
    calls=[]
    def call(command,deadline):
        calls.append(command)
        if 'jar' in command:raise RuntimeError('injected unknown upload')
    monkeypatch.setattr(module,'_call',call)
    result=execute(spec,config,root,run_job)
    assert result['state']=='failed' and result['error']=='publication_failed_or_unknown'
    assert (root/'tofu-publication-intent.json').exists()
    assert sum('jar' in command for command in calls)==1
    assert not any('-put' in command for command in calls)


def test_durable_receipt_is_exact_success_json_after_data_publication(publication):
    from tofu_meituan_environments.hope_publish import receipt_contract
    root,remote,config,spec=publication
    name=receipt_contract()['receipt_name']
    config['worker_receipt']={'uri':config['namespace']+'/'+name}
    result=execute(spec,config,root,run_job)
    assert result['state']=='succeeded'
    payload=(remote/'namespace'/name).read_bytes()
    assert payload==json.dumps(result,sort_keys=True,allow_nan=False).encode()
    assert (remote/'namespace/nested/result.bin').read_bytes()==b'abc'


def test_unknown_control_commit_is_failed_without_replay(publication,monkeypatch):
    import tofu_meituan_environments.hope_publish as module
    root,remote,config,spec=publication
    config['worker_receipt']={'uri':config['namespace']+'/'+module.receipt_contract()['receipt_name']}
    real=module._call;commits=[]
    def injected(command,deadline):
        if command[-1]==config['worker_receipt']['uri']:
            commits.append(command)
            real(command,deadline)
            raise RuntimeError('lost acknowledgement after commit')
        real(command,deadline)
    monkeypatch.setattr(module,'_call',injected)
    result=execute(spec,config,root,run_job)
    assert result['state']=='failed' and result['error']=='worker_receipt_publication_failed_or_unknown'
    assert len(commits)==1
    assert json.loads((remote/'namespace'/module.receipt_contract()['receipt_name']).read_bytes())['state']=='succeeded'


def test_real_published_receipt_survives_dropped_stdout_and_collects_hash_checked_output(publication,tmp_path):
    import os
    from tofu_environments.contracts import Actor,seal
    from tofu_environments.repository import MemoryRepository
    from tofu_environments.jobs import JobService
    from tofu_meituan_environments.hope_artifacts import HopeArtifactReader,owned_artifact_uri
    from tofu_meituan_environments.hope_evidence import HopeEvidencePort
    from tofu_meituan_environments.hope_outputs import HopeOutputPort
    from tofu_meituan_environments.hope_publish import RECEIPT_CONTRACT_FILE,receipt_contract
    root,remote,config,spec=publication
    actor=Actor(7,'tenant','/project');op='job-'+'a'*32
    reference={'kind':'hope_run_id','id':'7'};artifact_root='viewfs://cluster/owned'
    spec['resources']={'cpu':2,'memory_gib':16,'gpu_count':1}
    output_uri=owned_artifact_uri(actor,op,spec['outputs'][0],artifact_root)
    config['namespace']=output_uri.rsplit('/nested/',1)[0]
    config['outputs'][0]['uri']=output_uri
    config['worker_receipt']={'uri':owned_artifact_uri(actor,op,receipt_contract()['receipt_name'],artifact_root)}
    (remote/'owned').mkdir()
    result=execute(spec,config,root,run_job)
    assert result['state']=='succeeded'
    repo=MemoryRepository()
    plan={'scope':actor.scope,'project_root':actor.project_root,'provider':'hope','spec':spec,'admission':{'include':[RECEIPT_CONTRACT_FILE]}}
    record={'operation_id':op,'plan':plan,'plan_hash':seal(plan),'reference':reference,'phase':'running'}
    repo.put(actor,op,record,0)
    reader=HopeArtifactReader(actor,repo,root_uri=artifact_root,command=config['command'],environment=dict(os.environ))
    worker={'status':'Succeeded','cluster':'cluster','namespace':'group','podName':'worker','nodeName':'node','hostName':'node'}
    state={'ok':True,'job':{'hope_run_id':'7','app_id':'app','state':'SUCCEEDED','creator':'fixture.user'},
           'raw':{'status':{'apps_info':[{'run_id':'7','app_id':'app','creator':'fixture.user','state':'SUCCEEDED'}]}}}
    lost_log={'ok':True,'log_text':'final stdout lost','truncated':True}
    def call(a,name,args,**kw):
        assert a==actor
        if name.endswith('get_job_attempts'):return {'ok':True,'job_id':'app','attempts':{'Worker':[{'attemptInfo':[worker]}]}}
        if args['file_name']=='stdout':return lost_log
        return {'ok':True,'truncated':False,'log_text':'key:AFO_RESOURCE_CONFIG,value:'+json.dumps({'worker':{'cpu':2,'mem':16384,'gpu':1,'num':1}})}
    evidence=HopeEvidencePort(actor,repo,call,expected_creator='fixture.user',worker_receipt_reader=reader.worker_receipt)
    assert evidence(actor,op,reference,state)=={'state':'succeeded','exit_code':0}
    record['phase']='succeeded';repo.put(actor,op,record,1)
    outputs=HopeOutputPort(actor,repo,lambda *args,**kw:evidence(actor,op,reference,state),
        lambda *args,**kw:evidence.receipt_text(actor,op,reference,lost_log,deadline=kw['deadline']),reader,timeout_seconds=5)
    class Provider:
        def read_job_output(self,a,r,operation,name):return outputs(a,r,operation,name)
    (tmp_path/'collected').mkdir()
    collected=JobService(repo,lambda *args:Provider()).collect(actor,op,tmp_path/'collected')
    assert (Path(collected['directory'])/'nested/result.bin').read_bytes()==b'abc'
    assert collected['outputs']==result['outputs']
