"""Real owned ZIP preparation, worker execution and fail-closed lifecycle contracts."""
import hashlib
import json
from pathlib import Path
import zipfile
import pytest
from tofu_environments import Actor, EnvironmentError
from tofu_environments.job_runner import run_job
from tofu_meituan_environments.hope_source import HopeSourcePreparer
pytestmark = pytest.mark.unit

@pytest.fixture
def fixture(tmp_path):
    source = tmp_path/'source.zip'
    with zipfile.ZipFile(source, 'w') as archive: archive.writestr('src/main.py', 'print("hello")\n')
    actor = Actor(42, 'tenant', str(tmp_path))
    profile = {'usergroup':'group','queue':'root.queue','image':'registry/image:approved','gpu_resource_key':'worker.gcores-example'}
    spec = {'version':1,'project':'example','command':['/bin/echo','verified'],
            'source':{'uri':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest()},
            'resources':{'cpu':3,'memory_gib':1.25,'gpu_count':1},
            'limits':{'runtime_seconds':5,'gpu_seconds':5,'result_bytes':65536},'outputs':[],
            'provider_options':{'usergroup':'group','queue':'root.queue','job_type':'ml-easy-job','priority':'P1','conf_name':'tofu-job.hope'}}
    preparer = HopeSourcePreparer(actor, profile, byte_limit=65536,file_limit=100,total_byte_limit=262144)
    yield actor, spec, preparer
    preparer.close()

def test_stable_prepare_and_real_bounded_worker(fixture):
    actor,spec,p=fixture;first=p(actor,spec,operation_id='job-'+'a'*32);assert p(actor,spec,operation_id='job-'+'a'*32)==first
    root=Path(first['workdir']);execution=json.loads((root/'tofu-execution.json').read_text())
    result=run_job(execution,root);assert result['state']=='succeeded' and result['stdout']=='verified\n'
    config=(root/'tofu-job.hope').read_text();assert 'worker.vcore = 3' in config and 'worker.memory = 1280' in config and 'worker.gcores-example = 1' in config
    assert first['configuration_sha256']==hashlib.sha256((root/'tofu-job.hope').read_bytes()).hexdigest()

def test_foreign_actor_and_account_rejected(fixture):
    actor,spec,p=fixture
    with pytest.raises(EnvironmentError,match='active actor'):p(Actor(43,'tenant',actor.project_root),spec,operation_id='job-'+'a'*32)
    spec['provider_options']['usergroup']='another'
    with pytest.raises(EnvironmentError,match='trusted profile'):p(actor,spec,operation_id='job-'+'a'*32)

def test_explicit_recovery_is_in_configuration_and_content_admission(fixture):
    actor,spec,preparer=fixture
    first=preparer(actor,spec,operation_id='job-'+'a'*32)
    spec['recovery']={'max_attempts':3,'restart_safe':True,'allowed_reason':'scheduler_preemption'}
    spec['limits']['gpu_seconds']=15
    recovered=preparer(actor,spec,operation_id='job-'+'b'*32)
    config=(Path(recovered['workdir'])/'tofu-job.hope').read_text()
    assert 'afo.role.worker.task.attempt.max.retry = 2' in config
    assert recovered['configuration_sha256']!=first['configuration_sha256']

def test_changed_original_archive_rejected(fixture):
    actor,spec,p=fixture;p(actor,spec,operation_id='job-'+'a'*32);Path(spec['source']['uri']).write_bytes(b'changed')
    with pytest.raises(EnvironmentError,match='pinned hash'):p(actor,spec,operation_id='job-'+'a'*32)

def test_staged_change_rejected_and_close_removes_only_owned_staging(fixture):
    actor,spec,p=fixture;root=Path(p(actor,spec,operation_id='job-'+'a'*32)['workdir']);(root/'src/main.py').write_text('tampered')
    with pytest.raises(EnvironmentError,match='changed'):p(actor,spec,operation_id='job-'+'a'*32)
    p.close();assert not root.exists() and Path(spec['source']['uri']).exists()
    with pytest.raises(EnvironmentError,match='active actor'):p(actor,spec,operation_id='job-'+'a'*32)

def test_capacity_rejection_has_no_retained_staging(fixture):
    actor,spec,p=fixture;p.total_byte_limit=1
    with pytest.raises(EnvironmentError,match='capacity'):p(actor,spec,operation_id='job-'+'a'*32)
    assert not p.entries and p.used==0


def test_many_nested_sources_fit_platform_include_limit_without_losing_seal(fixture):
    import hashlib
    actor, spec, preparer = fixture
    source = Path(spec['source']['uri'])
    with zipfile.ZipFile(source, 'w') as archive:
        for index in range(250): archive.writestr(f'src/package/module{index}.py', b'print(42)')
    spec['source']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    preparer.file_limit = 300
    preparer.byte_limit = preparer.total_byte_limit = 1048576
    first = preparer(actor, spec, operation_id='job-'+'a'*32)
    assert 'src' in first['include'] and len(first['include']) <= 200
    assert len(list((Path(first['workdir'])/'src/package').iterdir())) == 250
    assert preparer(actor, spec, operation_id='job-'+'a'*32)['prepared_sha256'] == first['prepared_sha256']
    (Path(first['workdir'])/'src/package/module249.py').write_text('changed')
    with pytest.raises(EnvironmentError, match='changed'):
        preparer(actor, spec, operation_id='job-'+'a'*32)


