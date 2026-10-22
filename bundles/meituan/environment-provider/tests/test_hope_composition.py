"""One owned binding composes ports without ambient login or bridge startup."""
from contextlib import ExitStack
import json
import pytest
from tofu_environments.contracts import Actor as Actor,EnvironmentError as EnvironmentError
from tofu_meituan_environments.hope import hope_provider_factory
from test_hope_evidence import evidence as evidence
pytestmark=pytest.mark.unit

@pytest.fixture
def composition(evidence):
    actor,reference,spec,repo,receipt,attempt,state,resource,calls,old=evidence
    class Bridge:
        connected=False;closed=False
        def connect_server(self,name,config):self.connected=True;assert name=='hope' and config=={'fixture':True}
        def call_tool(self,name,args,**kw):
            assert 1<=kw['timeout_override']<=30
            if name=='mcp__hope__get_job':return state
            return old.call(actor,name,args,retry_on_disconnect=True)
        def disconnect_all(self):self.closed=True
    bridge=Bridge();created=[]
    def factory(a):assert a==actor;created.append(a);return bridge
    binding={'scope':actor.scope,'project_root':actor.project_root,'profile':{'usergroup':'group','queue':'root.queue','image':'registry/image','gpu_resource_key':'worker.gcores-example'},'artifact_profile':{'root_uri':'viewfs://cluster/owned','command':['/opt/hadoop/bin/hadoop']},'expected_creator':'fixture.user','environment':{},'mcp_configuration':{'fixture':True},'mcp_timeout_seconds':30,'output_timeout_seconds':60}
    stack=ExitStack();context={'repository':repo,'register_cleanup':stack.callback,'context_byte_limit':1048576,'context_file_limit':64,'hope_binding_loader':lambda a:binding,'hope_bridge_factory':factory}
    yield actor,reference,repo,receipt,bridge,created,binding,stack,context
    stack.close()

def test_composed_factory_is_lazy_owned_and_closes_after_status(composition):
    a,r,repo,receipt,bridge,created,binding,stack,context=composition
    provider=hope_provider_factory(a,context);assert not created and not bridge.connected
    assert provider.job_status(a,r,'job-7')=={'state':'succeeded','exit_code':0}
    assert created==[a]
    stack.close();assert bridge.closed and provider.prepare.closed

def test_output_proof_and_receipt_use_same_owned_evidence(composition):
    a,r,repo,receipt,bridge,created,binding,stack,context=composition
    provider=hope_provider_factory(a,context)
    import time
    assert provider.outputs.prove_success(a,r,'job-7',deadline=time.monotonic()+60)=={'state':'succeeded','exit_code':0}
    text=provider.outputs.read_receipt(a,r,'job-7',deadline=time.monotonic()+60)
    assert json.loads(text.split('=',1)[1])['outputs']==receipt['outputs']

@pytest.mark.parametrize('key,value',[('scope','foreign'),('project_root','/other')])
def test_foreign_binding_fails_before_connection(composition,key,value):
    a,r,repo,receipt,bridge,created,binding,stack,context=composition;binding[key]=value
    with pytest.raises(EnvironmentError):hope_provider_factory(a,context)
    assert not created

def test_partial_legacy_ports_do_not_silently_fallback(composition):
    a,r,repo,receipt,bridge,created,binding,stack,context=composition;context['hope_mcp_call']=lambda:None
    with pytest.raises(EnvironmentError):hope_provider_factory(a,context)
    assert not created

def test_configuration_failure_registers_transport_cleanup(composition):
    a,r,repo,receipt,bridge,created,binding,stack,context=composition;binding['profile']['queue']='bad\nqueue'
    with pytest.raises(EnvironmentError):hope_provider_factory(a,context)
    stack.close();assert not created


@pytest.mark.parametrize('mode', ['admitted', 'no_opt_in', 'bad_previous_resources'])
def test_composed_output_receipt_uses_shared_preemption_admission(composition, evidence, mode):
    from tofu_environments.contracts import seal
    from tofu_meituan_environments.hope_attempts import PREEMPTION_DIAGNOSTIC
    import time
    a,r,repo,receipt,bridge,created,binding,stack,context=composition
    _,_,spec,_,_,attempt,state,resource,calls,old=evidence
    if mode!='no_opt_in':
        row=repo.get(a,'job-7');record=row['value']
        record['plan']['spec']['recovery']={'max_attempts':2,'restart_safe':True,'allowed_reason':'scheduler_preemption'}
        record['plan']['spec']['limits']['gpu_seconds']=120
        record['plan_hash']=seal(record['plan']);repo.put(a,'job-7',record,row['version'])
    attempt.update(containerId='latest-container',startTime='2026-10-10 15:00:21',finishedTime='2026-10-10 15:00:23')
    previous={**attempt,'podName':'preempted-worker','containerId':'previous-container','status':'Failed','startTime':'2026-10-10 15:00:00','finishedTime':'2026-10-10 15:00:20','diagnostics':PREEMPTION_DIAGNOSTIC}
    def call(name,args,**kwargs):
        if name.endswith('get_job'):return state
        if name.endswith('get_job_attempts'):return {'ok':True,'job_id':'app-7','attempts':{'Worker':[{'attemptInfo':[attempt,previous]}]}}
        if mode=='bad_previous_resources' and args.get('file_name')=='sys_afo.log' and args.get('pod_name')=='preempted-worker':
            return {'ok':True,'truncated':False,'log_text':'key:AFO_RESOURCE_CONFIG,value:'+json.dumps({'worker':{**resource['worker'],'gpu':2}})}
        return old.call(a,name,args,retry_on_disconnect=True)
    bridge.call_tool=call;provider=hope_provider_factory(a,context)
    if mode=='admitted':
        text=provider.outputs.read_receipt(a,r,'job-7',deadline=time.monotonic()+60)
        assert json.loads(text.split('=',1)[1])['outputs']==receipt['outputs']
        stdout=[args['pod_name'] for name,args,kwargs in calls if args.get('file_name')=='stdout']
        assert stdout and set(stdout)=={'worker-0'}
    else:
        with pytest.raises(EnvironmentError):provider.outputs.read_receipt(a,r,'job-7',deadline=time.monotonic()+60)


@pytest.mark.parametrize('output_timeout',[600,1800])
def test_long_output_transfer_profile_does_not_expand_control_receipt_budget(composition,output_timeout):
    a,r,repo,receipt,bridge,created,binding,stack,context=composition
    binding['output_timeout_seconds']=output_timeout
    provider=hope_provider_factory(a,context)
    assert not created and not bridge.connected
    assert provider.evidence.receipt_timeout_seconds==300
    assert provider.outputs.timeout_seconds==output_timeout
    assert provider.job_status(a,r,'job-7')=={'state':'succeeded','exit_code':0}
