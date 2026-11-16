"""Execution preparation and launch use one admitted interpreter generation."""
import json
from pathlib import Path
import site
import sys

import pytest

from serverctl_pkg.execution_service import ExecutionService

pytestmark = pytest.mark.unit


def test_preparation_uses_installed_environment_before_computing_identity(tmp_path, monkeypatch):
    from serverctl_pkg import native_engine
    target = tmp_path / 'environment/bin/python'
    target.parent.mkdir(parents=True)
    target.symlink_to(sys.executable)
    (tmp_path / '.tofu_env.json').write_text(json.dumps({'python': str(target)}))
    calls = []

    def prepare(command, **options):
        calls.append((command, options))
        Path(command[-1]).write_text(json.dumps({'python': command[0]}))

    monkeypatch.setattr(native_engine, '_run', prepare)
    service = ExecutionService(str(tmp_path), '/old-manager/python')
    plan = service.prepare({'TOFU_PROJECT_PATH': str(tmp_path)})
    assert plan['python'] == str(target)
    assert calls[0][0][:3] == [str(target), '-m', 'serverctl_pkg.execution_host']
    assert calls[0][1]['environment']['TOFU_PROCESS_ROLE'] == 'execution'
    # A marker edited after preparation cannot change an already admitted launch.
    (tmp_path / '.tofu_env.json').write_text(json.dumps({'python': sys.executable}))
    assert service.launch_python(plan) == str(target)


@pytest.mark.parametrize('marker', [None, '{broken', '[]', '{"python": false}',
                                    '{"python": "/missing/python"}'])
def test_unusable_optional_marker_preserves_supplied_interpreter(tmp_path, monkeypatch, marker):
    from serverctl_pkg import native_engine
    if marker is not None:
        (tmp_path / '.tofu_env.json').write_text(marker)
    calls = []

    def prepare(command, **options):
        calls.append(command)
        Path(command[-1]).write_text('{}')

    monkeypatch.setattr(native_engine, '_run', prepare)
    ExecutionService(str(tmp_path), sys.executable).prepare({})
    assert calls[0][0] == sys.executable


def test_real_preparation_records_the_installed_interpreter_not_the_manager(tmp_path):
    import os
    import subprocess
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages',
                    str(tmp_path / 'environment')], check=True, capture_output=True, timeout=120)
    target = str(tmp_path / 'environment/bin/python')
    # An installed environment carries the application's dependencies. A venv nested
    # in a venv sees only the base interpreter's packages (--system-site-packages),
    # so expose this interpreter's site-packages too.
    purelib = subprocess.run(
        [target, '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    Path(purelib, 'tofu_test_dependencies.pth').write_text(
        '\n'.join(site.getsitepackages()) + '\n')
    (tmp_path / '.tofu_env.json').write_text(json.dumps({'python': target}))
    # The disposable project owns source identity; import the preparation implementation
    # from this lane, independently of the ambient shared-checkout PYTHONPATH.
    environment = {**os.environ, 'TOFU_PROJECT_PATH': str(tmp_path),
                   'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
    service = ExecutionService(str(tmp_path), '/old-manager/python')
    plan = service.prepare(environment)
    assert plan['python'] == target
    assert plan['environmentFingerprint']
    assert service.launch_python(plan) == target
