"""Reject foreign or unsafe profiles before reading another MCP account."""
import json
import pytest
from tofu_environments.contracts import Actor,EnvironmentError
from tofu_meituan_environments.hope_host import load_host_binding
pytestmark=pytest.mark.unit
@pytest.fixture
def profile(tmp_path,monkeypatch):
    import lib.runtime.runtime_paths
    import lib.mcp.config
    a=Actor(7,'tenant',str(tmp_path/'project'));p=tmp_path/'config/compute/hope'/f'{a.scope}.json';p.parent.mkdir(parents=True)
    d={'version':1,'owner_id':7,'tenant_id':'tenant','projects':[a.project_root],'profile':{},'artifact_profile':{},'expected_creator':'fixture','environment':{},'mcp_timeout_seconds':30,'output_timeout_seconds':300}
    p.write_text(json.dumps(d));p.chmod(0o600);calls=[]
    monkeypatch.setattr(lib.runtime.runtime_paths,'data_root',lambda:str(tmp_path))
    def config(*,user_id):calls.append(user_id);return {'hope':{'fixture':True,'enabled':True}}
    monkeypatch.setattr(lib.mcp.config,'load_mcp_config',config)
    return a,p,d,calls

def test_explicit_owner_profile_and_mcp_config(profile):
    a,p,d,c=profile;b=load_host_binding(a)
    assert b['scope']==a.scope and b['project_root']==a.project_root and b['mcp_configuration']['fixture'] is True
    assert c==[7]

@pytest.mark.parametrize('change',['owner','tenant','project','mode','duplicate','symlink','missing','oversize'])
def test_invalid_profile_never_reads_mcp_credentials(profile,change):
    a,p,d,c=profile
    if change=='owner':d['owner_id']=8
    if change=='tenant':d['tenant_id']='foreign'
    if change=='project':d['projects']=['/other']
    p.write_text(json.dumps(d))
    if change=='mode':p.chmod(0o644)
    if change=='duplicate':p.write_text('{"version":1,"version":1}')
    if change=='symlink':target=p.with_suffix('.target');p.rename(target);p.symlink_to(target)
    if change=='missing':p.unlink()
    if change=='oversize':p.write_bytes(b' '*65537)
    with pytest.raises(EnvironmentError):load_host_binding(a)
    assert not c

def test_missing_profile_is_visible_configuration_error(profile):
    a,p,d,c=profile;p.unlink()
    with pytest.raises(EnvironmentError) as e:load_host_binding(a)
    assert e.value.code=='hope_profile_missing'

def test_default_entrypoint_composes_private_profile_without_callback_injection(profile):
    from contextlib import ExitStack
    from tofu_environments.repository import MemoryRepository
    from tofu_meituan_environments.hope import hope_provider_factory
    a,p,d,c=profile
    d['profile']={'usergroup':'group','queue':'root.queue','image':'registry/image','gpu_resource_key':'worker.gcores-example'}
    d['artifact_profile']={'root_uri':'viewfs://cluster/owned','command':['/opt/hadoop/bin/hadoop']}
    p.write_text(json.dumps(d))
    with ExitStack() as stack:
        provider=hope_provider_factory(a,{'repository':MemoryRepository(),'register_cleanup':stack.callback,'context_byte_limit':1048576,'context_file_limit':64})
        assert provider.call.bridge is None and c==[7]
    assert provider.prepare.closed and provider.call.closed


@pytest.mark.parametrize('change',['hash','threshold','missing','extra'])
def test_invalid_parallel_upload_descriptor_rejected_before_MCP_config(profile,change):
    a,p,d,c=profile
    upload={'jar_path':'/owned/uploader.jar','sha256':'a'*64,'threshold_bytes':134217728}
    if change=='hash':upload['sha256']='not-a-pin'
    if change=='threshold':upload['threshold_bytes']=0
    if change=='missing':del upload['jar_path']
    if change=='extra':upload['unsealed']='ignored'
    d['artifact_profile']['parallel_upload']=upload;p.write_text(json.dumps(d))
    with pytest.raises(EnvironmentError):load_host_binding(a)
    assert not c
