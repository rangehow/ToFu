"""Per-project persistent toolchains reach every command of the project (tofu.toolchains.json)."""
from __future__ import annotations

import json
import os
import stat

import pytest

from lib.project_mod import toolchains
from lib.project_mod.run_command import _get_cmd_env, tool_run_command

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_toolchain_environment(monkeypatch):
    monkeypatch.setenv('PATH', os.defpath)
    for name in ('TOFU_TOOLCHAINS', 'TOFU_TOOLCHAINS_MISSING', 'TEXBIN', 'POPPLER'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def persistent(tmp_path, monkeypatch):
    """pytest's tmp_path lives under /tmp; treat only a sibling root as temporary here."""
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(toolchains, '_temporary_roots', lambda: [str(scratch)])
    store = tmp_path / 'persistent'
    store.mkdir()
    return store


def _program(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho {text}\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _project(tmp_path, toolchain_map):
    project = tmp_path / 'project'
    project.mkdir(exist_ok=True)
    (project / toolchains.REGISTRY_FILENAME).write_text(
        json.dumps({'version': 1, 'toolchains': toolchain_map}))
    return project


def _tex(store, **extra):
    return {'description': 'TeX Live', 'location': str(store / 'texlive'), 'path': ['bin/x86_64-linux'],
            'env': {'TEXBIN': '{location}/bin/x86_64-linux'}, 'provides': ['pdflatex'],
            'install': 'mkdir -p "$TOOLCHAIN_LOCATION/bin/x86_64-linux"', **extra}


def test_command_environment_puts_installed_toolchains_on_path(tmp_path, persistent):
    _program(persistent / 'texlive' / 'bin' / 'x86_64-linux' / 'pdflatex', 'tex')
    _program(persistent / 'poppler' / 'bin' / 'pdffonts', 'poppler')
    project = _project(tmp_path, {
        'texlive': _tex(persistent),
        'poppler': {'location': str(persistent / 'poppler'), 'path_append': ['bin'],
                    'install': 'conda create -y -p "$TOOLCHAIN_LOCATION" poppler'}})
    env = _get_cmd_env(str(project), project_root=str(project))
    path = env['PATH'].split(os.pathsep)
    texbin = str(persistent / 'texlive' / 'bin' / 'x86_64-linux')
    assert texbin in path and path[-1] == str(persistent / 'poppler' / 'bin')
    assert path[0] == texbin
    assert env['TEXBIN'] == texbin
    assert env['TOFU_TOOLCHAINS'] == 'texlive,poppler'
    assert 'TOFU_TOOLCHAINS_MISSING' not in env


def test_run_command_resolves_declared_programs_without_sourcing(tmp_path, persistent):
    _program(persistent / 'texlive' / 'bin' / 'x86_64-linux' / 'pdflatex', 'declared-tex')
    project = _project(tmp_path, {'texlive': _tex(persistent)})
    output = tool_run_command(str(project), 'pdflatex && echo "$TEXBIN"', environment_root=str(project))
    assert 'declared-tex' in output
    assert str(persistent / 'texlive' / 'bin' / 'x86_64-linux') in output
    assert '[exit code: 0]' in output


def test_ordinary_run_command_tool_gives_worker_declared_path_and_env(tmp_path, persistent, monkeypatch):
    """End to end through the model-facing tool entry, from a subdirectory of a Worker checkout.

    Poppler's conda bin also ships openssl/kinit/bzip2; it is appended, so the
    system copies stay first while its own pdffonts still resolves.
    """
    from lib.project_mod.tools import execute_tool
    system = tmp_path / 'system-bin'
    for name in ('openssl', 'kinit', 'bzip2'):
        _program(system / name, f'system-{name}')
        _program(persistent / 'poppler' / 'bin' / name, f'poppler-{name}')
    _program(persistent / 'poppler' / 'bin' / 'pdffonts', 'poppler-pdffonts')
    _program(persistent / 'texlive' / 'bin' / 'x86_64-linux' / 'pdflatex', 'texlive-pdflatex')
    monkeypatch.setenv('PATH', str(system) + os.pathsep + os.environ.get('PATH', os.defpath))
    checkout = _project(tmp_path, {
        'texlive': _tex(persistent),
        'poppler': {'location': str(persistent / 'poppler'), 'path_append': ['bin'],
                    'env': {'POPPLER': '{location}/bin'},
                    'install': 'conda create -y -p "$TOOLCHAIN_LOCATION" poppler'}})
    (checkout / 'paper').mkdir()
    output = execute_tool('run_command', {
        'command': 'cd paper && openssl; kinit; bzip2; pdffonts; pdflatex; '
                   'echo "TEXBIN=$TEXBIN"; echo "POPPLER=$POPPLER"; echo "APPLIED=$TOFU_TOOLCHAINS"'},
        str(checkout))
    lines = output.splitlines()
    for name in ('openssl', 'kinit', 'bzip2'):
        assert f'system-{name}' in lines and f'poppler-{name}' not in lines
    assert 'poppler-pdffonts' in lines and 'texlive-pdflatex' in lines
    assert f'TEXBIN={persistent / "texlive" / "bin" / "x86_64-linux"}' in lines
    assert f'POPPLER={persistent / "poppler" / "bin"}' in lines
    assert 'APPLIED=texlive,poppler' in lines
    assert '[exit code: 0]' in output


def test_worker_checkout_carries_the_declaration(tmp_path, persistent):
    """A Worker's checkout is its own root; the committed file applies there unchanged."""
    _program(persistent / 'texlive' / 'bin' / 'x86_64-linux' / 'pdflatex', 'tex')
    checkout = tmp_path / 'worker-checkout'
    checkout.mkdir()
    (checkout / toolchains.REGISTRY_FILENAME).write_text(json.dumps(
        {'version': 1, 'toolchains': {'texlive': _tex(persistent)}}))
    (checkout / 'paper').mkdir()
    env = _get_cmd_env(str(checkout / 'paper'), project_root=str(checkout))
    assert str(persistent / 'texlive' / 'bin' / 'x86_64-linux') in env['PATH'].split(os.pathsep)


def test_missing_toolchain_is_named_and_hinted_on_command_not_found(tmp_path, persistent):
    project = _project(tmp_path, {'texlive': _tex(persistent)})
    env = _get_cmd_env(str(project), project_root=str(project))
    assert env['TOFU_TOOLCHAINS_MISSING'] == 'texlive'
    assert str(persistent / 'texlive') not in env['PATH']
    output = tool_run_command(str(project), 'definitely-not-a-program-xyz', environment_root=str(project))
    assert '[exit code: 127]' in output
    assert 'toolchain texlive declared in tofu.toolchains.json is not installed' in output
    ok = tool_run_command(str(project), 'echo fine', environment_root=str(project))
    assert 'not installed' not in ok


def test_install_recipe_creates_the_location_then_commands_see_it(tmp_path, persistent):
    project = _project(tmp_path, {'texlive': _tex(persistent)})
    assert toolchains.install(str(project), 'texlive') == 0
    _program(persistent / 'texlive' / 'bin' / 'x86_64-linux' / 'pdflatex', 'tex')
    rows = toolchains.status(str(project))
    assert rows[0]['installed'] and rows[0]['provides']['pdflatex'].endswith('pdflatex')
    env = _get_cmd_env(str(project), project_root=str(project))
    assert env['TOFU_TOOLCHAINS'] == 'texlive'


@pytest.mark.parametrize('location', ['/tmp/repA-tl', '/var/tmp/tl', '/dev/shm/tl'])
def test_temporary_locations_are_refused(tmp_path, location):
    with pytest.raises(toolchains.ToolchainRegistryError, match='temporary root'):
        toolchains.parse_registry({'version': 1, 'toolchains': {
            'tex': {'location': location, 'install': 'true'}}})


@pytest.mark.parametrize('spec, message', [
    ({'location': 'relative/tl', 'install': 'x'}, 'absolute path'),
    ({'location': '/persist/tl'}, 'install must be'),
    ({'location': '/persist/tl', 'install': 'x', 'path': ['../escape']}, 'stay inside'),
    ({'location': '/persist/tl', 'install': 'x', 'env': {'PATH': '/x'}}, 'may not set'),
    ({'location': '/persist/tl', 'install': 'x', 'bins': ['bin']}, 'unknown fields'),
])
def test_malformed_declarations_are_refused_with_the_fix(spec, message):
    with pytest.raises(toolchains.ToolchainRegistryError, match=message):
        toolchains.parse_registry({'version': 1, 'toolchains': {'tex': spec}})


def test_malformed_registry_refuses_the_command_visibly(tmp_path, persistent):
    project = tmp_path / 'project'
    project.mkdir()
    (project / toolchains.REGISTRY_FILENAME).write_text('{"version": 1, "toolchains": ')
    output = tool_run_command(str(project), 'echo never', environment_root=str(project))
    assert 'Error starting command' in output and 'tofu.toolchains.json' in output
    assert 'never' not in output.split('\n', 1)[1]


def test_project_without_registry_is_unchanged(tmp_path):
    env = _get_cmd_env(str(tmp_path), project_root=str(tmp_path))
    assert 'TOFU_TOOLCHAINS' not in env
