"""Actor-owned queue query excludes apps and rejects changed binding."""
import pytest
from tofu_environments.contracts import Actor,EnvironmentError
from tofu_meituan_environments.hope import HopeJobProvider
from tofu_environments.jobs import JobService
pytestmark=pytest.mark.unit
def test_hope_catalog_safe_fields_and_no_writes():
    actor=Actor(7,'t','/project');calls=[]
    def call(a,name,args,**kw):
        calls.append((a,name,args));return {'ok':True,'queue':'root.fixture','summary':{'gpu':{'H200':{'total':4,'allocated':2,'remain':2,'pending':1}},'vcores_total':8,'vcores_allocated':2,'vcores_remain':6,'memory_total_mb':64,'memory_allocated_mb':16,'memory_remain_mb':48,'apps':['private']}}
    p=HopeJobProvider(actor,call,None,None,None,catalog_profile={'queue':'root.fixture','usergroup':'group','image':'secret-image'})
    result=JobService(None,lambda *_:p).catalog(actor,'hope')
    assert calls==[(actor,'mcp__hope__get_queue_resource',{'queue':'root.fixture','include_apps':False})]
    assert len(result['queues'][0]['resources'])==3 and 'secret-image' not in str(result) and 'private' not in str(result)
    with pytest.raises(EnvironmentError):p.compute_catalog(Actor(8,'t','/project'))
    with pytest.raises(EnvironmentError):p.compute_catalog(Actor(7,'t','/foreign-project'))
    assert len(calls)==1
