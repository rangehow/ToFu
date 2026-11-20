"""Batch submission authority, checkpoint loss and provider status contracts."""
from copy import deepcopy
import pytest
from tofu_environments.contracts import Actor, EnvironmentError
from tofu_environments.repository import MemoryRepository
from tofu_environments.jobs import JobService
pytestmark = pytest.mark.unit

class Provider:
    def __init__(self): self.calls = 0; self.lost = False; self.revision = 1; self.reference = None; self.snapshot = {'state':'succeeded','exit_code':0}
    def validate_job(self, actor, spec, *, operation_id): return {'revision':self.revision}
    def submit_job(self, actor, spec, operation_id, admission):
        self.calls += 1; self.reference = {'kind':'hope_run_id','id':'run-1'}
        if self.lost: raise TimeoutError()
        return self.reference
    def reconcile_job(self, actor, record): return self.reference
    def job_status(self, actor, reference, operation_id): return self.snapshot

@pytest.fixture
def context():
    provider = Provider(); repository = MemoryRepository(); service = JobService(repository, lambda *_: provider)
    actor = Actor(1, 'tenant', '/project')
    spec = {'version':1,'project':'demo','command':['python3','gpu_smoke.py'],
            'source':{'sha256':'a'*64,'uri':'artifact:source'},'resources':{'cpu':2,'memory_gib':8,'gpu_count':1},
            'limits':{'runtime_seconds':180,'gpu_seconds':180,'result_bytes':1048576},
            'outputs':['smoke-result.json'],'provider_options':{}}
    plan = service.plan(actor, 'hope', spec, 'request')
    return provider, repository, service, actor, spec, plan

def submit(context):
    p,r,s,a,spec,plan=context
    return s.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)

def test_complete_flow_and_idempotency(context):
    p,r,s,a,spec,plan=context
    assert s.plan(a,'hope',spec,'request')==plan
    assert submit(context)['phase']=='queued'
    assert submit(context)['phase']=='queued'
    assert s.status(a,plan['operation_id'])['phase']=='succeeded'
    assert p.calls==1

def test_failed_execution_can_be_closed_without_losing_failure_evidence(context):
    p,r,s,a,spec,plan=context;submit(context)
    p.snapshot={'state':'failed','exit_code':7}
    assert s.status(a,plan['operation_id'])['phase']=='failed'
    calls=[];p.cancel_job=lambda actor,reference,operation_id:calls.append((actor,reference,operation_id))
    assert s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='cancelling'
    assert len(calls)==1
    p.snapshot={'state':'cancelled','exit_code':None}
    assert s.status(a,plan['operation_id'])['phase']=='cancelled'
    failure=r.get(a,plan['operation_id'])['value']['execution_failure_before_cancellation']
    assert failure['phase']=='failed' and failure['exit_code']==7

def test_definitively_rejected_submission_without_reference_is_not_cancelled(context):
    p,r,s,a,spec,plan=context
    def reject(*args):raise EnvironmentError('submission_rejected','Explicit cluster rejection')
    p.submit_job=reject
    failed=submit(context);assert failed['phase']=='failed' and failed['reference'] is None
    def forbidden(*args):raise AssertionError('No external reference exists to cancel')
    p.cancel_job=forbidden
    assert s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)==failed
    assert r.get(a,plan['operation_id'])['value']['phase']=='failed'

def test_recovery_seals_restart_safety_and_all_attempt_gpu_budget(context):
    from tofu_environments.jobs import validate_job_spec
    spec=deepcopy(context[4])
    spec['recovery']={'max_attempts':3,'restart_safe':True,'allowed_reason':'scheduler_preemption'}
    with pytest.raises(EnvironmentError):validate_job_spec(spec)
    spec['limits']['gpu_seconds']=540
    assert validate_job_spec(spec)==spec
    spec['recovery']['restart_safe']=False
    with pytest.raises(EnvironmentError):validate_job_spec(spec)

