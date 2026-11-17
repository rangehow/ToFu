#!/usr/bin/env python3
"""The opensource post-copy sanitize never rewrites the destination's preserved state.

An opensource destination keeps its own ``.git/`` and runtime ``data/`` across
re-exports. ``_rg_excluded_globs`` keeps the candidate scan out of them, and the
scan must honour those exclusions on BOTH paths: ripgrep, and the Python walk
used when ripgrep is absent. The walk once ignored them, so a release run
without ``rg`` on PATH rewrote a worktree pointer under ``.git/worktrees/`` and
cached virtual-environment files under ``data/`` in the canonical Open record.

Internal markers are assembled from fragments (never contiguous literals)
because this guard file is itself shipped in the exported tree.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit

# Files the scan-scope fixtures create under tmp_path, not repository files.
_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/model_info/data/table.json'}

_MNT = '/mnt/' + 'dolphin' + 'fs'
_PRESERVED = (
    '.git/worktrees/head/gitdir',
    'data/mcp-cache/venv/pyvenv.cfg',
    'uploads/notes.txt',
)


def _make_dest(root: Path) -> dict[str, bytes]:
    for rel in _PRESERVED:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{_MNT}/state/{rel}\n', encoding='utf-8')
    (root / 'src').mkdir()
    (root / 'src' / 'module.py').write_text(f'STATE = "{_MNT}/state"\n', encoding='utf-8')
    (root / 'build.log').write_text(f'{_MNT}/state\n', encoding='utf-8')
    return {rel: (root / rel).read_bytes() for rel in (*_PRESERVED, 'build.log')}


@pytest.mark.parametrize('ripgrep', [True, False], ids=['ripgrep', 'python-walk'])
def test_scan_honours_the_excluded_globs(tmp_path, monkeypatch, ripgrep):
    from export_pkg import _sanitize
    if ripgrep and not _sanitize._have_tool('rg'):
        pytest.skip('ripgrep is not installed')
    monkeypatch.setattr(_sanitize, '_have_tool', lambda tool: ripgrep and tool == 'rg')
    _make_dest(tmp_path)
    hits = _sanitize._rg_files_with_matches(
        tmp_path, ['dolphin' + 'fs'], extra_globs=_sanitize._rg_excluded_globs())
    assert hits == {'src/module.py'}


@pytest.mark.parametrize('ripgrep', [True, False], ids=['ripgrep', 'python-walk'])
def test_post_copy_sanitize_leaves_preserved_state_untouched(tmp_path, monkeypatch, ripgrep):
    from export_pkg import _sanitize
    if ripgrep and not _sanitize._have_tool('rg'):
        pytest.skip('ripgrep is not installed')
    monkeypatch.setattr(_sanitize, '_have_tool', lambda tool: ripgrep and tool == 'rg')
    before = _make_dest(tmp_path)
    _sanitize._post_copy_sanitize(tmp_path, 'opensource')
    assert {rel: (tmp_path / rel).read_bytes() for rel in before} == before
    assert _MNT not in (tmp_path / 'src' / 'module.py').read_text(encoding='utf-8')


def test_python_walk_searches_exactly_what_ripgrep_searches(tmp_path, monkeypatch):
    """Nested names, a ``./`` glob and extension globs select the same files on both paths."""
    from export_pkg import _sanitize
    if not _sanitize._have_tool('rg'):
        pytest.skip('ripgrep is not installed')
    _make_dest(tmp_path)
    for rel in ('lib/model_info/data/table.json', 'a/node_modules/pkg.js', 'paper/a.md',
                'x/paper/b.md', 'x/run.log', 'x/.ruff_cache/c.txt'):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{_MNT}/state\n', encoding='utf-8')
    globs = _sanitize._rg_excluded_globs()
    ripgrep = _sanitize._rg_files_with_matches(tmp_path, ['dolphin' + 'fs'], extra_globs=globs)
    monkeypatch.setattr(_sanitize, '_have_tool', lambda tool: False)
    walk = _sanitize._rg_files_with_matches(tmp_path, ['dolphin' + 'fs'], extra_globs=globs)
    assert walk == ripgrep
    assert 'src/module.py' in walk and not walk & set(_PRESERVED)


def test_python_walk_refuses_a_glob_it_cannot_honour(tmp_path, monkeypatch):
    from export_pkg import _sanitize
    monkeypatch.setattr(_sanitize, '_have_tool', lambda tool: False)
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    with pytest.raises(ValueError):
        _sanitize._rg_files_with_matches(tmp_path, ['x'], extra_globs=['!src/*/cache'])
