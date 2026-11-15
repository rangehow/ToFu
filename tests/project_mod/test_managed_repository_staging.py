"""Platform staging never reads what a managed repository's content policy never versions.

Preparation shares bulky data instead of versioning it, but data a worker (or
person) creates later is only ever seen by staging: task checkpoints, work in
progress saves and task-end linear checkpoints. Each must classify it by the
same recorded policy and leave it unread, while sources are versioned as before.
"""
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

from lib import integration_control as ic
from lib import linear_git_checkpoint
from lib.project_mod import lanes
from lib.project_mod.managed_repository import ContentPolicy, prepare_managed_repository
from lib.project_mod.task_delta_delivery import checkout_reconstructible

pytestmark = pytest.mark.unit

SMALL = ContentPolicy(large_file_bytes=1024, bulk_directory_files=5)
SOURCES = {'code/train.py': b'print("train")\n', 'results/metrics.json': b'{"loss": 1}\n'}


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def stored(root, data):
    """Whether Git hashed DATA into the repository's object store."""
    oid = hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()
    return subprocess.run(['git', '-C', str(root), 'cat-file', '-e', oid],
                          capture_output=True, timeout=60).returncode == 0


def listed(root, tree):
    return set(git(root, 'ls-tree', '-r', '--name-only', tree).split())


def write_run_outputs(checkout, *, readable=True):
    """A training run's outputs: a large file and a bulky directory, beside small sources."""
    for path, data in SOURCES.items():
        write(checkout / path, data)
    outputs = {'results/model.safetensors': os.urandom(4 * SMALL.large_file_bytes)}
    outputs.update({f'results/grid/runs/shard{index}.bin': os.urandom(64)
                    for index in range(SMALL.bulk_directory_files + 1)})
    for path, data in outputs.items():
        write(checkout / path, data)
        if not readable and path.endswith('.bin'):
            # Reading any of these fails; staging must complete without trying.
            (checkout / path).chmod(0)
    return outputs


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / 'knowledge'
    write(root / 'notes.md', b'# notes\n')
    prepare_managed_repository(str(root), policy=SMALL)
    return root.resolve()


@pytest.fixture
def worker(folder, tmp_path):
    checkout = tmp_path / 'worker'
    git(folder, 'worktree', 'add', '-q', '--detach', str(checkout), 'HEAD')
    return checkout


def assert_sources_only(folder, tree, outputs):
    paths = listed(folder, tree)
    assert {'notes.md', *SOURCES} <= paths
    assert not paths & {*outputs, 'results/grid', 'results/grid/runs'}
    assert not any(stored(folder, data) for data in outputs.values())


def test_task_checkpoint_never_reads_data_the_content_policy_shares(folder, worker):
    outputs = write_run_outputs(worker, readable=False)
    base = git(worker, 'rev-parse', 'HEAD').strip()

    checkpoint = ic._alternate_index_checkpoint(worker, base, 'task')

    assert_sources_only(folder, checkpoint, outputs)
    assert all(os.path.lexists(worker / path) for path in outputs)


def test_a_checkout_holding_withheld_data_is_never_reclaimed(folder, worker):
    outputs = write_run_outputs(worker)
    base = git(worker, 'rev-parse', 'HEAD').strip()

    checkpoint = ic._alternate_index_checkpoint(worker, base, 'task')
    assert not checkout_reconstructible(folder, worker, checkpoint)

    # Only the withheld data kept it: without it the same checkout is reconstructible.
    shutil.rmtree(worker / 'results' / 'grid')
    (worker / 'results' / 'model.safetensors').unlink()
    checkpoint = ic._alternate_index_checkpoint(worker, base, 'task')
    assert checkout_reconstructible(folder, worker, checkpoint)
    assert set(outputs).isdisjoint(listed(folder, checkpoint))


def test_work_in_progress_save_never_reads_withheld_data(folder, worker):
    outputs = write_run_outputs(worker, readable=False)

    _head, tree = lanes.capture_working_tree(worker)

    assert_sources_only(folder, tree, outputs)


@pytest.mark.serial
def test_task_end_linear_checkpoint_commits_sources_but_never_bulky_data(
        folder, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT', '1')
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', f'{sys.executable} -c pass')
    monkeypatch.setattr(linear_git_checkpoint, '_lock_path',
                        lambda root: tmp_path / f'{root.name}.checkpoint.lock')
    outputs = write_run_outputs(folder, readable=False)

    result = linear_git_checkpoint.settle_task_checkpoint(
        {'id': 'task-run', 'convId': 'conv-run', '_userId': 7, 'status': 'done',
         'error': '', 'aborted': False, 'finishReason': 'stop'},
        user_id=7, project_path=str(folder), project_paths=None)

    row = result['repositories'][0]
    assert row['status'] == 'committed', row
    assert row['captureConsistent'] is True
    assert_sources_only(folder, row['checkpointSha'], outputs)
    assert all(os.path.lexists(folder / path) for path in outputs)


def test_a_repository_tofu_did_not_prepare_keeps_its_own_staging(tmp_path):
    root = tmp_path / 'project'
    write(root / 'README.md', b'readme\n')
    git(root, 'init', '-q')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'add', '.')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'base')
    data = os.urandom(4 * SMALL.large_file_bytes)
    write(root / 'asset.bin', data)

    tree = ic._alternate_index_tree(root, git(root, 'rev-parse', 'HEAD').strip(),
                                    str(tmp_path / 'index'))

    assert 'asset.bin' in listed(root, tree) and stored(root, data)