def test_cached_source_directory_cannot_add_unsealed_symlink(fixture):
    actor, spec, preparer = fixture
    first = preparer(actor, spec, operation_id='job-'+'a'*32)
    (Path(first['workdir'])/'src/escape').symlink_to(Path(actor.project_root))
    with pytest.raises(EnvironmentError, match='link'):
        preparer(actor, spec, operation_id='job-'+'a'*32)

def test_reserved_runner_rejected(fixture):
    actor,spec,p=fixture;source=Path(spec['source']['uri'])
    with zipfile.ZipFile(source,'w') as archive:archive.writestr('tofu-job-runner.py','malicious')
    spec['source']['sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(EnvironmentError,match='reserved'):p(actor,spec,operation_id='job-'+'a'*32)

def test_publication_configuration_binds_operation_and_is_sealed(fixture):
    actor,spec,p=fixture
    p.artifact_profile={'root_uri':'viewfs://cluster/owned','command':['/opt/hadoop/bin/hadoop']}
    first=p(actor,spec,operation_id='job-'+'a'*32)
    second=p(actor,spec,operation_id='job-'+'b'*32)
    one=Path(first['workdir']);two=Path(second['workdir'])
    publication=json.loads((one/'tofu-publication.json').read_text())
    assert publication['namespace']!=json.loads((two/'tofu-publication.json').read_text())['namespace']
    assert 'tofu-hope-publish.py' in first['include']
    assert 'worker.script = python3 tofu-hope-publish.py' in (one/'tofu-job.hope').read_text()
    (one/'tofu-publication.json').write_text('{}')
    with pytest.raises(EnvironmentError,match='changed'):p(actor,spec,operation_id='job-'+'a'*32)

def test_plan_can_submit_after_preparer_teardown_with_exact_content_seal(fixture):
    from tofu_environments.repository import MemoryRepository
    from tofu_environments.jobs import JobService
    from tofu_meituan_environments.hope import HopeJobProvider
    actor,spec,first=fixture;repo=MemoryRepository();calls=[]
    def call(a,tool,args,**kw):
        calls.append(args);assert Path(args['workdir']).is_dir()
        assert 'tofu-job-runner.py' in args['include']
        return {'ok':True,'hope_run_id':'run-resumed'}
    provider=HopeJobProvider(actor,call,first,None,None)
    service=JobService(repo,lambda *_:provider)
    plan=service.plan(actor,'hope',spec,'restart-plan')
    old_paths=[entry[0].name for entry in first.entries.values()]
    assert 'workdir' not in plan['plan']['admission'] and plan['plan']['admission']['prepared_sha256']
    first.close();assert all(not Path(p).exists() for p in old_paths)
    second=HopeSourcePreparer(actor,first.profile,byte_limit=65536,file_limit=100,total_byte_limit=262144)
    try:
        resumed=HopeJobProvider(actor,call,second,None,None)
        new_service=JobService(repo,lambda *_:resumed)
        result=new_service.submit(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
        assert result['phase']=='queued' and result['reference']['id']=='run-resumed'
        assert len(calls)==1 and calls[0]['workdir'] not in old_paths
    finally:second.close()

def test_changed_runtime_content_prevents_reconstructed_submission(fixture,monkeypatch,tmp_path):
    from tofu_environments.repository import MemoryRepository
    from tofu_environments.jobs import JobService
    from tofu_meituan_environments.hope import HopeJobProvider
    from tofu_environments import job_runner
    actor,spec,first=fixture;repo=MemoryRepository();calls=[]
    provider=HopeJobProvider(actor,lambda *args,**kw:calls.append(args),first,None,None)
    service=JobService(repo,lambda *_:provider);plan=service.plan(actor,'hope',spec,'changed-runtime');first.close()
    changed=tmp_path/'changed_runner.py';changed.write_text('# different runtime semantics\n')
    monkeypatch.setattr(job_runner,'__file__',str(changed))
    second=HopeSourcePreparer(actor,first.profile,byte_limit=65536,file_limit=100,total_byte_limit=262144)
    try:
        resumed=HopeJobProvider(actor,lambda *args,**kw:calls.append(args),second,None,None)
        with pytest.raises(EnvironmentError,match='admission changed'):
            JobService(repo,lambda *_:resumed).submit(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
        assert not calls and repo.get(actor,plan['operation_id'])['value']['phase']=='planned'
    finally:second.close()


def test_parallel_upload_binary_and_CPU_lanes_enter_prepared_seal(fixture,tmp_path):
    actor,spec,p=fixture;jar=tmp_path/'uploader.jar';jar.write_bytes(b'pinned')
    p.artifact_profile={'root_uri':'viewfs://cluster/owned','command':['/opt/hadoop/bin/hadoop'],
        'parallel_upload':{'jar_path':str(jar),'sha256':hashlib.sha256(jar.read_bytes()).hexdigest(),'threshold_bytes':134217728}}
    result=p(actor,spec,operation_id='job-'+'a'*32);directory=Path(result['workdir'])
    assert (directory/'tofu-parallel-upload.jar').read_bytes()==b'pinned'
    publication=json.loads((directory/'tofu-publication.json').read_text())
    assert publication['parallel_upload']['lanes']==spec['resources']['cpu']
    assert 'tofu-parallel-upload.jar' in result['include']
    jar.write_bytes(b'tampered')
    with pytest.raises(EnvironmentError,match='trusted pin'):p(actor,spec,operation_id='job-'+'a'*32)
