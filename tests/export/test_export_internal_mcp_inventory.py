"""Internal MCP repos are declared once and bundled by every private export path.

The inventory is the overlay launcher manifest
(``bundles/yourprovider/mcp_launchers/yourprovider.json``): each launcher whose sources
name ``vendor/<launcher>`` is a sibling repo that personal and internal exports
vendor, whose copied registration is made portable, and that install.sh warms
on both install backends. Personal export never runs ``_create_skeleton`` (tar
fast path), so both paths are pinned here; opensource never ships these repos.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.installer.test_install_unattended import _executable, _run

export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

import export_pkg._export_core as export_core  # noqa: E402
import export_pkg._internal as internal  # noqa: E402
import export_pkg._lists as lists  # noqa: E402
import export_pkg._orchestrate as orchestrate  # noqa: E402

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / 'bundles' / 'yourprovider' / 'mcp_launchers' / 'yourprovider.json'


def _declared() -> tuple[str, ...]:
    launchers = json.loads(MANIFEST.read_text(encoding='utf-8'))['launchers']
    return tuple(name for name, row in launchers.items() if f'vendor/{name}' in row['sources'])


def test_bundled_repos_are_the_declared_launcher_inventory():
    assert lists._BUNDLED_INTERNAL_MCP_REPOS == _declared()
    assert {'hope-mcp', 'llm-mcp', 'xuecheng-mcp'} <= set(lists._BUNDLED_INTERNAL_MCP_REPOS)


@pytest.fixture()
def siblings(tmp_path, monkeypatch):
    """A source checkout whose ``../<name>`` holds every declared repo plus a stray one."""
    root = tmp_path / 'tofu'
    root.mkdir()
    for name in (*lists._BUNDLED_INTERNAL_MCP_REPOS, 'unrelated-mcp'):
        repo = tmp_path / name
        (repo / '.git').mkdir(parents=True)
        (repo / 'pyproject.toml').write_text(f'[project]\nname="{name}"\n', encoding='utf-8')
    monkeypatch.setattr(internal, 'ROOT', root)
    return tmp_path


def _write_pinned_config(dest: Path, siblings_dir: Path) -> None:
    config = {name.removesuffix('-mcp'): {
        'command': 'uvx', 'args': ['--from', str(siblings_dir / name), name],
        'enabled': True, 'env': {'KEEP': 'value'}} for name in lists._BUNDLED_INTERNAL_MCP_REPOS}
    path = dest / 'data' / 'config' / 'mcp_servers.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding='utf-8')


def _assert_bundled_and_portable(dest: Path) -> None:
    vendored = {p.name for p in (dest / 'vendor').iterdir() if p.is_dir()}
    assert vendored == set(lists._BUNDLED_INTERNAL_MCP_REPOS)
    for name in vendored:
        assert (dest / 'vendor' / name / 'pyproject.toml').is_file()
        assert not (dest / 'vendor' / name / '.git').exists()
    config = json.loads((dest / 'data' / 'config' / 'mcp_servers.json').read_text(encoding='utf-8'))
    for name in lists._BUNDLED_INTERNAL_MCP_REPOS:
        assert config[name.removesuffix('-mcp')] == {
            'command': name, 'args': [], 'enabled': True, 'env': {'KEEP': 'value'}}


def _stub_unrelated_skeleton_steps(monkeypatch, dest_config_writer):
    for helper in ('_seed_internal_config', '_stage_carried_model_routing', '_copy_carried_config',
                   '_package_builtin_robot', '_copy_carried_memories', '_bundle_github_batch_mcp',
                   '_copy_installed_skills', '_bundle_tofu_search_wheel', '_patch_install_sh_proxy'):
        monkeypatch.setattr(export_core, helper, lambda *args, **kwargs: None)
    monkeypatch.setattr(export_core, '_stage_portable_mcp_config', dest_config_writer)


def test_internal_skeleton_bundles_every_declared_repo(siblings, tmp_path, monkeypatch):
    dest = tmp_path / 'dest'
    _stub_unrelated_skeleton_steps(monkeypatch, lambda target: _write_pinned_config(target, siblings))
    export_core._create_skeleton(dest, 'internal')
    _assert_bundled_and_portable(dest)


def test_personal_tar_path_bundles_every_declared_repo(siblings, tmp_path, monkeypatch):
    dest = tmp_path / 'dest'

    def tar_copy(target, progress=False):
        _write_pinned_config(target, siblings)
        return {'copied': 1, 'sanitized': 0, 'excluded': 0, 'excluded_reasons': {}}

    monkeypatch.setattr(orchestrate, '_export_personal_via_tar', tar_copy)
    monkeypatch.setattr(orchestrate, '_bundle_tofu_search_wheel', lambda *args: None)
    monkeypatch.setattr(orchestrate, '_print_export_summary', lambda *args: None)
    orchestrate.export_project('personal', dest)
    _assert_bundled_and_portable(dest)


def test_opensource_skeleton_ships_no_internal_repo(siblings, tmp_path, monkeypatch):
    dest = tmp_path / 'dest'
    _stub_unrelated_skeleton_steps(monkeypatch, lambda target: None)
    _write_pinned_config(dest, siblings)
    before = (dest / 'data' / 'config' / 'mcp_servers.json').read_text(encoding='utf-8')
    export_core._create_skeleton(dest, 'opensource')
    assert not (dest / 'vendor').exists()
    assert (dest / 'data' / 'config' / 'mcp_servers.json').read_text(encoding='utf-8') == before


def _bundle_internal_mcps(project: Path) -> None:
    """Lay out what an internal export ships: the launcher manifest plus vendor/<repo>."""
    manifest = project / 'bundles' / 'yourprovider' / 'mcp_launchers' / 'yourprovider.json'
    manifest.parent.mkdir(parents=True)
    shutil.copy(MANIFEST, manifest)
    for name in lists._BUNDLED_INTERNAL_MCP_REPOS:
        (project / 'vendor' / name).mkdir(parents=True)
        (project / 'vendor' / name / 'pyproject.toml').write_text(f'[project]\nname="{name}"\n')


def _select_conda_backend(tmp_path: Path, project: Path, env: dict) -> None:
    """Replace the fixture's network-refusing conda with a hermetic one.

    ``create`` yields an env whose python records and accepts every pip call;
    all other conda commands succeed without doing anything.
    """
    fake_bin = Path(env['PATH'].split(os.pathsep)[0])
    base = tmp_path / 'conda-base'
    (base / 'etc' / 'profile.d').mkdir(parents=True)
    (base / 'etc' / 'profile.d' / 'conda.sh').write_text(
        'conda() { if [[ "$1" == activate ]]; then export CONDA_PREFIX="$2" PATH="$2/bin:$PATH"; '
        'else "$FAKE_CONDA" "$@"; fi; }\n')
    (base / 'bin').mkdir()
    (base / 'bin' / 'python').symlink_to(env['FIXTURE_PYTHON'])
    env_python = tmp_path / 'conda-env-python'
    _executable(env_python,
        'if [[ "${1:-}" == -m && "${2:-}" == pip ]]; then printf "pip:%s\\n" "$*" >> "$CALLS_FILE"; exit 0; fi\n'
        'if [[ "${1:-}" == -c && "${2:-}" == *tofu_search* ]]; then exit 0; fi\n'
        'exec "$REAL_PYTHON" "$@"\n')
    _executable(fake_bin / 'conda',
        'printf "conda:%s\\n" "$*" >> "$CALLS_FILE"\n'
        'case "$1" in\n'
        '  --version) echo "conda 24.11.0";;\n'
        '  info) echo "$FAKE_CONDA_BASE";;\n'
        '  create) mkdir -p "$3/bin"; cp "$FAKE_CONDA_ENV_PYTHON" "$3/bin/python";;\n'
        'esac\n')
    (project / 'bootstrap_pkg' / 'runtime_checks.py').write_text('')
    # The conda path checks its package lists cover every declared requirement.
    (project / 'requirements.txt').write_text('quart\ntofu-search>=0.1\n')
    env.update(FAKE_CONDA=str(fake_bin / 'conda'), FAKE_CONDA_BASE=str(base),
               FAKE_CONDA_ENV_PYTHON=str(env_python))


def _install(installer, tmp_path, backend: str, *, bundled: bool) -> tuple[Path, list[str]]:
    project, env = installer
    fake_bin = Path(env['PATH'].split(os.pathsep)[0])
    # A Node >= 22 on PATH keeps the YourProvider runtime step off every package manager.
    _executable(fake_bin / 'node', 'echo 22\n')
    env.pop('TOFU_NODE', None)
    args = ['--non-interactive', '--no-update', '--no-launch', '--skip-playwright']
    if backend == 'conda':
        _select_conda_backend(tmp_path, project, env)
        args.append('--use-conda')
    if bundled:
        _bundle_internal_mcps(project)
    result = _run(project, env, *args)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert json.loads((project / '.tofu_env.json').read_text())['backend'] == backend
    calls = Path(env['CALLS_FILE']).read_text().splitlines()
    assert not [line for line in calls if 'UNEXPECTED' in line]
    return project, calls


def _warm_calls(calls: list[str]) -> list[str]:
    return [line for line in calls if line.startswith('uv:run --no-project --with-editable ')]


@pytest.mark.parametrize('backend', ['uv', 'conda'])
def test_install_warms_every_declared_repo_once_on_both_backends(installer, tmp_path, backend):
    project, calls = _install(installer, tmp_path, backend, bundled=True)
    assert _warm_calls(calls) == [
        f'uv:run --no-project --with-editable {project}/vendor/{name} python -c import {name.replace("-", "_")}'
        for name in lists._BUNDLED_INTERNAL_MCP_REPOS]
    sandbox_sdk = [line for line in calls if 'mt-paas-sandbox-python-sdk==1.1.4' in line]
    installer_prefix = 'uv:pip install --python ' if backend == 'uv' else 'pip:-m pip install '
    assert len(sandbox_sdk) == 1 and sandbox_sdk[0].startswith(installer_prefix), calls


@pytest.mark.parametrize('backend', ['uv', 'conda'])
def test_install_without_internal_bundle_warms_nothing(installer, tmp_path, backend):
    _, calls = _install(installer, tmp_path, backend, bundled=False)
    assert _warm_calls(calls) == []
    assert not [line for line in calls if 'mt-paas-sandbox-python-sdk' in line]
