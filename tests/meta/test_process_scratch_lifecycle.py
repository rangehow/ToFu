"""Process-owned test scratch is reclaimed when its owner process exits."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.support.process_scratch import process_scratch_directory, remove_exited_owners

pytestmark = pytest.mark.unit


def _exited_pid() -> int:
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    return child.pid


def test_a_new_directory_reclaims_those_of_exited_owners_only(tmp_path):
    dead = tmp_path / f'tofu-runtime-sections-{_exited_pid()}-abc'
    (dead / 'bundle').mkdir(parents=True)
    (dead / 'bundle' / 'app.js').write_text('x')
    live = tmp_path / f'tofu-runtime-sections-{os.getppid()}-def'
    live.mkdir()
    other_prefix = tmp_path / f'tofu-frontend-test-root-{_exited_pid()}-ghi'
    other_prefix.mkdir()
    legacy = tmp_path / 'tofu-runtime-sections-k2v77kj1'
    legacy.mkdir()

    created = process_scratch_directory('tofu-runtime-sections-', tmp_path)

    assert created.is_dir() and created.name.startswith(f'tofu-runtime-sections-{os.getpid()}-')
    assert not dead.exists()
    assert live.is_dir() and other_prefix.is_dir() and legacy.is_dir()


def test_a_killed_owner_leaves_nothing_for_the_next_session(tmp_path):
    script = (
        'import os, sys; sys.path.insert(0, sys.argv[2]);'
        'from pathlib import Path;'
        'from tests.support.process_scratch import process_scratch_directory;'
        "d = process_scratch_directory('chat-sidecar-', Path(sys.argv[1]));"
        "(d / 'db').write_text('x'); os._exit(0)")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    subprocess.run([sys.executable, '-c', script, str(tmp_path), root], check=True)
    leaked = list(tmp_path.glob('chat-sidecar-*'))
    assert len(leaked) == 1, 'os._exit skips atexit, which is the leak being reclaimed'

    assert remove_exited_owners('chat-sidecar-', tmp_path) == leaked
    assert not leaked[0].exists()
