import json
from tofu_environments import Actor
from tofu_meituan_environments.workspace import WorkspaceRuntime

class Platform:
    cancelled=staticmethod(lambda:False)
    token_source=staticmethod(lambda *args:'private-platform-ticket')
class Socket:
    def __init__(self):self.closed=False;self.frames=[]
    def send(self,message):
        command=json.loads(message)[1]
        marker=command.split('echo ')[-1].strip()
        self.frames=[json.dumps(['stdout',command]),json.dumps(['stdout','\r\n'+marker+'\r\n'])]
    def recv(self):return self.frames.pop(0)
    def close(self):self.closed=True

def test_bootstrap_does_not_accept_terminal_echo_as_execution_ack_and_cleans_up():
    sock=Socket();runtime=WorkspaceRuntime(Platform(),websocket_factory=lambda *a,**k:sock)
    calls=[]
    def request(url,token,method='GET',payload=None,extra_headers=None):
        calls.append((url,method));return {'name':'term'}
    runtime._request=request
    spec={'workspace':{'startup_command':['python','serverctl.py','start'],'service_port':15000}}
    detail={'dockerUid':'uid','cluster':'zw05_training_cluster','token':'private-jupyter-ticket'}
    runtime.start(Actor(11),spec,detail,'operation')
    assert not sock.frames and sock.closed
    assert calls[-1][1]=='DELETE'
    assert runtime.service_url(spec,detail)=='https://uid-vscode-zw05.mlp.sankuai.com/proxy/15000/'

import pytest
pytestmark = pytest.mark.unit
