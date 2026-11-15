"""Specifications for the lint gates applied to changed files (scripts/check_static_gates.py)."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'


def _load():
    spec = importlib.util.spec_from_file_location('check_static_gates', SCRIPTS / 'check_static_gates.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gates = _load()


def test_only_gated_rust_package_sources_select_the_rust_gates():
    engine = gates.RUST_GATED_PACKAGES[0]
    assert gates.rust_targets([engine + '/src/wal.rs']) == [engine]
    assert gates.rust_targets([engine + '/Cargo.lock']) == [engine]
    assert gates.rust_targets([engine + '/build.rs']) == [engine]
    assert gates.rust_targets([engine + '/README.md', 'packages/other/src/lib.rs', 'docs/x.rs']) == []


def test_python_gate_covers_existing_python_files_only(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, 'ROOT', tmp_path)
    (tmp_path / 'kept.py').write_text('x = 1\n')
    assert gates.python_targets(['kept.py', 'deleted.py', 'notes.md']) == ['kept.py']


def test_ruff_version_comes_from_the_uv_lock_pin(tmp_path):
    assert gates.ruff_pin(tmp_path) is None
    (tmp_path / 'uv.lock').write_text('[[package]]\nname = "ruffle"\nversion = "1.0"\n')
    assert gates.ruff_pin(tmp_path) is None
    (tmp_path / 'uv.lock').write_text('[[package]]\nname = "ruff"\nversion = "0.16.4"\n')
    assert gates.ruff_pin(tmp_path) == '0.16.4'


def _checkout(tmp_path, *, pin='9.9.9'):
    """A checkout holding the gate script, an optional ruff pin and one Python file."""
    root = tmp_path / 'checkout'
    (root / 'scripts').mkdir(parents=True)
    shutil.copyfile(SCRIPTS / 'check_static_gates.py', root / 'scripts' / 'check_static_gates.py')
    if pin:
        (root / 'uv.lock').write_text(f'[[package]]\nname = "ruff"\nversion = "{pin}"\n')
    (root / 'module.py').write_text('x = 1\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    return root


def _path_with_ruff(tmp_path, *, version='9.9.9', check_exit=0):
    """PATH holding git and a ruff stub that reports VERSION and records its check argv."""
    directory = tmp_path / 'bin'
    directory.mkdir()
    (directory / 'git').symlink_to(shutil.which('git'))
    if version:
        stub = directory / 'ruff'
        stub.write_text('#!/bin/sh\n'
                        f'if [ "$1" = "--version" ]; then echo "ruff {version}"; exit 0; fi\n'
                        f'printf "%s\\n" "$@" > "$RUFF_ARGS"\nexit {check_exit}\n')
        stub.chmod(0o755)
    return str(directory)


def _gate(root, env, *paths):
    return subprocess.run([sys.executable, str(root / 'scripts' / 'check_static_gates.py'), '--', *paths],
                          cwd=root, env=env, text=True, capture_output=True, timeout=60)


@pytest.mark.parametrize('check_exit', [0, 1])
def test_changed_python_files_pass_or_fail_with_the_pinned_ruff(tmp_path, check_exit):
    root = _checkout(tmp_path)
    record = tmp_path / 'ruff-args'
    env = dict(os.environ, PATH=_path_with_ruff(tmp_path, check_exit=check_exit), RUFF_ARGS=str(record))
    result = _gate(root, env, 'module.py', 'removed.py')
    assert record.read_text().split('\n') == ['check', '--force-exclude', '--', 'module.py', '']
    assert (result.returncode == 0) is (check_exit == 0), result.stdout + result.stderr


def test_an_unavailable_pinned_ruff_fails_closed(tmp_path):
    root = _checkout(tmp_path)
    env = dict(os.environ, PATH=_path_with_ruff(tmp_path, version='0.0.1'), RUFF_ARGS=str(tmp_path / 'unused'))
    result = _gate(root, env, 'module.py')
    assert result.returncode == 2
    assert 'ruff 9.9.9 is unavailable' in result.stdout
    assert not (tmp_path / 'unused').exists()


def test_a_checkout_without_a_ruff_pin_has_no_python_lint(tmp_path):
    root = _checkout(tmp_path, pin=None)
    env = dict(os.environ, PATH=_path_with_ruff(tmp_path, version=None))
    result = _gate(root, env, 'module.py')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'declares no ruff' in result.stdout


def test_selector_run_fails_when_a_lint_gate_fails(tmp_path):
    root = _checkout(tmp_path)
    for name in ('test_select.py', 'test_execution.py', 'agent_wheel_manifest.py'):
        shutil.copyfile(SCRIPTS / name, root / 'scripts' / name)
    (root / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytest_timeout"\n'
        'markers = ["unit", "api", "visual", "slow", "live_llm"]\n')
    selector = importlib.util.spec_from_file_location('gate_selector', SCRIPTS / 'test_select.py')
    selector_module = importlib.util.module_from_spec(selector)
    selector.loader.exec_module(selector_module)
    for name in selector_module.GUARD_CORE_BASENAMES:
        guard = root / 'tests' / 'guards' / name
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text('import pytest\npytestmark = pytest.mark.unit\ndef test_guard():\n    assert True\n')
    for support in ('tests/__init__.py', 'tests/support/__init__.py',
                    'tests/support/process_priority.py', 'tests/support/lower_priority_plugin.py'):
        (root / support).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / support, root / support)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'initial'], cwd=root, check=True)
    (root / 'module.py').write_text('x = 2\n')
    env = dict(os.environ, PATH=_path_with_ruff(tmp_path, check_exit=1), RUFF_ARGS=str(tmp_path / 'args'))
    result = subprocess.run([sys.executable, str(root / 'scripts' / 'test_select.py'), '--run', '--jobs', '0'],
                            cwd=root, env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode != 0
    assert 'static gates exit=1' in result.stdout.splitlines()[-1], result.stdout + result.stderr