def test_lost_response_is_read_only_reconciled(context):
    p,r,s,a,spec,plan=context;p.lost=True
    assert submit(context)['phase']=='uncertain'
    assert submit(context)['phase']=='uncertain'
    assert s.status(a,plan['operation_id'])['phase']=='succeeded'
    assert p.calls==1

def test_checkpoint_failure_and_new_service_do_not_replay(context,monkeypatch):
    p,r,s,a,spec,plan=context; original=r.put
    def put(actor,op,value,version):
        if value['phase']=='queued':raise RuntimeError('checkpoint failure')
        return original(actor,op,value,version)
    monkeypatch.setattr(r,'put',put)
    with pytest.raises(RuntimeError):submit(context)
    monkeypatch.setattr(r,'put',original)
    restarted=JobService(r,lambda *_:p)
    assert restarted.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='submitting'
    assert restarted.status(a,plan['operation_id'])['phase']=='succeeded'
    assert p.calls==1

def test_authority_and_admission(context):
    p,r,s,a,spec,plan=context
    with pytest.raises(EnvironmentError,match='authorization'):s.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:False)
    with pytest.raises(EnvironmentError):s.status(Actor(2,'tenant','/project'),plan['operation_id'])
    with pytest.raises(EnvironmentError):s.status(Actor(1,'tenant','/other'),plan['operation_id'])
    p.revision=2
    with pytest.raises(EnvironmentError,match='admission changed'):submit(context)
    assert p.calls==0

@pytest.mark.parametrize('mutation',[lambda x:x['limits'].update(gpu_seconds=179),lambda x:x.update(command=[]),lambda x:x.update(outputs=['../escape']),lambda x:x['resources'].update(gpu_count=-1)])
def test_invalid_resource_and_output_contract(context,mutation):
    p,r,s,a,spec,plan=context;mutation(spec)
    with pytest.raises(EnvironmentError):s.plan(a,'hope',spec,'other')

@pytest.mark.parametrize('snapshot',[{'state':'succeeded','exit_code':None},{'state':'succeeded','exit_code':1},{'state':'succeeded','exit_code':False},{'state':'invented','exit_code':0}])
def test_success_requires_proven_exit(context,snapshot):
    p,r,s,a,spec,plan=context;submit(context);p.snapshot=snapshot
    with pytest.raises(EnvironmentError):s.status(a,plan['operation_id'])
    assert r.get(a,plan['operation_id'])['value']['phase']=='queued'

def test_terminal_status_seals_typed_discrepancies_on_the_job_record(context):
    p,r,s,a,spec,plan=context;submit(context)
    discrepancies=[{'code':'allocation_over_budget','message':'1990 GPU-seconds above the sealed 1800'}]
    p.snapshot={'state':'succeeded','exit_code':0,'discrepancies':discrepancies}
    view=s.status(a,plan['operation_id'])
    assert view['phase']=='succeeded' and view['exit_code']==0 and view['discrepancies']==discrepancies
    assert r.get(a,plan['operation_id'])['value']['discrepancies']==discrepancies

@pytest.mark.parametrize('snapshot',[
    {'state':'running','exit_code':None,'discrepancies':[{'code':'x','message':'y'}]},
    {'state':'succeeded','exit_code':0,'discrepancies':[]},
    {'state':'failed','exit_code':None,'discrepancies':[{'code':'','message':'y'}]},
    {'state':'failed','exit_code':None,'discrepancies':[{'code':'x'}]},
    {'state':'failed','exit_code':None,'discrepancies':{'code':'x','message':'y'}}])
def test_discrepancies_are_typed_evidence_of_a_terminal_status_only(context,snapshot):
    p,r,s,a,spec,plan=context;submit(context);p.snapshot=snapshot
    with pytest.raises(EnvironmentError) as error:s.status(a,plan['operation_id'])
    assert error.value.code=='provider_protocol'
    assert r.get(a,plan['operation_id'])['value']['phase']=='queued'

def test_conflicting_request(context):
    p,r,s,a,spec,plan=context;spec['command']=['other']
    with pytest.raises(EnvironmentError,match='another job'):s.plan(a,'hope',spec,'request')

