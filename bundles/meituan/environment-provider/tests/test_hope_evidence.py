"""Reject unowned, unverified or repeated execution before reporting success."""
import json
import pytest
from tofu_environments.contracts import Actor,EnvironmentError,seal
from tofu_environments.repository import MemoryRepository
from tofu_meituan_environments.hope_evidence import HopeEvidencePort,decode_hope_response,verify_worker_receipt
pytestmark=pytest.mark.unit

@pytest.fixture
def evidence():
    actor=Actor(7,'tenant','/project');reference={'kind':'hope_run_id','id':'run-7'}
    spec={'resources':{'cpu':2,'memory_gib':16,'gpu_count':1},'outputs':['result.json'],'limits':{'runtime_seconds':60,'result_bytes':4096}}
    plan={'scope':actor.scope,'project_root':actor.project_root,'spec':spec};repo=MemoryRepository();record={'operation_id':'job-7','plan':plan,'plan_hash':seal(plan),'reference':reference};repo.put(actor,'job-7',record,0)
    receipt={'state':'succeeded','exit_code':0,'error':None,'elapsed_seconds':2.0,'outputs':[{'name':'result.json','bytes':2,'sha256':'a'*64}],'stdout':'','stderr':''}
    attempt={'status':'Succeeded','cluster':'cluster','namespace':'group','podName':'worker-0','nodeName':'node','hostName':'node'}
    state={'ok':True,'job':{'hope_run_id':'run-7','app_id':'app-7','state':'SUCCEEDED','creator':'fixture.user'}}
    state['raw']={'status':{'apps_info':[{'run_id':'run-7','app_id':'app-7','creator':'fixture.user','state':'SUCCEEDED'}]}}
    resource={'worker':{'cpu':2,'mem':16384,'gpu':1,'num':1}};calls=[]
    def call(a,name,args,**kw):
        calls.append((name,args,kw));assert a==actor and kw['retry_on_disconnect'] is True
        if name.endswith('get_job_attempts'):return {'ok':True,'job_id':'app-7','attempts':{'Worker':[{'attemptInfo':[attempt]}]}}
        text='TOFU_JOB_RESULT='+json.dumps(receipt) if args['file_name']=='stdout' else 'key:AFO_RESOURCE_CONFIG,value:'+json.dumps(resource)
        return {'ok':True,'log_text':text,'truncated':False}
    port=HopeEvidencePort(actor,repo,call,expected_creator='fixture.user')
    return actor,reference,spec,repo,receipt,attempt,state,resource,calls,port

