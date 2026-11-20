"""A live executor claim survives independent contenders, not process death."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lib.channels import outbox

pytestmark = pytest.mark.unit


def test_wechat_executor_lock_released_on_process_death(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    owner = 41
    child_code = '''
import sys
from types import SimpleNamespace
from lib.channels import outbox
from lib.runtime import runtime_paths
runtime_paths.data_root = lambda: sys.argv[1]
def hold(*args, **kwargs):
    print('locked', flush=True)
    sys.stdin.read()
    return 1
outbox._execute_pass = hold
outbox.execute(41, None, channel='wechat', store=SimpleNamespace(owner=41))
'''
    with subprocess.Popen([sys.executable, '-c', child_code, str(tmp_path)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as child:
        try:
            import queue
            from threading import Thread
            line = queue.Queue()
            Thread(target=lambda: line.put(child.stdout.readline()), daemon=True).start()
            assert line.get(timeout=10) == 'locked\n'
            monkeypatch.setattr(outbox, '_execute_pass', lambda *args, **kwargs: 1)
            store = SimpleNamespace(owner=owner)
            assert outbox.execute(owner, None, channel='wechat', store=store) == 0
        finally:
            child.kill()
            child.wait(timeout=10)
        assert outbox.execute(owner, None, channel='wechat', store=store) == 1
