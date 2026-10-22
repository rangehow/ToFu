import io
import json as json
import ssl
import urllib.error  # noqa: F401 - dependency availability check
import pytest as pytest
from tofu_environments import Actor, EnvironmentError
from tofu_meituan_environments import provider_factory, OfficialAgentCredentialSource
from tofu_meituan_environments.transport import PlatformTransport

class Response(io.BytesIO):
    pass

def test_credentials_remain_headers_tls_verified_and_redirects_refused(monkeypatch):
    seen={}
    def opener(*handlers):
        secure=next(h for h in handlers if hasattr(h,'_context'))
        assert secure._context.verify_mode==ssl.CERT_REQUIRED and secure._context.check_hostname
        redirect=next(h for h in handlers if hasattr(h,'redirect_request'))
        assert redirect.redirect_request(None,None,None,None,None,None) is None
        class Open:
            def open(self,request,timeout):
                seen['request']=request
                return Response(b'{"code":0,"data":{"id":1}}')
        return Open()
    monkeypatch.setattr('urllib.request.build_opener',opener)
    transport=PlatformTransport(lambda *_:'SECRET')
    assert transport.request(Actor(11),'example','GET','/mlapi/image/1')=={'id':1}
    request=seen['request'];assert 'SECRET' not in request.full_url and request.get_header('Access-token')=='SECRET'

def test_auth_binding_fails_before_cli_or_platform(monkeypatch):
    with pytest.raises(EnvironmentError,match='owner-bound'):provider_factory(Actor(11),{})
    source=OfficialAgentCredentialSource(Actor(11,'tenant'))
    with pytest.raises(EnvironmentError,match='another owner'):source(Actor(12,'tenant'),'audience')

def test_lost_mutation_response_is_uncertain_and_error_has_no_ticket(monkeypatch):
    class Open:
        def open(self,*args,**kwargs):raise OSError('SECRET')
    monkeypatch.setattr('urllib.request.build_opener',lambda *_:Open())
    with pytest.raises(EnvironmentError) as exc:PlatformTransport(lambda *_:'SECRET').request(Actor(11),'example','POST','/mlapi/image/create',{})
    assert exc.value.uncertain and 'SECRET' not in str(exc.value)

pytestmark = pytest.mark.unit


def test_private_distribution_is_excluded_from_public_export():
    lists=pytest.importorskip('export_pkg._lists')
    assert 'bundles' in lists.OPENSOURCE_EXTRA_EXCLUDE_DIRS
