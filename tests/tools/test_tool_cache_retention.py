"""Tool caches under data/mcp-cache are pruned by their own tools after installs."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from lib.project_mod.run_command import _get_cmd_env, tool_run_command
from lib.tools import tool_cache_retention as retention

pytestmark = pytest.mark.unit


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    cache = tmp_path / 'cache'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(cache))
    for key in ('UV_CACHE_DIR', 'PIP_CACHE_DIR', 'XDG_CACHE_HOME', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR'):
        monkeypatch.delenv(key, raising=False)
    return cache


def _files(root: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(root))


def _wheel(directory: Path, payload: str) -> Path:
    path = directory / 'retentionprobe-1.0-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('retentionprobe/__init__.py', payload)
        for index in range(20):
            archive.writestr(f'retentionprobe/module_{index}.py', payload)
        archive.writestr('retentionprobe-1.0.dist-info/METADATA',
                         'Metadata-Version: 2.1\nName: retentionprobe\nVersion: 1.0\n')
        archive.writestr('retentionprobe-1.0.dist-info/WHEEL',
                         'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('retentionprobe-1.0.dist-info/RECORD', '')
    return path


@pytest.mark.parametrize('command, installs', [
    ('uv pip install --target out requests', True),
    ('.venv/bin/pip install -e .', True),
    ('python3 -m pip install x', True),
    ('cd sub && uvx ruff check', True),
    ('echo uvicorn; ls pipeline', False),
    ('git status', False),
])
def test_installer_commands_are_recognized(command, installs):
    assert retention.invokes_package_installer(command) is installs


def test_a_finished_installer_command_schedules_the_prune(tmp_path, cache_root, monkeypatch):
    scheduled = []
    monkeypatch.setattr(retention, 'schedule_prune', scheduled.append)
    tool_run_command(str(tmp_path), 'echo no-installer')
    assert scheduled == []
    tool_run_command(str(tmp_path), 'pip --version || true')
    assert scheduled == [str(cache_root)]


def test_commands_use_a_uv_cache_long_lived_launchers_never_pin(tmp_path, cache_root):
    assert _get_cmd_env(str(tmp_path))['UV_CACHE_DIR'] == str(cache_root / retention.COMMAND_UV_CACHE)


@pytest.mark.skipif(shutil.which('uv') is None, reason='needs the uv executable')
def test_prune_after_each_install_keeps_the_uv_cache_from_regrowing(tmp_path, cache_root):
    uv_cache = cache_root / retention.COMMAND_UV_CACHE
    source = tmp_path / 'dist'
    source.mkdir()
    environment = {**os.environ, 'UV_CACHE_DIR': str(uv_cache), 'UV_LINK_MODE': 'copy'}

    def install(round_number: int) -> None:
        wheel = _wheel(source, f'ROUND = {round_number}\n')
        os.utime(wheel, (1_700_000_000 + round_number,) * 2)
        subprocess.run(['uv', 'pip', 'install', '--offline', '--no-index', '--python', sys.executable,
                        '--target', str(tmp_path / f'target-{round_number}'), str(wheel)],
                       env=environment, check=True, capture_output=True)

    install(0)
    unpruned = [_files(uv_cache)]
    for round_number in range(1, 4):
        install(round_number)
        unpruned.append(_files(uv_cache))
    assert unpruned[-1] > unpruned[0], 'without pruning every install leaves its archive behind'

    pruned = []
    for round_number in range(4, 8):
        install(round_number)
        runs = retention.prune(str(cache_root))
        assert any(run['argv'][1:3] == ['cache', 'prune'] and run['exit'] == 0 for run in runs)
        pruned.append(_files(uv_cache))
    assert max(pruned) <= unpruned[0]
    assert pruned[-1] == pruned[0]


@pytest.mark.skipif(shutil.which('uv') is None, reason='needs the uv executable')
def test_prune_refuses_at_once_while_another_uv_holds_the_cache(cache_root):
    uv_cache = cache_root / retention.COMMAND_UV_CACHE
    uv_cache.mkdir(parents=True)
    descriptor = os.open(uv_cache / '.lock', os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        runs = retention.prune(str(cache_root))
    finally:
        os.close(descriptor)
    uv_runs = [run for run in runs if run['argv'][1:3] == ['cache', 'prune']]
    assert uv_runs and uv_runs[0]['exit'] != 0


@pytest.mark.parametrize('pip_in_venv', [True, False])
def test_prune_purges_the_pip_cache(cache_root, tmp_path, monkeypatch, pip_in_venv):
    if not pip_in_venv:
        # uv sync does not seed pip. Reproduce that layout with a real venv
        # while leaving the pytest runner's own interpreter untouched.
        venv = tmp_path / 'unseeded'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
        python = venv / 'bin' / 'python'
        monkeypatch.delenv('PYTHONPATH', raising=False)
        monkeypatch.setattr(retention.sys, 'executable', str(python))
    wheel = cache_root / 'pip' / 'wheels' / 'ab' / 'cached-1.0-py3-none-any.whl'
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b'cached')
    runs = retention.prune(str(cache_root))
    assert any(run['argv'][1:] == ['-m', 'pip', 'cache', 'purge'] and run['exit'] == 0 for run in runs)
    assert not wheel.exists()


def test_discoverable_pip_failure_does_not_fall_back(cache_root, tmp_path, monkeypatch):
    (cache_root / 'pip').mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pip.py').write_text(
        'import sys\n'
        'print(f"{sys.executable}: No module named pip", file=sys.stderr)\n'
        'raise SystemExit(1)\n')
    [run] = retention.prune(str(cache_root))
    assert run['argv'][0] == sys.executable
    assert run['exit'] == 1
    assert run['detail'] == f'{sys.executable}: No module named pip'


def test_invalid_pip_cache_failure_is_visible(cache_root, monkeypatch):
    (cache_root / 'pip').mkdir(parents=True)
    monkeypatch.setenv('PIP_NO_CACHE_DIR', '1')
    [run] = retention.prune(str(cache_root))
    assert run['argv'][0] == sys.executable
    assert run['exit'] != 0
    assert 'cache is disabled' in run['detail']


def test_concurrent_requests_coalesce_into_one_more_prune(cache_root, monkeypatch):
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def fake_prune(root):
        calls.append(root)
        started.set()
        if len(calls) == 1:
            release.wait(10)
        else:
            done.set()
        return []

    monkeypatch.setattr(retention, 'prune', fake_prune)
    assert retention.schedule_prune(str(cache_root)) is True
    assert started.wait(10)
    assert retention.schedule_prune(str(cache_root)) is False
    assert retention.schedule_prune(str(cache_root)) is False
    release.set()
    assert done.wait(10)
    assert calls == [str(cache_root)] * 2
