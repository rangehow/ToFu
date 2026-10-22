"""Artifact integrity and owner checks precede admission to collected results."""
import hashlib
import json
import pytest
from tofu_environments.contracts import Actor, EnvironmentError, seal
from tofu_environments.repository import MemoryRepository
from tofu_meituan_environments.hope_outputs import HopeOutputPort
pytestmark = pytest.mark.unit

@pytest.fixture
def setup():
    actor=Actor(7,'tenant','/project'); ref={'kind':'hope_run_id','id':'7'}
    repo=MemoryRepository(); spec={'outputs':['result.bin'],'limits':{'runtime_seconds':60,'result_bytes':32}}
    plan={'scope':actor.scope,'project_root':actor.project_root,'spec':spec}
    record={'operation_id':'job-7','plan':plan,'plan_hash':seal(plan),'reference':ref,'phase':'succeeded'}
    repo.put(actor,'job-7',record,0)
    data={'chunks':[b'abc'],'time':0,'closed':False,'calls':[]}
    receipt={'state':'succeeded','exit_code':0,'error':None,'elapsed_seconds':1,'stdout':'','stderr':'',
             'outputs':[{'name':'result.bin','bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}
    def proof(*args,**kw):data['calls'].append('proof');return {'state':'succeeded','exit_code':0}
    def read_receipt(*args,**kw):return 'TOFU_JOB_RESULT='+json.dumps(receipt)
    def read_bytes(a,r,o,n,**kw):
        assert (a,r,o,n)==(actor,ref,'job-7','result.bin')
        assert kw=={'deadline':10,'maximum_bytes':3}
        try:yield from data['chunks']
        finally:data['closed']=True
    port=HopeOutputPort(actor,repo,proof,read_receipt,read_bytes,timeout_seconds=10,clock=lambda:data['time'])
    return actor,ref,repo,record,data,receipt,port

def test_exact_stream_matches_worker_receipt_and_closes(setup):
    a,r,repo,record,data,receipt,port=setup
    assert b''.join(port(a,r,'job-7','result.bin'))==b'abc';assert data['closed']

@pytest.mark.parametrize('chunks',[[b'ab'],[b'abd'],[b'abcd'],['abc']])
def test_changed_truncated_oversize_or_nonbytes_rejected(setup,chunks):
    a,r,repo,record,data,receipt,port=setup;data['chunks']=chunks
    with pytest.raises(EnvironmentError):list(port(a,r,'job-7','result.bin'))
    assert data['closed']

@pytest.mark.parametrize('kind',['owner','project','reference','undeclared','seal','phase'])
def test_wrong_binding_never_invokes_transport(setup,kind):
    a,r,repo,record,data,receipt,port=setup;name='result.bin'
    if kind=='owner':a=Actor(8,'tenant','/project')
    if kind=='project':a=Actor(7,'tenant','/other')
    if kind=='reference':r={'kind':'hope_run_id','id':'foreign'}
    if kind=='undeclared':name='../secret'
    if kind=='seal':record['plan_hash']='bad'
    if kind=='phase':record['phase']='running'
    if kind in ('seal','phase'):repo.put(a,'job-7',record,1)
    with pytest.raises(EnvironmentError):list(port(a,r,'job-7',name))
    assert not data['calls'] and not data['closed']

def test_expired_read_closes_stream(setup):
    a,r,repo,record,data,receipt,port=setup
    stream=port(a,r,'job-7','result.bin');assert next(stream)==b'abc';data['time']=10
    with pytest.raises(EnvironmentError) as error:next(stream)
    assert error.value.code=='hope_output_timeout' and data['closed']

def test_failed_receipt_never_reads_bytes(setup):
    a,r,repo,record,data,receipt,port=setup;receipt.update(state='failed',exit_code=1)
    with pytest.raises(EnvironmentError):list(port(a,r,'job-7','result.bin'))
    assert not data['closed']

@pytest.mark.parametrize('payload',[b'abc',b'abd'])
def test_generic_collection_admits_only_worker_verified_bytes(setup,tmp_path,payload):
    from tofu_environments.jobs import JobService
    a,r,repo,record,data,receipt,port=setup;data['chunks']=[payload]
    record['plan']['provider']='hope';record['plan_hash']=seal(record['plan']);repo.put(a,'job-7',record,1)
    class Provider:
        def read_job_output(self,actor,reference,operation_id,name):
            return port(actor,reference,operation_id,name)
    service=JobService(repo,lambda actor,provider:Provider())
    if payload==b'abc':
        result=service.collect(a,'job-7',tmp_path)
        assert result['outputs']==receipt['outputs']
        assert repo.get(a,'job-7')['value']['outputs']==receipt['outputs']
    else:
        with pytest.raises(EnvironmentError):service.collect(a,'job-7',tmp_path)
        assert list(tmp_path.iterdir())==[]
        assert 'outputs' not in repo.get(a,'job-7')['value']
