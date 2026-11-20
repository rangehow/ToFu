"""Executable specification of scripts/verify_checkout_view.py (stale DolphinFS view check)."""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location('verify_checkout_view',
                                               ROOT / 'scripts' / 'verify_checkout_view.py')
view = importlib.util.module_from_spec(_SPEC)
sys.modules['verify_checkout_view'] = view
_SPEC.loader.exec_module(view)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                                          'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    (root / 'src').mkdir(parents=True)
    _git(tmp_path, 'init', '-q', str(root))
    (root / '.gitattributes').write_text('*.py text eol=lf\n')
    (root / 'src' / 'engine.rs').write_text('fn main() {}\n')
    (root / 'tool.py').write_text('print(1)\n')
    (root / 'run.sh').write_text('#!/bin/sh\n')
    (root / 'run.sh').chmod(0o755)
    (root / 'link').symlink_to('tool.py')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')
    return root


def _run(repo: Path, *args: str) -> int:
    return view.main(['--project', str(repo), *args])


def test_a_current_view_passes(repo, capsys):
    assert _run(repo) == 0
    assert 'View is current: 5 files match the index' in capsys.readouterr().out


def test_bytes_equal_after_the_paths_eol_filter_are_current(repo):
    (repo / 'tool.py').write_bytes(b'print(1)\r\n')
    index_blob = _git(repo, 'ls-files', '-s', 'tool.py').split()[1]
    assert _git(repo, 'hash-object', '--path=tool.py', 'tool.py').strip() == index_blob
    assert _run(repo) == 0


def test_every_mismatched_and_unreadable_file_is_listed_and_fails(repo, capsys, monkeypatch):
    (repo / 'src' / 'engine.rs').write_text('fn main() { old() }\n')
    real_open = builtins.open

    def stale_open(file, *args, **kwargs):
        if str(file).endswith('run.sh'):
            raise FileNotFoundError(2, 'No such file or directory')
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', stale_open)
    assert _run(repo) == 1
    out = capsys.readouterr().out
    assert '[FAIL] src/engine.rs: content' in out
    assert '[FAIL] run.sh: unreadable (ENOENT' in out
    assert '2 files mismatched or unreadable' in out


def test_a_size_that_disagrees_with_the_bytes_read_is_a_stale_view(repo, capsys, monkeypatch):
    real_lstat = os.lstat

    def stale_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if str(path).endswith('engine.rs'):
            return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
                                   result.st_uid, result.st_gid, 51221, 0, 0, 0))
        return result

    monkeypatch.setattr(view.os, 'lstat', stale_lstat)
    assert _run(repo) == 1
    assert 'src/engine.rs: unreadable (ESTALE: stat reports 51221 bytes' in capsys.readouterr().out


def test_recorded_uncommitted_work_is_accepted_only_at_the_recorded_bytes(repo, capsys):
    (repo / 'tool.py').write_text('print("wip")\n')
    assert _run(repo) == 1
    assert _run(repo, '--record') == 0
    record = json.loads((repo / view.RECORD_LEAF).read_text())
    assert list(record['uncommitted']) == ['tool.py']
    capsys.readouterr()
    assert _run(repo) == 0
    assert '[WORK] tool.py' in capsys.readouterr().out

    (repo / 'tool.py').write_text('print(1)\n')  # a host still seeing the index bytes
    assert _run(repo) == 1
    assert 'recorded uncommitted work not seen here' in capsys.readouterr().out


def test_a_record_of_another_head_fails(repo, capsys):
    assert _run(repo, '--record') == 0
    (repo / 'new.txt').write_text('x\n')
    _git(repo, 'add', 'new.txt')
    _git(repo, 'commit', '-qm', 'next')
    assert _run(repo) == 1
    assert 'record HEAD' in capsys.readouterr().out


def test_rematerialize_keeps_bytes_mode_and_mtime_under_new_inodes(repo):
    (repo / 'tool.py').write_text('print("wip")\n')
    work_inode = os.lstat(repo / 'tool.py').st_ino
    status = _git(repo, 'status', '--porcelain')
    before = {name: os.lstat(repo / name) for name in ('src/engine.rs', 'run.sh', 'link')}

    assert _run(repo, '--rematerialize', '--first', 'src/') == 0

    for name, old in before.items():
        new = os.lstat(repo / name)
        assert new.st_ino != old.st_ino
        assert (new.st_mode, new.st_size) == (old.st_mode, old.st_size)
        if name != 'link':
            assert new.st_mtime_ns == old.st_mtime_ns
    assert os.readlink(repo / 'link') == 'tool.py'
    assert os.lstat(repo / 'tool.py').st_ino == work_inode
    assert (repo / 'tool.py').read_text() == 'print("wip")\n'
    assert _git(repo, 'status', '--porcelain') == status
    assert not [path for path in repo.rglob('.*view-refresh*')]


def test_rematerialize_since_touches_only_files_changed_after_it(repo):
    base = _git(repo, 'rev-parse', 'HEAD').strip()
    (repo / 'src' / 'engine.rs').write_text('fn main() { new() }\n')
    _git(repo, 'commit', '-qam', 'rewrite')
    untouched = os.lstat(repo / 'run.sh').st_ino
    changed = os.lstat(repo / 'src' / 'engine.rs').st_ino
    assert _run(repo, '--rematerialize', '--since', base) == 0
    assert os.lstat(repo / 'run.sh').st_ino == untouched
    assert os.lstat(repo / 'src' / 'engine.rs').st_ino != changed


def test_repair_rewrites_stale_files_from_the_index_and_never_recorded_work(repo, capsys):
    (repo / 'tool.py').write_text('print("wip")\n')
    assert _run(repo, '--record') == 0
    (repo / 'src' / 'engine.rs').write_text('fn main() { old() }\n')  # the stale view
    assert _run(repo) == 1
    capsys.readouterr()

    assert _run(repo, '--repair') == 0
    out = capsys.readouterr().out
    assert '[REWRITE] src/engine.rs' in out
    assert (repo / 'src' / 'engine.rs').read_text() == 'fn main() {}\n'
    assert (repo / 'tool.py').read_text() == 'print("wip")\n'


def test_repair_refuses_without_a_matching_record(repo, capsys):
    (repo / 'src' / 'engine.rs').write_text('changed\n')
    assert _run(repo, '--repair') == 1
    assert 'Refused' in capsys.readouterr().out
    assert (repo / 'src' / 'engine.rs').read_text() == 'changed\n'
