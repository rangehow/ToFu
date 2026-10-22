"""MLP/CodeLab source-compatible fixtures; these do not claim live allocation."""
from copy import deepcopy
import json
import pytest
pytestmark = pytest.mark.unit
from tofu_environments import Actor, EnvironmentError, EnvironmentService, MemoryRepository
from tofu_meituan_environments import MlpProvider

class Platform:
    origin='https://mlp.sankuai.com'
    cancelled=staticmethod(lambda:False)
    token_source=staticmethod(lambda *args:'SECRET_TICKET')
    def __init__(self):self.calls=[];self.image=None;self.workspace=None;self.running=0;self.lost=False
    def request(self,actor,project,method,path,payload=None,params=None):
        self.calls.append((actor.scope,project,method,path,deepcopy(payload),params))
        if path=='/mlapi/imageinfo/list':return {'items':[{'imageId':1,'id':2,'imageName':'base:1'}]}
        if path=='/mlapi/image/2':return {'imageId':1,'imageName':'base:1','buildStatus':'Pushed'}
        if path=='/mlapi/codelab/docker/queue':return [{'name':'queue'}]
        if path=='/mlapi/image/create':
            self.image={'id':3,'imageId':3,'imageName':'registryonline-hulk.sankuai.com/example/image:operation','buildStatus':'Pending','comment':payload['comment']}
            if self.lost:raise EnvironmentError('timeout','response lost',uncertain=True)
            return 3
        if path=='/mlapi/image/3':return self.image
        if path=='/mlapi/image/3/build':self.image['buildStatus']='Pushed';return True
        if path=='/mlapi/codelab/image/list':return [{'imageAddress':self.image['imageName']}]
        if path=='/mlapi/codelab/list':
            assert params['pageSize']==100 and 'size' not in params
            if params.get('status')==20:return {'items':[],'totalCount':self.running}
            return {'items':[self.workspace] if self.workspace else []}
        if path=='/mlapi/codelab' and method=='PUT':
            self.workspace={**payload,'id':7,'status':20,'ideStatus':[],'token':'SECRET_WORKSPACE','dockerUid':'workspace-id','cluster':'zw05_training_cluster'}
            return {'id':7}
        if path=='/mlapi/codelab/7':return self.workspace
        if '/ide/' in path:
            kind=int(path.split('/')[-2]);self.workspace['ideStatus'].append({'devTool':kind,'status':0});return True
        if path=='/mlapi/image/list':
            assert params['type']=='1' and params['pageSize']==100 and params['manual']=='false'
            return {'items':[self.image]}
        raise AssertionError((method,path,payload,params))

class Runtime:
    def __init__(self):self.started=0;self.is_ready=False
    def start(self,*args):self.started+=1
    def ready(self,*args):return self.is_ready
    def service_url(self,*args):return 'https://service.example/proxy/15000/'

@pytest.fixture
def spec():return {'profile':'project_development','project':'example','provider_options':{'hadoop_account':'approved-account'},'image':{'base':'1','base_revision':'2','name':'project-image','instructions':'RUN true'},'workspace':{'name':'project','cpu':8,'memory_gib':28,'queue':'queue'}}

def flow(spec):
    platform=Platform();runtime=Runtime();provider=MlpProvider(platform,workspace_runtime=runtime)
    service=EnvironmentService(MemoryRepository(),lambda actor,key:provider);actor=Actor(11,'tenant')
    plan=service.plan(actor,'mlp',spec,'request')
    apply=lambda:service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)
    return platform,runtime,provider,service,actor,plan,apply

def test_complete_flow_uses_verified_address_personal_scope_and_gib(spec):
    platform,runtime,provider,service,actor,plan,apply=flow(spec)
    assert apply()['phase']=='image_created';assert apply()['phase']=='building'
    assert service.status(actor,plan['operation_id'])['phase']=='image_ready'
    assert apply()['phase']=='workspace_created';assert apply()['phase']=='activating'
    result=service.status(actor,plan['operation_id']);assert result['phase']=='ready'
    write=[x for x in platform.calls if x[2]=='PUT'][0][4]
    assert write['image']==platform.image['imageName'] and write['memory']==28 and write['scope']==0
    assert write['dolphinfsUserType']==0 and write['gpuNumber']==0
    assert 'SECRET' not in json.dumps(result)
    assert all(x[0]==actor.scope and x[1]=='example' for x in platform.calls)

