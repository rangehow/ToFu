"""Hope wire contract and fail-closed trusted source/evidence boundaries."""
import pytest
from tofu_environments import Actor, EnvironmentError
from tofu_meituan_environments.hope import HopeJobProvider, hope_provider_factory
pytestmark=pytest.mark.unit

@pytest.fixture
def setup():
    actor=Actor(1,'tenant','/project');calls=[]
    prepared={'workdir':'/sealed','include':['gpu_smoke.py','.hope'],'source_sha256':'a'*64,
              'configuration_sha256':'b'*64,'prepared_sha256':'d'*64,'limits_enforced':True,'binding_revision':'revision-1'}
    spec={'source':{'sha256':'a'*64},'provider_options':{'usergroup':'group','job_type':'gpu','queue':'queue','priority':'P0','resource_code':'code'}}
    def call(actor,name,args,**kwargs):
        calls.append((actor,name,args,kwargs));return {'ok':True,'hope_run_id':'run-1'}
    provider=HopeJobProvider(actor,call,lambda *args,**kw:dict(prepared),lambda *_:{'state':'succeeded','exit_code':0},lambda *_:iter([b'ok']))
    return actor,calls,prepared,spec,provider

def test_submit_wire_has_marker_explicit_binding_and_no_write_replay(setup):
    a,c,p,s,provider=setup;admission=provider.validate_job(a,s,operation_id='job-1')
    assert provider.submit_job(a,s,'job-1',admission)=={'kind':'hope_run_id','id':'run-1'}
    assert c[0][1]=='mcp__hope__submit_job'
    assert c[0][2]['retry_on_quota'] is False and c[0][3]['retry_on_disconnect'] is False
    assert c[0][2]['annotation'].startswith('tofu:')

def test_missing_ports_and_foreign_owner_fail_closed(setup):
    a,c,p,s,provider=setup
    with pytest.raises(EnvironmentError):hope_provider_factory(a,{})
    with pytest.raises(EnvironmentError):provider.validate_job(Actor(2),s,operation_id='job-1')
    assert not c

def test_preparation_change_prevents_submission(setup):
    a,c,p,s,provider=setup;admission=provider.validate_job(a,s,operation_id='job-1');p['configuration_sha256']='c'*64
    with pytest.raises(EnvironmentError):provider.submit_job(a,s,'job-1',admission)
    assert not c

@pytest.mark.parametrize('change',[{'limits_enforced':False},{'source_sha256':'c'*64},{'include':[]}])
def test_no_allocation_without_sealed_bounded_source(setup,change):
    a,c,p,s,provider=setup;p.update(change)
    with pytest.raises(EnvironmentError):provider.validate_job(a,s,operation_id='job-1')
    assert not c

def test_unknown_submission_is_uncertain(setup):
    a,c,p,s,provider=setup;provider.call=lambda *args,**kw: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(EnvironmentError) as error:provider.submit_job(a,s,'job-1',provider.validate_job(a,s,operation_id='job-1'))
    assert error.value.uncertain

def test_wrong_identifier_kind_not_sent(setup):
    a,c,p,s,provider=setup
    with pytest.raises(EnvironmentError):provider.job_status(a,{'kind':'app_id','id':'x'},'job-1')
    assert not c

def test_status_uses_actual_hope_full_detail_contract(setup):
    a,c,p,s,provider=setup
    assert provider.job_status(a,{'kind':'hope_run_id','id':'run-1'},'job-1')=={'state':'succeeded','exit_code':0}
    assert c[0][1]=='mcp__hope__get_job'
    assert c[0][2]=={'hope_run_id':'run-1','detail':'full'}
    assert c[0][3]['retry_on_disconnect'] is True

def test_cancel_uses_one_use_platform_confirmation_without_replay(setup):
    a,c,p,s,provider=setup
    def call(actor,name,args,**kw):
        c.append((actor,name,args,kw))
        return {'ok':True,'confirmation_token':'one-use'}
    provider.call=call
    provider.cancel_job(a,{'kind':'hope_run_id','id':'run-1'},'job-1')
    assert [x[1] for x in c]==['mcp__hope__get_job','mcp__hope__stop_job','mcp__hope__stop_job']
    assert c[1][2]['dry_run'] is True and c[2][2]['dry_run'] is False
    assert c[2][2]['confirmation_token']=='one-use' and c[2][3]['retry_on_disconnect'] is False

def test_verification_timeout_preserves_known_run_for_observation(setup):
    a,c,p,s,provider=setup
    provider.call=lambda *args,**kw:'MCP Error: {"ok":false,"hope_run_id":"run-7","state":"SUBMITTING","accepted":false}'
    assert provider.submit_job(a,s,'job-1',provider.validate_job(a,s,operation_id='job-1'))=={'kind':'hope_run_id','id':'run-7'}

def test_proven_platform_presubmission_rejection_is_not_unknown_write(setup):
    a,c,p,s,provider=setup
    provider.call=lambda *args,**kw:{'ok':False,'hope_run_id':'run-7','state':'UNSUBMITTED','app_id':'','verify':{'verdict':'submission_failed'}}
    with pytest.raises(EnvironmentError) as error:provider.submit_job(a,s,'job-1',provider.validate_job(a,s,operation_id='job-1'))
    assert not error.value.uncertain
