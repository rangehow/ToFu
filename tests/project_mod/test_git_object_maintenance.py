"""lib/project_mod/git_object_maintenance.py: when a delivery commit starts a pass."""
from __future__ import annotations

import json
import os
import subprocess
import threading

import pytest

from lib.project_mod import git_object_maintenance as maintenance

pytestmark = pytest.mark.unit
DAY = 86400


def due(counts, record=None, *, loose_limit=6700, pack_limit=50, now=10 * DAY):
    return maintenance.due_reason(counts, record, loose_limit=loose_limit,
                                  pack_limit=pack_limit, now=now)


def test_git_thresholds_decide_and_the_last_pass_residue_is_not_repeated():
    assert due({'count': 6701, 'packs': 1})
    assert due({'count': 10, 'packs': 51})
    assert not due({'count': 6700, 'packs': 50})
    residue = {'finishedAt': 10 * DAY - 3600, 'looseAfter': 60000, 'expireDays': 1}
    assert not due({'count': 60500, 'packs': 1}, residue), 'its expiry has not passed'
    assert due({'count': 66800, 'packs': 1}, residue), 'new loose objects beyond the residue'
    assert due({'count': 60500, 'packs': 1}, residue, now=11 * DAY), 'the residue expired'
    assert not due({'count': 99999, 'packs': 1}, loose_limit=0), 'gc.auto=0 disables loose'


def git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
    return subprocess.run(['git', '-C', str(cwd), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _join_passes():
    for thread in threading.enumerate():
        if thread.name.startswith('tofu-git-maintenance-'):
            thread.join(120)


def test_a_delivery_event_runs_one_due_pass_and_records_it(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'gc.auto', '3')
    for index in range(4):
        (repo / f'f{index}.txt').write_text(f'{index}\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', str(index))
    monkeypatch.setenv('GIT_CONFIG_PARAMETERS', "'gc.auto=0'")
    assert maintenance._git_threshold(repo, 'gc.auto') == 3, 'reads the repository, not the policy'

    assert maintenance.run_when_due(repo)
    _join_passes()

    record = json.loads((repo / '.git' / maintenance.RECORD_NAME).read_text())
    assert record['looseAfter'] == 0 and record['packsAfter'] == 1
    assert git(repo, 'count-objects', '-v').splitlines()[0] == 'count: 0'
    stamp = (repo / '.git' / maintenance.RECORD_NAME).stat().st_mtime_ns

    assert maintenance.run_when_due(repo)
    _join_passes()
    assert (repo / '.git' / maintenance.RECORD_NAME).stat().st_mtime_ns == stamp, 'not due'