def test_service_not_ready_until_application_probe_passes(spec):
    spec['profile']='tofu_service';spec['workspace'].update(startup_command=['python','/opt/tofu/serverctl.py','start'],service_port=15000)
    platform,runtime,provider,service,actor,plan,apply=flow(spec)
    apply();apply();service.status(actor,plan['operation_id']);apply();apply()
    assert runtime.started==1
    assert service.status(actor,plan['operation_id'])['phase']=='activating'
    runtime.is_ready=True
    assert service.status(actor,plan['operation_id'])['phase']=='ready'
    assert runtime.started==1

def test_workspace_limit_and_not_ready_image_block_allocations(spec):
    platform,runtime,provider,service,actor,plan,apply=flow(spec)
    apply();apply();service.status(actor,plan['operation_id']);platform.running=2
    assert apply()['phase']=='failed'
    assert not any(x[2]=='PUT' for x in platform.calls)

def test_lost_image_ack_reconciles_exact_marker_once(spec):
    platform,runtime,provider,service,actor,plan,apply=flow(spec);platform.lost=True
    assert apply()['phase']=='uncertain'
    assert service.status(actor,plan['operation_id'])['phase']=='image_created'
    assert len([x for x in platform.calls if x[3]=='/mlapi/image/create'])==1

def test_mutated_owner_markers_are_rejected(spec):
    platform,runtime,provider,service,actor,plan,apply=flow(spec)
    apply();apply();service.status(actor,plan['operation_id']);apply()
    platform.workspace['otherParam']='pc.env.TOFU_ENV_OWNER=another-owner'
    assert apply()['phase']=='failed'
    assert not any('/ide/' in x[3] for x in platform.calls)

def test_base_version_and_shared_options_fail_before_writes(spec):
    platform=Platform();provider=MlpProvider(platform,workspace_runtime=Runtime());actor=Actor(11)
    spec['image'].pop('base_revision')
    with pytest.raises(EnvironmentError,match='immutable version'):provider.validate(actor,spec)
    spec['image']['base_revision']='2';spec['provider_options']={'scope':'shared'}
    with pytest.raises(EnvironmentError,match='Unknown'):provider.validate(actor,spec)
    assert not any(x[2]!='GET' for x in platform.calls)


def test_catalog_preserves_base_and_version_ids(spec):
    platform=Platform();provider=MlpProvider(platform)
    result=provider.catalog(Actor(11),spec['project'],'base')
    assert result['images'][0]['base']=='1' and result['images'][0]['revision']=='2'
    assert result['queues'][0]['id']=='queue'
    assert platform.calls[0][-1]['keyword']=='base' and platform.calls[0][-1]['pageSize']==20

def test_account_is_explicit_not_guessed_from_project(spec):
    spec['provider_options']={}
    with pytest.raises(EnvironmentError,match='not inferred'):MlpProvider(Platform()).validate(Actor(11),spec)

def test_workspace_markers_require_exact_values_and_project(spec):
    actor=Actor(11);provider=MlpProvider(Platform());detail={'otherParam':'pc.env.TOFU_ENV_OPERATION=op-suffix\npc.env.TOFU_ENV_OWNER='+actor.scope}
    assert not provider._owns(actor,detail,'op')
    detail['otherParam']='pc.env.TOFU_ENV_OPERATION=op\npc.env.TOFU_ENV_OWNER='+actor.scope
    assert provider._owns(actor,detail,'op')
    detail['otherParam']+='\npc.env.TOFU_ENV_OPERATION=op'
    assert not provider._owns(actor,detail,'op')

def test_malformed_create_ack_keeps_recoverable_uncertainty(spec):
    platform=Platform();original=platform.request
    def malformed(actor,project,method,path,payload=None,params=None):
        result=original(actor,project,method,path,payload,params)
        return None if path=='/mlapi/image/create' else result
    platform.request=malformed
    service=EnvironmentService(MemoryRepository(),lambda *_:MlpProvider(platform));actor=Actor(11)
    plan=service.plan(actor,'mlp',spec,'malformed')
    assert service.apply(actor,plan['operation_id'],plan['plan_hash'],lambda *_:True)['phase']=='uncertain'
    assert service.status(actor,plan['operation_id'])['phase']=='image_created'
    assert sum(x[3]=='/mlapi/image/create' for x in platform.calls)==1
