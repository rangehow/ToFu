"""Internal MCP repos are declared once and bundled by every private export path.

The inventory is the overlay launcher manifest
(``bundles/yourprovider/mcp_launchers/yourprovider.json``): each launcher whose sources
name ``vendor/<launcher>`` is a sibling repo that personal and internal exports
vendor, whose copied registration is made portable, and that install.sh warms.
Personal export never runs ``_create_skeleton`` (tar fast path), so both paths
are pinned here; opensource never ships these repos.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_install_sh_reads_the_same_inventory():
    text = (REPO / 'install.sh').read_text(encoding='utf-8')
    reader = re.search(r"mapfile -t _INTERNAL_MCP_REPOS < <\(python - \"\$_INTERNAL_MCP_MANIFEST\" <<'PY'\n(.*?)\nPY\n",
                       text, re.S)
    assert reader, 'install.sh no longer derives its MCP list from the launcher manifest'
    names = subprocess.run([sys.executable, '-', str(MANIFEST)], input=reader.group(1),
                           capture_output=True, text=True, check=True).stdout.split()
    assert tuple(names) == lists._BUNDLED_INTERNAL_MCP_REPOS
    assert not re.search(r'for _\w+ in [^\n]*hope-mcp', text), 'install.sh hard-codes an MCP repo list'
