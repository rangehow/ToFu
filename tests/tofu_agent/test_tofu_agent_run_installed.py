"""Installed-wheel `tofu-agent run`: one real tool round without checkout imports.

A checkout hides wheel-boundary regressions: PYTHONPATH, the editable-install
finder and the working directory all make packages the wheel excludes, such as
`lib.storage`, importable again. This test builds the wheel from the working
tree with an isolated setuptools ``build_base``, extracts it as the only
application path beside this interpreter's installed dependencies, and runs the
wheel's declared ``tofu-agent`` console entry point under ``-S`` from a
directory outside the checkout against a scripted loopback provider (the shared
round in ``tests/support/agent_wheel_run.py``). The build is offline: the
pinned ``build-system.requires`` come from the uv cache, never the network
(landing verification runs without proxy routes).

It also proves that the package manifest change-based selection and the
storage-free probe read (``scripts/agent_wheel_manifest.py``) names exactly the
modules this build ships.
"""
from __future__ import annotations

from importlib.metadata import Distribution
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import zipfile

import pytest

from tests.support.agent_wheel_run import (
    ROOT, assert_tool_round, dependency_paths, manifest_modules, module_name, run_tofu_agent,
    unsatisfied_imports,
)

pytestmark = pytest.mark.api


def _build_wheel(tmp_path: Path) -> Path:
    from uv import find_uv_bin

    # setuptools otherwise writes build/ and *.egg-info into the source tree,
    # and a stale build/lib can leak packages the current exclusions drop.
    isolation = tmp_path / 'setuptools-isolation.cfg'
    egg_base = tmp_path / 'egg-info'
    egg_base.mkdir()
    isolation.write_text(
        f'[build]\nbuild_base = {tmp_path / "build"}\n'
        f'[egg_info]\negg_base = {egg_base}\n', encoding='utf-8')
    dist = tmp_path / 'dist'
    environment = {**os.environ, 'DIST_EXTRA_CONFIG': str(isolation)}
    environment.pop('PYTHONPATH', None)
    try:
        built = subprocess.run(
            [find_uv_bin(), 'build', '--wheel', '--offline', '--python', sys.executable,
             '--out-dir', str(dist), str(ROOT)],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as expired:
        pytest.fail(f'wheel build exceeded {expired.timeout}s: {str(expired.stderr)[-4000:]}')
    if built.returncode and 'was not found in the cache' in built.stderr:
        # Landing verification points UV_CACHE_DIR at an isolated root. The
        # pinned build backend is not a test oracle: a cold cache is an
        # unexercisable precondition, not a wheel defect. When it is warm the
        # build is offline and exercises the installed wheel end to end.
        pytest.skip('uv cache has no pinned build backend for an offline wheel build')
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-4000:]
    (wheel,) = dist.glob('tofu_agent-*.whl')
    return wheel


@pytest.mark.timeout(600)
def test_installed_wheel_run_completes_a_tool_round_without_checkout_imports(tmp_path):
    wheel = _build_wheel(tmp_path)
    installed = tmp_path / 'installed'
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
        built = {module_name(name) for name in archive.namelist()
                 if name.endswith('.py') and '.dist-info/' not in name}
    # The published contract keeps durable storage authorities out of the wheel.
    assert not (installed / 'lib' / 'storage').exists()
    assert built == manifest_modules()
    (distribution,) = (Distribution.at(path) for path in installed.glob('tofu_agent-*.dist-info'))
    (console,) = distribution.entry_points.select(group='console_scripts', name='tofu-agent')
    top_level = distribution.read_text('top_level.txt').split()

    run = run_tofu_agent(tmp_path, application=installed, entry=(console.module, console.attr),
                         top_level=top_level)

    assert_tool_round(run)
    paths = sysconfig.get_paths()
    allowed = [root.resolve() for root in (
        installed, *map(Path, dependency_paths()), Path(paths['stdlib']), Path(paths['platstdlib']))]
    foreign = {name: path for name, path in run.report['modules'].items() if path and not any(
        Path(path).resolve().is_relative_to(root) for root in allowed)}
    assert not foreign
    assert 'tofu_agent.cli' in run.report['modules']
    assert not unsatisfied_imports(run)