def test_concurrent_submission_has_one_external_write(context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p,r,s,a,spec,plan=context; entered=Event(); release=Event(); original=p.submit_job
    def slow(*args):
        entered.set(); assert release.wait(5)
        return original(*args)
    p.submit_job=slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(submit,context)
        assert entered.wait(5)
        assert submit(context)['phase']=='submitting'
        release.set()
        assert first.result()['phase']=='queued'
    assert p.calls==1

def test_collect_hashes_owned_outputs_and_detects_changed_result(context,tmp_path):
    import hashlib
    p,r,s,a,spec,plan=context;submit(context);s.status(a,plan['operation_id'])
    calls=[]
    def read(actor,reference,operation_id,name):
        calls.append((actor,reference,operation_id,name));yield b'{"ok":true}'
    p.read_job_output=read
    receipt=s.collect(a,plan['operation_id'],tmp_path)
    assert receipt['outputs']==[{'name':'smoke-result.json','bytes':11,'sha256':hashlib.sha256(b'{"ok":true}').hexdigest()}]
    assert calls[0]==(a,p.reference,plan['operation_id'],'smoke-result.json')
    p.read_job_output=lambda *args: iter([b'changed'])
    with pytest.raises(EnvironmentError,match='differ'):s.collect(a,plan['operation_id'],tmp_path)
    assert len(list(tmp_path.iterdir()))==1

def test_collect_rejects_unfinished_and_foreign_job(context,tmp_path):
    p,r,s,a,spec,plan=context
    with pytest.raises(EnvironmentError,match='successful'):s.collect(a,plan['operation_id'],tmp_path)
    with pytest.raises(EnvironmentError):s.collect(Actor(2),plan['operation_id'],tmp_path)
    assert not list(tmp_path.iterdir())

@pytest.mark.parametrize('chunks',[[b'x'*1048577],[b'x'*600000,b'x'*600000],['text']])
def test_collect_bounds_and_cleans_partial_results(context,tmp_path,chunks):
    p,r,s,a,spec,plan=context;submit(context);s.status(a,plan['operation_id'])
    p.read_job_output=lambda *args:iter(chunks)
    with pytest.raises(EnvironmentError):s.collect(a,plan['operation_id'],tmp_path)
    assert not list(tmp_path.iterdir())
    assert 'outputs' not in r.get(a,plan['operation_id'])['value']

def test_collect_checkpoint_failure_cleans_partial(context,tmp_path,monkeypatch):
    p,r,s,a,spec,plan=context;submit(context);s.status(a,plan['operation_id'])
    p.read_job_output=lambda *args:iter([b'ok'])
    monkeypatch.setattr(r,'put',lambda *args: (_ for _ in ()).throw(RuntimeError('lost checkpoint')))
    with pytest.raises(RuntimeError):s.collect(a,plan['operation_id'],tmp_path)
    assert not list(tmp_path.iterdir())

@pytest.mark.parametrize('lost',[False,True])
def test_cancel_is_claimed_once_and_requires_terminal_readback(context,lost):
    p,r,s,a,spec,plan=context;submit(context);calls=[]
    def cancel(*args):
        calls.append(args)
        if lost:raise TimeoutError()
    p.cancel_job=cancel;p.snapshot={'state':'running','exit_code':None}
    result=s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert result['phase']==('cancel_uncertain' if lost else 'cancelling')
    assert s.status(a,plan['operation_id'])['phase']==result['phase']
    s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert len(calls)==1
    p.snapshot={'state':'cancelled','exit_code':None}
    assert s.status(a,plan['operation_id'])['phase']=='cancelled'

def test_cancel_rejects_foreign_or_unapproved_job(context):
    p,r,s,a,spec,plan=context;submit(context)
    with pytest.raises(EnvironmentError):s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:False)
    with pytest.raises(EnvironmentError):s.cancel(Actor(2),plan['operation_id'],plan['plan_hash'],lambda *_:True)