def test_known_owned_job_requires_full_worker_and_allocation_proof(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    assert port(a,'job-7',r,state)=={'state':'succeeded','exit_code':0};assert len(calls)==3

def test_owned_cancelled_platform_requires_no_worker_logs(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    state['job']['state']='STOPPED';state['raw']['status']['apps_info'][0]['state']='STOPPED'
    assert port(a,'job-7',r,state)=={'state':'cancelled','exit_code':None}
    assert not calls

def test_execution_rejection_cannot_prevent_owned_cancellation(evidence):
    from tofu_meituan_environments.hope import HopeJobProvider
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    state['job']['state']='RUNNING';state['raw']['status']['apps_info'][0]['state']='RUNNING'
    def bad_execution(actor,name,args,**kw):
        return {'ok':True,'job_id':'app-7','attempts':{'Worker':[{'attemptInfo':[attempt,attempt]}]}}
    port.call=bad_execution
    with pytest.raises(EnvironmentError):port(a,'job-7',r,state)
    mutations=[]
    def call(actor,name,args,**kw):
        mutations.append((name,args,kw))
        if name.endswith('get_job'):return state
        return {'ok':True,'confirmation_token':'owned-stop'}
    provider=HopeJobProvider(a,call,None,port,None)
    provider.cancel_job(a,r,'job-7')
    assert [x[0] for x in mutations]==['mcp__hope__get_job','mcp__hope__stop_job','mcp__hope__stop_job']
    assert mutations[-1][2]['retry_on_disconnect'] is False
    assert mutations[-1][1]['confirmation_token']=='owned-stop'
    mutations.clear();state['raw']['status']['apps_info'][0]['creator']='foreign'
    with pytest.raises(EnvironmentError):provider.cancel_job(a,r,'job-7')
    assert [x[0] for x in mutations]==['mcp__hope__get_job']

def test_recovered_success_verifies_all_worker_allocations(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,old=evidence
    from tofu_meituan_environments.hope_attempts import PREEMPTION_DIAGNOSTIC
    row=repo.get(a,'job-7');value=row['value']
    value['plan']['spec']['recovery']={'max_attempts':3,'restart_safe':True,'allowed_reason':'scheduler_preemption'}
    value['plan']['spec']['limits']['gpu_seconds']=180
    value['plan_hash']=seal(value['plan']);repo.put(a,'job-7',value,row['version'])
    attempt.update(containerId='final-container',startTime='2026-10-10 15:00:21',finishedTime='2026-10-10 15:00:23')
    previous={**attempt,'podName':'preempted-worker','containerId':'old-container','status':'Failed',
              'startTime':'2026-10-10 15:00:00','finishedTime':'2026-10-10 15:00:20','diagnostics':PREEMPTION_DIAGNOSTIC}
    wrong_previous=False
    def call(actor,name,args,**kw):
        if name.endswith('get_job_attempts'):
            return {'ok':True,'job_id':'app-7','attempts':{'Worker':[{'attemptInfo':[attempt,previous]}]}}
        if wrong_previous and args['file_name']=='sys_afo.log' and args['pod_name']=='preempted-worker':
            return {'ok':True,'truncated':False,'log_text':'key:AFO_RESOURCE_CONFIG,value:'+json.dumps({'worker':{**resource['worker'],'gpu':2}})}
        return old.call(actor,name,args,**kw)
    port=HopeEvidencePort(a,repo,call,expected_creator='fixture.user')
    assert port(a,'job-7',r,state)=={'state':'succeeded','exit_code':0}
    assert {args['pod_name'] for name,args,kw in calls if args['file_name']=='sys_afo.log'}=={'worker-0','preempted-worker'}
    wrong_previous=True
    with pytest.raises(EnvironmentError) as error:port(a,'job-7',r,state)
    assert error.value.code=='hope_resource_unverified'

@pytest.mark.parametrize('change',[{'creator':'other'},{'hope_run_id':'foreign'}])
def test_foreign_platform_identity_rejected_before_logs(evidence,change):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;state['job'].update(change)
    with pytest.raises(EnvironmentError):port(a,'job-7',r,state)
    assert not calls

def test_project_change_rejected_before_repository_or_transport(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    with pytest.raises(EnvironmentError):port(Actor(7,'tenant','/other'),'job-7',r,state)
    assert not calls

def test_platform_success_without_zero_receipt_is_failure(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;receipt.update(state='failed',exit_code=3)
    assert port(a,'job-7',r,state)=={'state':'failed','exit_code':3}

def test_false_resource_allocation_rejected(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;resource['worker']['gpu']=0
    with pytest.raises(EnvironmentError) as error:port(a,'job-7',r,state)
    assert error.value.code=='hope_resource_unverified'

def test_assigned_worker_is_not_running_experiment(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;state['job']['state']='RUNNING';state['raw']['status']['apps_info'][0]['state']='RUNNING';attempt['status']='Assigned'
    assert port(a,'job-7',r,state)=={'state':'queued','exit_code':None};assert len(calls)==1

def test_pending_reference_before_application_is_kept_without_guessing(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;state['job']={'hope_run_id':'run-7','state':'SUBMITTING'}
    assert port(a,'job-7',r,state)=={'state':'queued','exit_code':None};assert not calls

def test_unknown_submission_has_no_automatic_replay(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    assert port(a,'job-7',None,{}) is None;assert not calls

@pytest.mark.parametrize('change',[{'exit_code':True},{'elapsed_seconds':float('nan')},{'outputs':[]},{'error':'bad'}])
def test_invalid_success_receipt_fails_closed(evidence,change):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;receipt.update(change)
    with pytest.raises(EnvironmentError):verify_worker_receipt('TOFU_JOB_RESULT='+json.dumps(receipt),s)

def test_duplicate_receipt_lines_rejected(evidence):
    a,r,s,repo,receipt,*_=evidence;text='TOFU_JOB_RESULT='+json.dumps(receipt)
    with pytest.raises(EnvironmentError):verify_worker_receipt(text+'\n'+text,s)

def test_mcp_error_prefix_and_identical_duplicate_json_are_decoded():
    assert decode_hope_response('MCP Error: {"ok":false}\n{"ok":false}')=={'ok':False}
    with pytest.raises(ValueError):decode_hope_response('{"ok":true}\n{"ok":false}')


def test_request_echo_cannot_hide_foreign_raw_application(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence;state['raw']['status']['apps_info'][0]['run_id']='foreign'
    with pytest.raises(EnvironmentError):port(a,'job-7',r,state)
    assert not calls


def test_provider_status_composes_real_transport_and_worker_evidence(evidence):
    from tofu_meituan_environments.hope import HopeJobProvider
    from tofu_meituan_environments.hope_mcp import HopeMCPPort
    actor,reference,spec,repo,receipt,attempt,state,resource,calls,old=evidence
    class Bridge:
        closed=False
        def connect_server(self,name,config):assert name=='hope' and config=={'fixture':True}
        def call_tool(self,name,args,**kw):
            assert kw=={'timeout_override':30,'retry_on_disconnect':True}
            if name=='mcp__hope__get_job':
                assert args=={'hope_run_id':reference['id'],'detail':'full'}
                return state
            return old.call(actor,name,args,retry_on_disconnect=True)
        def disconnect_all(self):self.closed=True
    bridge=Bridge()
    transport=HopeMCPPort(actor,lambda a:bridge,lambda a:{'fixture':True},timeout_seconds=30)
    proof=HopeEvidencePort(actor,repo,transport,expected_creator='fixture.user')
    provider=HopeJobProvider(actor,transport,None,proof,None)
    try:
        assert provider.job_status(actor,reference,'job-7')=={'state':'succeeded','exit_code':0}
        assert calls[0][0]=='mcp__hope__get_job_attempts'
        resource['worker']['gpu']=0
        with pytest.raises(EnvironmentError) as error:provider.job_status(actor,reference,'job-7')
        assert error.value.code=='hope_resource_unverified'
    finally:transport.close()
    assert bridge.closed


@pytest.mark.parametrize('worker_state',['Failed','Killed','Stopped','Cancelled'])
def test_terminal_worker_failure_is_not_queued_when_platform_still_running(evidence,worker_state):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    state['job']['state']='RUNNING';state['raw']['status']['apps_info'][0]['state']='RUNNING'
    attempt['status']=worker_state
    assert port(a,'job-7',r,state)=={'state':'failed','exit_code':None}
    assert len(calls)==1


def test_successful_worker_waits_for_platform_success(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    state['job']['state']='RUNNING';state['raw']['status']['apps_info'][0]['state']='RUNNING'
    assert port(a,'job-7',r,state)=={'state':'queued','exit_code':None}
    assert len(calls)==1


def test_platform_cancellation_retains_cancellation_with_failed_worker(evidence):
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    state['job']['state']='STOPPED';state['raw']['status']['apps_info'][0]['state']='STOPPED'
    attempt['status']='Failed'
    assert port(a,'job-7',r,state)=={'state':'cancelled','exit_code':None}


@pytest.mark.parametrize('truncated',[False,True])
def test_durable_receipt_recovers_missing_stdout_without_weakening_resources(evidence,truncated):
    from tofu_meituan_environments.hope_publish import RECEIPT_CONTRACT_FILE
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    row=repo.get(a,'job-7');record=row['value']
    record['plan']['admission']={'include':[RECEIPT_CONTRACT_FILE]}
    record['plan_hash']=seal(record['plan']);repo.put(a,'job-7',record,row['version'])
    native=json.dumps(receipt,sort_keys=True).encode();reads=[];old=port.call
    def call(actor,name,args,**kw):
        if args.get('file_name')=='stdout':return {'ok':True,'log_text':'lost final stdout','truncated':truncated}
        return old(actor,name,args,**kw)
    port.call=call
    def read(actor,reference,operation_id,**kw):
        reads.append((actor,reference,operation_id));return native
    port.worker_receipt_reader=read
    assert port(a,'job-7',r,state)=={'state':'succeeded','exit_code':0}
    assert reads==[(a,r,'job-7')]
    resource['worker']['gpu']=2
    with pytest.raises(EnvironmentError) as error:port(a,'job-7',r,state)
    assert error.value.code=='hope_resource_unverified'


@pytest.mark.parametrize('truncated',[False,True])
def test_durable_and_stdout_disagreement_rejected_even_with_truncation(evidence,truncated):
    from tofu_meituan_environments.hope_publish import RECEIPT_CONTRACT_FILE
    a,r,s,repo,receipt,attempt,state,resource,calls,port=evidence
    row=repo.get(a,'job-7');record=row['value']
    record['plan']['admission']={'include':[RECEIPT_CONTRACT_FILE]}
    record['plan_hash']=seal(record['plan']);repo.put(a,'job-7',record,row['version'])
    port.worker_receipt_reader=lambda *args,**kw:json.dumps(dict(receipt,elapsed_seconds=3)).encode()
    with pytest.raises(EnvironmentError) as error:
        port.receipt_text(a,'job-7',r,{'log_text':'TOFU_JOB_RESULT='+json.dumps(receipt),'truncated':truncated})
    assert error.value.code=='hope_receipt_conflict'
