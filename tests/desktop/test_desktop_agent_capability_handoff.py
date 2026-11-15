"""The packaged app's in-process agent must be accepted by its server child.

The launcher (tray process) runs the desktop agent and spawns the server as a
separate child process. Before 2026-11-10 each process minted its own random
capability, so every local ``/api/desktop/poll`` was refused and enabling
computer control from the role window could never link.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

_CHILD = r'''
import os
from lib.bridge_auth import process_agent_token, resolve_bridge_credential
assert 'TOFU_DESKTOP_AGENT_TOKEN' not in os.environ, 'capability leaks to children'
token = os.environ['EXPECTED']
assert process_agent_token() == token
context = resolve_bridge_credential(token, allow_process_agent=True)
assert context is not None and 'agents:bridge' in context.scopes
assert resolve_bridge_credential(token, allow_process_agent=False) is None
print('OK')
'''


def test_server_child_accepts_the_launcher_capability():
    sys.path.insert(0, str(_ROOT))
    from desktop import launcher
    from lib.desktop_wire.process_agent_token import process_agent_token

    _argv, env = launcher._server_command(15999)
    token = process_agent_token()
    assert env['TOFU_DESKTOP_AGENT_TOKEN'] == token
    env = dict(env, EXPECTED=token, PYTHONPATH=str(_ROOT))
    proc = subprocess.run([sys.executable, '-c', _CHILD], cwd=_ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0 and 'OK' in proc.stdout, proc.stderr[-2000:]


def test_launcher_environment_never_carries_the_capability():
    from lib.desktop_wire.process_agent_token import child_environment
    assert 'TOFU_DESKTOP_AGENT_TOKEN' not in os.environ
    assert set(child_environment()) == {'TOFU_DESKTOP_AGENT_TOKEN'}