def test_cancel_checkpoint_loss_does_not_issue_stop_again(context,monkeypatch):
    p,r,s,a,spec,plan=context;submit(context);calls=[]
    p.cancel_job=lambda *args:calls.append(args)
    original=r.put
    def put(actor,op,value,version):
        current=r.get(actor,op)['value']
        if value['phase']=='cancelling' and current['phase']=='cancelling':raise RuntimeError('checkpoint lost')
        return original(actor,op,value,version)
    monkeypatch.setattr(r,'put',put)
    with pytest.raises(RuntimeError):s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    monkeypatch.setattr(r,'put',original)
    restarted=JobService(r,lambda *_:p)
    assert restarted.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='cancelling'
    assert len(calls)==1

def test_provider_admission_receives_same_operation_at_plan_and_submit(context):
    p,r,s,a,spec,plan=context;seen=[]
    def validate(actor,spec,*,operation_id):
        seen.append(operation_id);return {'revision':p.revision}
    p.validate_job=validate
    assert s.plan(a,'hope',spec,'request')['operation_id']==plan['operation_id']
    submit(context)
    assert seen==[plan['operation_id'],plan['operation_id']]

def test_planned_cancellation_terminally_prevents_submission(context):
    p,r,s,a,spec,plan=context;calls=[]
    p.cancel_job=lambda *args:calls.append(args)
    cancelled=s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert cancelled['phase']=='cancelled' and cancelled['reference'] is None
    restarted=JobService(r,lambda *_:p)
    assert restarted.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)==cancelled
    assert restarted.status(a,plan['operation_id'])==cancelled
    assert restarted.plan(a,'hope',spec,'request')==cancelled
    assert p.calls==0 and calls==[]

@pytest.mark.parametrize('lost',[False,True])
def test_uncertain_cancellation_intent_survives_restart_and_read_only_reconciliation(context,lost):
    p,r,s,a,spec,plan=context;p.lost=True
    assert submit(context)['phase']=='uncertain'
    calls=[]
    def cancel(*args):
        calls.append(args)
        if lost:raise TimeoutError()
    p.cancel_job=cancel;p.snapshot={'state':'running','exit_code':None}
    reference=p.reference;p.reference=None
    requested=s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    intent={'plan_hash':plan['plan_hash'],'state':'requested'}
    assert requested['phase']=='uncertain' and requested['cancellation']==intent
    assert r.get(a,plan['operation_id'])['value']['cancellation']==intent
    restarted=JobService(r,lambda *_:p)
    assert restarted.status(a,plan['operation_id'])==requested
    assert restarted.progress_cancellation(a,plan['operation_id'])==requested
    p.reference=reference
    reconciled=restarted.status(a,plan['operation_id'])
    assert reconciled['reference']==reference and reconciled['cancellation']==intent
    assert reconciled['phase']=='running' and calls==[]
    assert restarted.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)==reconciled
    result=restarted.progress_cancellation(a,plan['operation_id'])
    assert result['phase']==('cancel_uncertain' if lost else 'cancelling')
    assert result['cancellation']['state']=='claimed'
    assert JobService(r,lambda *_:p).progress_cancellation(a,plan['operation_id'])==result
    assert restarted.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)==result
    assert restarted.status(a,plan['operation_id'])==result
    assert len(calls)==1 and p.calls==1
    p.snapshot={'state':'failed','exit_code':7}
    assert restarted.status(a,plan['operation_id'])['phase']=='failed'
    assert restarted.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='failed'
    assert len(calls)==1

