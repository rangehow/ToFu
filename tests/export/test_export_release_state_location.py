"""The Open release dest (and its public history cursor) defaults to durable storage.

docs/PUBLICATION_AUDIT.md §R.6: the cursor in ``<dest>/.git`` records where
the next incremental public release continues. Run from a linked worktree on
local ``/tmp``, the old ``ROOT.parent / 'tofu-open'`` default put it beside
the disposable worktree; it now lives under the main checkout's ``data/``.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    is_opensource_build(), reason='The private publication toolchain is not shipped')]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def linked_worktree(tmp_path, monkeypatch):
    from export_pkg import _orchestrate
    main = tmp_path / 'durable' / 'tofu'
    main.mkdir(parents=True)
    git(main, 'init', '-q', '-b', 'main')
    git(main, '-c', 'user.name=x', '-c', 'user.email=x@example.org',
        'commit', '-q', '--allow-empty', '-m', 'root')
    worktree = tmp_path / 'local' / 'lane'
    git(main, 'worktree', 'add', '-q', '--detach', str(worktree))
    monkeypatch.setattr(_orchestrate, 'ROOT', worktree)
    return main.resolve(), worktree


def test_open_dest_is_under_the_main_checkout_data_root_from_a_worktree(linked_worktree):
    from export_pkg._orchestrate import default_export_dest
    main, worktree = linked_worktree
    assert default_export_dest('opensource', None, 'main') == main / 'data' / 'export' / 'tofu-open'
    assert default_export_dest('opensource', 'main', 'main') == main / 'data' / 'export' / 'tofu-open'
    assert (default_export_dest('opensource', 'cli_switch', 'main')
            == main / 'data' / 'export' / 'tofu-open--cli_switch')
    # Internal/personal destinations hold no resumable release state.
    assert default_export_dest('internal', None, 'master') == worktree.parent / 'tofu-yourprovider'
    assert default_export_dest('personal', None, None) == worktree.parent / 'tofu-personal'


def test_cli_without_dest_exports_opensource_to_the_durable_default(linked_worktree, monkeypatch):
    import export
    main, _ = linked_worktree
    seen = {}
    monkeypatch.setattr(export, 'export_project', lambda mode, dest, **kw: seen.update(dest=dest))
    monkeypatch.setattr('sys.argv', ['export.py', '--mode', 'opensource', '--dry-run'])
    export.main()
    assert seen['dest'] == main / 'data' / 'export' / 'tofu-open'


def test_release_dests_never_ship_in_any_mode(tmp_path):
    from export_pkg._pipeline import RELEASE_DEST_PARENT, _build_tar_excludes_for_mode
    assert '--exclude=./' + RELEASE_DEST_PARENT in _build_tar_excludes_for_mode('personal', tmp_path / 'p')[0]
    for mode in ('internal', 'opensource'):
        assert '--exclude=./data' in _build_tar_excludes_for_mode(mode, tmp_path / mode)[0]


def test_dry_run_reports_the_resumable_cursor(tmp_path, capsys):
    from export_pkg._history import _policy_digest, _write_state
    from export_pkg._orchestrate import _report_public_history_cursor
    dest = tmp_path / 'tofu-open'
    (dest / '.git').mkdir(parents=True)
    _report_public_history_cursor(dest)
    assert 'No public history cursor' in capsys.readouterr().out

    _write_state(dest / '.git' / 'tofu-export-history.json',
                 {'schema': 3, 'source_tip': 'a' * 40, 'public_tip': 'b' * 40,
                  'policy': _policy_digest()})
    _report_public_history_cursor(dest)
    out = capsys.readouterr().out
    assert 'a' * 40 in out and 'b' * 40 in out and 'policy matches' in out

    (dest / '.git' / 'tofu-export-history.json').write_text('{"integrity": "forged"}')
    _report_public_history_cursor(dest)
    assert 'Invalid public history cursor integrity' in capsys.readouterr().out
