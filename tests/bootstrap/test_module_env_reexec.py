"""Environment selection preserves Python module entry points."""
import json
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('owner', ['server', 'bootstrap', 'entry'])
def test_module_launch_survives_environment_reexec(tmp_path, monkeypatch, owner):
    from bootstrap_pkg import env_reexec, server_entry
    from lib.server import server_boot_env
    from serverctl_pkg import runtime_replica
    target = tmp_path / 'bin/python'
    target.parent.mkdir()
    target.write_text('interpreter')
    target.chmod(0o755)
    (tmp_path / '.tofu_env.json').write_text(json.dumps({
        'python': str(target), 'env_prefix': str(tmp_path), 'backend': 'uv'}))
    monkeypatch.setattr(sys.modules['__main__'], '__spec__',
                        SimpleNamespace(name='serverctl_pkg.execution_host'))
    monkeypatch.setattr(sys, 'argv', ['/project/serverctl_pkg/execution_host.py', 'serve', '--manager-fd', '4'])
    monkeypatch.setattr(runtime_replica, 'runs_marked_environment', lambda *_: False)
    monkeypatch.setattr(server_boot_env, '_hold_runtime_replica_leases', lambda: None)
    monkeypatch.setattr(server_boot_env, '_tofu_export_env_native_paths', lambda *_: None)
    monkeypatch.setattr(env_reexec, '_tofu_export_env_native_paths', lambda *_: None)
    monkeypatch.delenv('_TOFU_ENV_REEXEC', raising=False)
    monkeypatch.delenv('TOFU_DISABLE_ENV_REEXEC', raising=False)
    monkeypatch.setattr(server_boot_env, '_PROJ_DIR', str(tmp_path))
    monkeypatch.setattr(env_reexec, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(server_entry, '_PROJ_DIR', str(tmp_path))
    for key in ('TOFU_SERVER_WORKER', '_TOFU_VIA_BOOTSTRAP', 'TOFU_RUN_SERVER', '_TOFU_REEXEC_PORT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server_entry.os, 'getpid', lambda: 42)
    class Reexec(BaseException):
        pass
    calls = []
    def execv(path, argv):
        calls.append((path, argv))
        raise Reexec
    monkeypatch.setattr(env_reexec.os, 'execv', execv)
    with pytest.raises(Reexec):
        if owner == 'server':
            server_boot_env._tofu_maybe_reexec_into_env()
        elif owner == 'bootstrap':
            env_reexec._tofu_maybe_reexec_into_env()
        else:
            server_entry.delegate_server_start([])
    assert calls == [(str(target), [str(target), '-m', 'serverctl_pkg.execution_host',
                                   'serve', '--manager-fd', '4'])]



def test_script_environment_reexec_keeps_forwarded_arguments(monkeypatch):
    from bootstrap_pkg.env_reexec import environment_reexec_argv
    monkeypatch.setattr(sys.modules['__main__'], '__spec__', None)
    monkeypatch.setattr(sys, 'argv', ['/project/server.py', '--port', '18080'])
    assert environment_reexec_argv('/env/python') == [
        '/env/python', '/project/server.py', '--port', '18080']