@pytest.mark.parametrize('lost',[False,True])
def test_cancel_during_external_submission_preserves_intent(context,lost):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p,r,s,a,spec,plan=context;entered=Event();release=Event();original=p.submit_job;p.lost=lost
    calls=[];p.cancel_job=lambda *args:calls.append(args)
    def slow(*args):
        entered.set();assert release.wait(5)
        return original(*args)
    p.submit_job=slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        submitting=pool.submit(submit,context)
        assert entered.wait(5)
        try:
            result=s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
            assert result['phase']=='submitting' and result['cancellation']['state']=='requested'
            assert r.get(a,plan['operation_id'])['value']['cancellation']==result['cancellation']
        finally:release.set()
        finished=submitting.result()
    assert finished['phase']==('uncertain' if lost else 'queued')
    assert finished['cancellation']==result['cancellation'] and calls==[]
    p.snapshot={'state':'running','exit_code':None}
    restarted=JobService(r,lambda *_:p)
    assert restarted.status(a,plan['operation_id'])['phase']=='running'
    assert calls==[]
    assert restarted.progress_cancellation(a,plan['operation_id'])['phase']=='cancelling'
    assert p.calls==1 and len(calls)==1

@pytest.mark.parametrize('winner',['submit','cancel'])
def test_concurrent_submit_cancel_preserves_intent_and_reads_cas_winner(context,monkeypatch,winner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p,r,s,a,spec,plan=context;entered=Event();release=Event();original=r.put
    stops=[];p.cancel_job=lambda *args:stops.append(args)
    loser='submitting' if winner=='cancel' else 'cancelled'
    def put(actor,op,value,version):
        if value['phase']==loser and version==1:
            entered.set();assert release.wait(5)
        return original(actor,op,value,version)
    monkeypatch.setattr(r,'put',put)
    def cancel():return s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(submit,context) if winner=='cancel' else pool.submit(cancel)
        assert entered.wait(5)
        try:
            result=cancel() if winner=='cancel' else submit(context)
            assert result['phase']==('cancelled' if winner=='cancel' else 'queued')
        finally:release.set()
        settled=pending.result()
    assert settled['phase']==('cancelled' if winner=='cancel' else 'cancelling')
    assert p.calls==(0 if winner=='cancel' else 1)
    assert len(stops)==(0 if winner=='cancel' else 1)
    if winner=='submit':
        assert cancel()['phase']=='cancelling' and len(stops)==1
    else:assert submit(context)['phase']=='cancelled'

def test_concurrent_cancel_progression_has_one_stop_claim(context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p,r,s,a,spec,plan=context;submit(context);entered=Event();release=Event();calls=[]
    def stop(*args):
        calls.append(args);entered.set();assert release.wait(5)
    p.cancel_job=stop
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(s.cancel,a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
        assert entered.wait(5)
        try:
            assert s.progress_cancellation(a,plan['operation_id'])['phase']=='cancelling'
            assert s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='cancelling'
        finally:release.set()
        assert first.result()['phase']=='cancelling'
    assert len(calls)==1

@pytest.mark.parametrize('phase',['planned','uncertain','queued'])
def test_cancellation_denied_or_foreign_does_not_record_authority(context,phase):
    p,r,s,a,spec,plan=context
    if phase!='planned':
        p.lost=phase=='uncertain';submit(context)
    before=r.get(a,plan['operation_id']);calls=[];p.cancel_job=lambda *args:calls.append(args)
    for actor,digest,allowed in [(a,plan['plan_hash'],False),(a,'wrong',True),
                                 (Actor(2,'tenant','/project'),plan['plan_hash'],True),
                                 (Actor(1,'tenant','/other'),plan['plan_hash'],True)]:
        with pytest.raises(EnvironmentError):s.cancel(actor,plan['operation_id'],digest,lambda *_:allowed)
    with pytest.raises(EnvironmentError,match='Recorded exact'):
        s.progress_cancellation(a,plan['operation_id'])
    with pytest.raises(EnvironmentError):s.progress_cancellation(Actor(2),plan['operation_id'])
    assert r.get(a,plan['operation_id'])==before and calls==[]

def test_recorded_intent_does_not_bypass_fresh_public_authorizer(context):
    p,r,s,a,spec,plan=context;p.lost=True;submit(context)
    s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    p.snapshot={'state':'running','exit_code':None};s.status(a,plan['operation_id'])
    calls=[];p.cancel_job=lambda *args:calls.append(args)
    with pytest.raises(EnvironmentError,match='authorization'):
        s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:False)
    assert calls==[]
    assert s.progress_cancellation(a,plan['operation_id'])['phase']=='cancelling'
    assert len(calls)==1

def test_definitively_rejected_stop_needs_fresh_authorization(context):
    p,r,s,a,spec,plan=context;submit(context);calls=[]
    def reject(*args):
        calls.append(args);raise EnvironmentError('stop_denied','Definite stop rejection')
    p.cancel_job=reject
    result=s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert result['phase']=='queued' and result['cancellation']['state']=='rejected'
    with pytest.raises(EnvironmentError,match='Recorded exact'):s.progress_cancellation(a,plan['operation_id'])
    with pytest.raises(EnvironmentError):s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:False)
    assert len(calls)==1
    s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert len(calls)==2

def test_reconciled_reference_is_checkpointed_before_status_read_failure(context):
    p,r,s,a,spec,plan=context;p.lost=True;submit(context)
    s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    def fail(*args):raise TimeoutError('status read failed')
    p.job_status=fail
    with pytest.raises(TimeoutError):s.status(a,plan['operation_id'])
    saved=r.get(a,plan['operation_id'])['value']
    assert saved['phase']=='queued' and saved['reference']==p.reference
    assert saved['cancellation']['state']=='requested'
    def no_reconcile(*args):raise AssertionError('Reference already recovered')
    p.reconcile_job=no_reconcile;calls=[];p.cancel_job=lambda *args:calls.append(args)
    assert JobService(r,lambda *_:p).progress_cancellation(a,plan['operation_id'])['phase']=='cancelling'
    assert len(calls)==1 and p.calls==1

def test_terminal_status_does_not_steal_stop_settlement(context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p,r,s,a,spec,plan=context;submit(context);entered=Event();release=Event();calls=[]
    def stop(*args):
        calls.append(args);entered.set();assert release.wait(5)
    p.cancel_job=stop
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(s.cancel,a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
        assert entered.wait(5)
        try:
            p.snapshot={'state':'cancelled','exit_code':None}
            assert s.status(a,plan['operation_id'])['phase']=='cancelled'
        finally:release.set()
        assert pending.result()['phase']=='cancelled'
    assert s.progress_cancellation(a,plan['operation_id'])['phase']=='cancelled'
    assert len(calls)==1

@pytest.mark.parametrize('second_check',[False,True])
@pytest.mark.parametrize('raises',[False,True])
def test_submit_guard_checks_claim_and_physical_boundary_without_replaying(context,second_check,raises):
    p,r,s,a,spec,plan=context;checks=[];authorizations=[]
    class Aborted(BaseException):pass
    def guard():
        checks.append(r.get(a,plan['operation_id'])['value']['phase'])
        if len(checks)==(2 if second_check else 1):
            if raises:raise Aborted()
            return False
    def authorize(*args):authorizations.append(args);return True
    with pytest.raises(Aborted if raises else EnvironmentError):
        s.submit(a,plan['operation_id'],plan['plan_hash'],authorize,before_submit=guard)
    saved=r.get(a,plan['operation_id'])['value']
    assert checks==(['planned','submitting'] if second_check else ['planned'])
    assert len(authorizations)==1 and p.calls==0
    assert saved['phase']==('failed' if second_check else 'planned')
    if second_check:
        assert saved['error']=='submission_aborted'
        assert JobService(r,lambda *_:p).submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='failed'
        assert p.calls==0

def test_submit_guard_runs_after_admission_recheck(context):
    p,r,s,a,spec,plan=context;admitted=[]
    def validate(actor,spec,*,operation_id):
        admitted.append(operation_id);return {'revision':p.revision}
    p.validate_job=validate
    def guard():
        assert admitted==[plan['operation_id']]
    assert s.submit(a,plan['operation_id'],plan['plan_hash'],lambda *_:True,before_submit=guard)['phase']=='queued'
    assert p.calls==1

def test_competing_stop_claims_never_duplicate_provider_write(context,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    p,r,s,a,spec,plan=context;p.lost=True;submit(context)
    s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    p.snapshot={'state':'running','exit_code':None};s.status(a,plan['operation_id'])
    original=r.put;barrier=Barrier(2);calls=[];claim_version=r.get(a,plan['operation_id'])['version']
    p.cancel_job=lambda *args:calls.append(args)
    def put(actor,op,value,version):
        if value['phase']=='cancelling' and version==claim_version:
            barrier.wait(5)
        return original(actor,op,value,version)
    monkeypatch.setattr(r,'put',put)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=[pool.submit(s.progress_cancellation,a,plan['operation_id']) for _ in range(2)]
        results=[task.result()['phase'] for task in pending]
    assert results==['cancelling','cancelling'] and len(calls)==1

def test_failed_intent_checkpoint_never_reports_cancellation_or_stops(context,monkeypatch):
    p,r,s,a,spec,plan=context;p.lost=True;submit(context)
    before=r.get(a,plan['operation_id']);calls=[];p.cancel_job=lambda *args:calls.append(args)
    def fail(*args):raise RuntimeError('intent checkpoint failed')
    monkeypatch.setattr(r,'put',fail)
    with pytest.raises(RuntimeError,match='intent checkpoint'):
        s.cancel(a,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    assert r.get(a,plan['operation_id'])==before and calls==[]


def test_stop_during_last_admission_check_prevents_physical_submission(context):
    p, r, s, a, spec, plan = context
    checks = []

    def guard():
        checks.append(True)
        if len(checks) == 2:
            s.cancel(a, plan['operation_id'], plan['plan_hash'], lambda *_: True)
        return True

    result = s.submit(a, plan['operation_id'], plan['plan_hash'], lambda *_: True,
                      before_submit=guard)
    assert result['phase'] == 'failed' and result['error'] == 'submission_aborted'
    assert p.calls == 0
    assert submit(context)['phase'] == 'failed' and p.calls == 0


def test_cancel_intent_survives_concurrent_submit_claim_without_new_grant(context, monkeypatch):
    p, r, s, a, spec, plan = context
    original = r.put
    injected = []
    stops = []
    p.cancel_job = lambda *args: stops.append(args)

    def racing(actor, op, value, version):
        if value['phase'] == 'cancelled' and not injected:
            injected.append(True)
            submit(context)
        return original(actor, op, value, version)

    monkeypatch.setattr(r, 'put', racing)
    grants = []
    result = s.cancel(a, plan['operation_id'], plan['plan_hash'],
                      lambda *_: grants.append(True) or True)
    assert result['cancellation']['state'] == 'claimed'
    assert p.calls == 1 and len(stops) == 1 and len(grants) == 1


def test_submit_claim_conflict_reads_other_submit_without_external_replay(context, monkeypatch):
    p, r, s, a, spec, plan = context
    original = r.put
    injected = []

    def racing(actor, op, value, version):
        if value['phase'] == 'submitting' and not injected:
            injected.append(True)
            submit(context)
        return original(actor, op, value, version)

    monkeypatch.setattr(r, 'put', racing)
    assert submit(context)['phase'] == 'queued'
    assert p.calls == 1


def test_cancel_claim_conflict_retains_intent_without_replaying_stop(context, monkeypatch):
    p, r, s, a, spec, plan = context
    submit(context)
    p.snapshot = {'state': 'running', 'exit_code': None}
    original = r.put
    injected, stops = [], []
    p.cancel_job = lambda *args: stops.append(args)

    def racing(actor, op, value, version):
        if value['phase'] == 'cancelling' and not injected:
            injected.append(True)
            s.status(a, plan['operation_id'])
        return original(actor, op, value, version)

    monkeypatch.setattr(r, 'put', racing)
    result = s.cancel(a, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    assert result['cancellation']['state'] == 'requested' and not stops
    assert s.progress_cancellation(a, plan['operation_id'])['phase'] == 'cancelling'
    assert len(stops) == 1
