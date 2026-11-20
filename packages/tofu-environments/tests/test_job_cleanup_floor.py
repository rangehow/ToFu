"""Cleanup uses Python-3.10 descriptor APIs, without changing the SDK floor."""
from copy import deepcopy
import shutil
import pytest
import test_jobs

context = test_jobs.context
pytestmark = pytest.mark.unit


def test_failed_nested_collection_keeps_primary_error_and_reclaims_snapshot(context, tmp_path, monkeypatch):
    p, repo, service, actor, spec, plan = context
    changed = deepcopy(spec)
    changed['outputs'] = ['nested/result.json']
    plan = service.plan(actor, 'hope', changed, 'nested-cleanup')
    service.submit(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    service.status(actor, plan['operation_id'])
    def read(*_):
        yield b'partial'
        raise ValueError('primary provider read failure')
    p.read_job_output = read
    # Match 3.10's signature; calling dir_fd on it must not be part of cleanup.
    def legacy_rmtree(path, ignore_errors=False, onerror=None):
        raise AssertionError('path-based rmtree is not descriptor-relative')
    monkeypatch.setattr(shutil, 'rmtree', legacy_rmtree)
    with pytest.raises(ValueError, match='primary provider read failure'):
        service.collect(actor, plan['operation_id'], tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert 'outputs' not in repo.get(actor, plan['operation_id'])['value']


def test_actual_python310_collection_cleanup(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    python = sys.executable if sys.version_info[:2] == (3, 10) else shutil.which('python3.10')
    if python is None:
        pytest.skip('Python 3.10 interpreter unavailable; floor execution remains unverified')
    script = '''
import sys
assert sys.version_info[:2] == (3, 10), sys.version
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from tofu_environments.contracts import Actor, seal
from tofu_environments.jobs import JobService
from tofu_environments.repository import MemoryRepository
actor = Actor(1, "fixture", sys.argv[2])
repo = MemoryRepository()
plan = {"scope": actor.scope, "project_root": actor.project_root, "provider": "fixture",
        "spec": {"outputs": ["nested/result.json"], "limits": {"result_bytes": 100}}, "admission": {}}
record = {"operation_id": "job-fixture", "plan": plan, "plan_hash": seal(plan),
          "phase": "succeeded", "reference": {"kind": "fixture", "id": "one"}}
repo.put(actor, "job-fixture", record, 0)
class Provider:
    def read_job_output(self, *args):
        yield b"partial"
        raise ValueError("primary floor failure")
service = JobService(repo, lambda *_: Provider())
try:
    service.collect(actor, "job-fixture", Path(sys.argv[2]))
except ValueError as error:
    assert str(error) == "primary floor failure"
else:
    raise AssertionError("primary failure was not preserved")
assert list(Path(sys.argv[2]).iterdir()) == []
print("3.10 cleanup verified")
'''
    result = subprocess.run([python, '-I', '-c', script, str(Path(__file__).resolve().parents[1]),
                             str(tmp_path)], capture_output=True, text=True,
                            env={'PATH': os.defpath, 'HOME': str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '3.10 cleanup verified'


def test_cleanup_failure_preserves_primary_as_raised_exception(tmp_path, monkeypatch):
    import os
    from tofu_environments.job_files import OutputDirectory
    with OutputDirectory(tmp_path) as root:
        os.mkdir('snapshot', dir_fd=root.fd)
        def failed(*_):
            raise OSError('cleanup IO failure')
        monkeypatch.setattr(root, 'remove_tree', failed)
        with pytest.raises(ValueError, match='primary') as error:
            with root.cleanup_child('snapshot'):
                raise ValueError('primary collection failure')
        assert isinstance(error.value.__cause__, OSError)
        assert str(error.value.__cause__) == 'cleanup IO failure'
