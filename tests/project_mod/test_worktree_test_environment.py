"""A task worktree's interpreter sees what the project interpreter sees.

Regression (2026-11-15): the project `.venv` was created with
--system-site-packages over a conda base that holds pytest; the bridged task
`.venv` did not inherit the flag, so `make test` in every Worker worktree failed
with "No module named pytest". The `test` extra declared in pyproject.toml is
the one list of what the suite needs, and verification checks it up front.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from lib.project_mod import landing_verification, suite_dependencies
from lib.project_mod.workspace_runtime import SOURCE_PACKAGES_PTH, prepare_workspace_runtime

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX venv layout')]

_SITE_DIRS = 'import site, sys; print("\\n".join(site.getsitepackages()))'


def _venv(path: Path, *flags: str) -> Path:
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', *flags, str(path)],
                   check=True, capture_output=True, timeout=60)
    return path / 'bin/python'


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key not in ('PYTHONPATH', 'VIRTUAL_ENV', 'PYTHONHOME')}


def _sys_path(python: Path) -> list[str]:
    return subprocess.check_output([str(python), '-c', 'import sys; print("\\n".join(sys.path))'],
                                   text=True, env=_clean_env()).splitlines()


def _site_dirs(python: Path) -> list[str]:
    return subprocess.check_output([str(python), '-c', _SITE_DIRS], text=True,
                                   env=_clean_env()).splitlines()


@pytest.mark.parametrize('system_site', [True, False])
def test_task_environment_resolves_every_site_directory_of_its_source(tmp_path, system_site):
    source, task = tmp_path / 'source', tmp_path / 'task'
    source.mkdir()
    task.mkdir()
    source_python = _venv(source / '.venv', *(['--system-site-packages'] if system_site else []))
    assert prepare_workspace_runtime(str(source), str(task)) == ['.venv']
    visible = _sys_path(task / '.venv/bin/python')
    missing = [path for path in _site_dirs(source_python)
               if os.path.isdir(path) and path not in visible]
    assert missing == []
    import sysconfig
    base_packages = sysconfig.get_path('purelib', vars={'base': sys.base_prefix,
                                                        'platbase': sys.base_prefix})
    assert (base_packages in visible) is system_site


def test_an_existing_bridge_is_realigned_with_its_source_flag(tmp_path):
    source, task = tmp_path / 'source', tmp_path / 'task'
    source.mkdir()
    task.mkdir()
    _venv(source / '.venv', '--system-site-packages')
    prepare_workspace_runtime(str(source), str(task))
    config = task / '.venv/pyvenv.cfg'
    config.write_text(config.read_text().replace('include-system-site-packages = true',
                                                  'include-system-site-packages = false'))
    assert prepare_workspace_runtime(str(source), str(task)) == []
    assert 'include-system-site-packages = true' in config.read_text()


def test_an_unbridged_environment_is_left_alone(tmp_path):
    source, task = tmp_path / 'source', tmp_path / 'task'
    source.mkdir()
    task.mkdir()
    _venv(source / '.venv', '--system-site-packages')
    _venv(task / '.venv')
    before = (task / '.venv/pyvenv.cfg').read_text()
    prepare_workspace_runtime(str(source), str(task))
    assert (task / '.venv/pyvenv.cfg').read_text() == before


def test_missing_reports_unsatisfied_test_extra_requirements(tmp_path):
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\nname = "probe"\nversion = "0"\n[project.optional-dependencies]\n'
        'test = ["pytest>=1", "pytest>=999999", "tofu-surely-absent-package>=1", '
        '"pytest>=999999; sys_platform == \'never\'"]\n')
    assert suite_dependencies.missing(sys.executable, tmp_path) == [
        'pytest>=999999', 'tofu-surely-absent-package>=1']


def test_the_repository_declares_the_runner_plugins_its_addopts_load():
    import tomllib
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    declared = {entry.split('>')[0].split('=')[0].split('[')[0].strip().lower()
                for entry in project['project']['optional-dependencies']['test']}
    assert {'pytest', 'pytest-timeout', 'pytest-xdist', 'requests-mock'} <= declared


def test_installs_target_the_source_environment_of_a_bridged_interpreter(tmp_path):
    source, task = tmp_path / 'source', tmp_path / 'task'
    source.mkdir()
    task.mkdir()
    _venv(source / '.venv', '--system-site-packages')
    prepare_workspace_runtime(str(source), str(task))
    assert list((task / '.venv').glob(f'lib/python*/site-packages/{SOURCE_PACKAGES_PTH}'))
    owner = suite_dependencies.owning_interpreter(str(task / '.venv/bin/python3'))
    assert Path(owner) == source / '.venv/bin/python3'
    plain = str(source / '.venv/bin/python3')
    assert suite_dependencies.owning_interpreter(plain) == plain


def test_the_warm_checkout_links_the_current_worktree_dependencies(tmp_path):
    first, second, warm = tmp_path / 'first', tmp_path / 'second', tmp_path / 'warm'
    for checkout in (first, second, warm):
        checkout.mkdir()
    for checkout in (first, second):
        (checkout / 'node_modules').mkdir()
        (checkout / '.venv').mkdir()
    landing_verification._bridge(first, warm)
    landing_verification._bridge(second, warm)
    assert os.readlink(warm / 'node_modules') == str(second / 'node_modules')
    assert os.readlink(warm / '.venv') == str(second / '.venv')
    (tmp_path / 'real').mkdir()
    real = tmp_path / 'real'
    (real / 'node_modules').mkdir()
    landing_verification._bridge(first, real)
    assert not (real / 'node_modules').is_symlink()
