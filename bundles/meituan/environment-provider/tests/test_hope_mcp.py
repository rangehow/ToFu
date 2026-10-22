"""Owner binding, mutation uncertainty, lazy startup and serialized teardown."""
from threading import Event, Thread
import pytest
from tofu_environments import Actor, EnvironmentError
from tofu_meituan_environments.hope_mcp import HopeMCPPort
pytestmark=pytest.mark.unit

class Bridge:
    def __init__(self):self.calls=[];self.closed=False
    def connect_server(self,*args):self.calls.append(('connect',args))
    def call_tool(self,*args,**kwargs):self.calls.append(('call',args,kwargs));return {'ok':True}
    def disconnect_all(self):self.closed=True

def fixture():
    actor=Actor(42,'tenant','/owned');bridge=Bridge();observed=[]
    def factory(a):observed.append(a);return bridge
    return actor,bridge,observed,HopeMCPPort(actor,factory,lambda a:{'owned_scope':a.scope},timeout_seconds=30)

def test_lazy_owner_scoped_connection_and_reuse():
    a,b,seen,p=fixture();assert not seen
    p(a,'mcp__hope__get_job',{'hope_run_id':'r'});p(a,'mcp__hope__get_job',{'hope_run_id':'r'})
    assert seen==[a] and sum(x[0]=='connect' for x in b.calls)==1
    assert b.calls[-1][2]=={'timeout_override':30,'retry_on_disconnect':False}
    p.close();assert b.closed

def test_foreign_actor_project_and_undeclared_tool_never_connect():
    a,b,seen,p=fixture()
    for foreign in [Actor(43,'tenant','/owned'),Actor(42,'tenant','/other')]:
        with pytest.raises(EnvironmentError):p(foreign,'mcp__hope__get_job',{})
    with pytest.raises(EnvironmentError):p(a,'mcp__hope__arbitrary_command',{})
    assert not seen

def test_mutation_replay_refused_before_connection():
    a,b,seen,p=fixture()
    with pytest.raises(EnvironmentError) as e:p(a,'mcp__hope__submit_job',{},retry_on_disconnect=True)
    assert e.value.code=='hope_replay_forbidden' and not seen

def test_lost_write_uncertain_and_not_replayed():
    a,b,seen,p=fixture()
    def fail(*args,**kwargs):b.calls.append(('write',));raise TimeoutError('credential-shaped private message')
    b.call_tool=fail
    with pytest.raises(EnvironmentError) as e:p(a,'mcp__hope__submit_job',{})
    assert e.value.uncertain and 'private' not in str(e.value)
    assert sum(x[0]=='write' for x in b.calls)==1
    p.close()

def test_failed_connection_disposes_partial_bridge():
    a,b,seen,p=fixture()
    def fail(*args):raise RuntimeError('secret')
    b.connect_server=fail
    with pytest.raises(EnvironmentError):p(a,'mcp__hope__get_job',{})
    assert b.closed and p.bridge is None

def test_close_waits_for_active_call():
    a,b,seen,p=fixture();entered=Event();release=Event();closed=Event();failures=[]
    def call(*args,**kwargs):entered.set();assert release.wait(2);return {'ok':True}
    b.call_tool=call
    def invoke():
        try:p(a,'mcp__hope__submit_job',{})
        except BaseException as e:failures.append(e)
    worker=Thread(target=invoke);worker.start();assert entered.wait(2)
    def close():p.close();closed.set()
    closer=Thread(target=close);closer.start();assert not closed.wait(.05);assert not b.closed
    release.set();worker.join(2);closer.join(2);assert not worker.is_alive() and not closer.is_alive() and not failures and b.closed
    with pytest.raises(EnvironmentError):p(a,'mcp__hope__get_job',{})
