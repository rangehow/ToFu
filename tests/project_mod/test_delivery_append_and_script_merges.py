"""Append-only files and non-overlapping shared-script edits land without a repair Worker.

The publisher's conflict resolver (``lib.project_mod.insertion_conflicts``,
shared by reviewed deliveries and ``scripts/git_land.py``) declares an append
strategy for append-only files (the project ``JOURNAL.md``, or any path with the
``tofu-append-only`` Git attribute): two landings that only appended bytes land
as the earlier landing's file followed by the later landing's appended bytes.
Shared scripts merge three-way: separated edits land (Git), and so do edits
the branch already made identically or with more. Edits that change the same
or adjacent base lines differently stay held, and the hold names the file and
the lines.
"""
import subprocess

import pytest

from lib.project_mod.insertion_conflicts import resolve_insertion_conflicts
from tests.project_mod.test_delivery_landing_queue import (
    commit, reviewed_workspace, settle, shared as shared)
from tests.storage.test_integration_delivery import git, repo as repo

pytestmark = pytest.mark.unit

JOURNAL = b'# Journal\n\n## 2026-11-17\nolder entry'  # Written without a final newline.


def conflicted(tmp_path, name, base, ours, theirs, attributes=b''):
    """A repository mid-merge with NAME unmerged; returns (root, run port)."""
    root = tmp_path / 'merge'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Merge test')
    git(root, 'config', 'user.email', 'merge@example.invalid')
    if attributes:
        (root / '.gitattributes').write_bytes(attributes)
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(base)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'base')
    git(root, 'checkout', '-b', 'theirs')
    (root / name).write_bytes(theirs)
    git(root, 'commit', '-am', 'theirs')
    git(root, 'checkout', 'main')
    (root / name).write_bytes(ours)
    git(root, 'commit', '-am', 'ours')
    merged = subprocess.run(['git', '-C', str(root), 'merge', 'theirs'], capture_output=True)
    assert merged.returncode == 1, merged

    def run(args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    return root, run


def staged(root, name):
    return subprocess.run(['git', '-C', str(root), 'show', f':{name}'],
                          capture_output=True, check=True).stdout


def test_two_appends_to_the_journal_land_the_earlier_first(tmp_path):
    root, run = conflicted(tmp_path, 'JOURNAL.md', JOURNAL,
                           JOURNAL + b'\n\n## A\nalpha\n', JOURNAL + b'\n\n## B\nbeta\n')

    assert resolve_insertion_conflicts(['JOURNAL.md'], run) == []

    assert git(root, 'ls-files', '-u') == ''
    assert staged(root, 'JOURNAL.md') == JOURNAL + b'\n\n## A\nalpha\n\n## B\nbeta\n'


def test_an_appended_entry_the_branch_already_holds_is_not_appended_twice(tmp_path):
    entry = b'\n## A\nalpha\nVerified: tests pass\n'
    base = JOURNAL + b'\n'
    root, run = conflicted(tmp_path, 'JOURNAL.md', base,
                           base + entry + b'\n## C\ngamma\n', base + entry)

    assert resolve_insertion_conflicts(['JOURNAL.md'], run) == []

    assert staged(root, 'JOURNAL.md') == base + entry + b'\n## C\ngamma\n'


def test_a_repository_declares_more_append_only_files_by_attribute(tmp_path):
    base = b'run 1: ok'
    run_2 = b'\nrun 2: ok\n'
    root, run = conflicted(tmp_path, 'logs/runs.log', base, base + run_2 + b'run 3: failed\n',
                           base + run_2, attributes=b'logs/*.log tofu-append-only\n')

    assert resolve_insertion_conflicts(['logs/runs.log'], run) == []

    assert staged(root, 'logs/runs.log') == b'run 1: ok\nrun 2: ok\nrun 3: failed\n'


def test_an_edit_to_the_journal_s_history_falls_back_to_the_line_rules(tmp_path):
    base = b'# Journal\n\nentry one\nentry two\n'
    root, run = conflicted(tmp_path, 'JOURNAL.md', base,
                           base.replace(b'entry one', b'entry one (fixed)'),
                           base.replace(b'entry one', b'entry one (amended)'))
    causes = {}

    assert resolve_insertion_conflicts(['JOURNAL.md'], run, causes) == ['JOURNAL.md']

    assert causes == {'JOURNAL.md': 'both sides changed line 3'}


SCRIPT = b''.join(b'step_%d --flag\n' % number for number in range(1, 9))


def test_adjacent_edits_to_a_shared_script_stay_held_as_git_holds_them(tmp_path):
    """An edit beside lines the other side changed may depend on them."""
    root, run = conflicted(tmp_path, 'scripts/run.sh', SCRIPT,
                           SCRIPT.replace(b'step_4 --flag', b'step_4 --flag --ours'),
                           SCRIPT.replace(b'step_5 --flag', b'step_5 --flag --theirs'))
    causes = {}

    assert resolve_insertion_conflicts(['scripts/run.sh'], run, causes) == ['scripts/run.sh']

    assert causes == {'scripts/run.sh': 'both sides changed lines 4-5'}


def test_a_change_the_branch_already_made_with_more_lands_as_the_branch_has_it(tmp_path):
    ours = SCRIPT.replace(b'step_4 --flag\n', b'# step 4 runs from cmd.sh\nstep_4 --entry\n')
    root, run = conflicted(tmp_path, 'scripts/run.sh', SCRIPT, ours,
                           SCRIPT.replace(b'step_4 --flag\n', b'step_4 --entry\n'))

    assert resolve_insertion_conflicts(['scripts/run.sh'], run) == []

    assert staged(root, 'scripts/run.sh') == ours


def test_a_script_change_the_branch_already_made_inside_a_larger_one_lands_once(tmp_path):
    """2026-11-17 parallel c3289f41, infra/npu/submit_gate_910cexp.sh: both hunks shapes."""
    ours = (SCRIPT.replace(b'step_2 --flag\n', b'step_2 --flag\nexport A=1\n${ENV_LINES:-}\nexport B=2\n')
            .replace(b'step_6 --flag\n', b'# step 6 runs from cmd.sh\nstep_6 --entry\n'))
    theirs = (SCRIPT.replace(b'step_2 --flag\n', b'step_2 --flag\n${ENV_LINES:-}\n')
              .replace(b'step_6 --flag\n', b'step_6 --entry\n'))
    root, run = conflicted(tmp_path, 'scripts/run.sh', SCRIPT, ours, theirs)

    assert resolve_insertion_conflicts(['scripts/run.sh'], run) == []

    assert staged(root, 'scripts/run.sh') == ours


def test_a_genuine_overlap_in_a_shared_script_stays_unmerged_and_named(tmp_path):
    root, run = conflicted(tmp_path, 'scripts/run.sh', SCRIPT,
                           SCRIPT.replace(b'step_4 --flag', b'step_4 --fast')
                           .replace(b'step_7 --flag', b'step_7 --a'),
                           SCRIPT.replace(b'step_4 --flag', b'step_4 --slow')
                           .replace(b'step_7 --flag', b'step_7 --a'))
    before = git(root, 'ls-files', '-u')
    causes = {}

    assert resolve_insertion_conflicts(['scripts/run.sh'], run, causes) == ['scripts/run.sh']

    assert git(root, 'ls-files', '-u') == before
    assert causes == {'scripts/run.sh': 'both sides changed line 4'}


def append_bytes(path, data):
    path.write_bytes(path.read_bytes() + data)


@pytest.fixture
def journal_and_script(repo, monkeypatch, shared):
    root = shared
    (root / 'JOURNAL.md').write_bytes(JOURNAL)
    (root / 'scripts').mkdir()
    (root / 'scripts' / 'run.sh').write_bytes(SCRIPT)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'journal and script')
    return root


def test_concurrent_deliveries_appending_the_journal_and_editing_a_script_both_land(
        repo, monkeypatch, journal_and_script):
    root = journal_and_script
    first = reviewed_workspace(repo, monkeypatch, 'task-a')
    second = reviewed_workspace(repo, monkeypatch, 'task-b')
    commit(first, 'a: journal entry, step 3', lambda w: (
        append_bytes(w / 'JOURNAL.md', b'\n\n## A\nalpha\n'),
        (w / 'scripts' / 'run.sh').write_bytes(SCRIPT.replace(b'step_3 --flag', b'step_3 --a'))))
    commit(second, 'b: journal entry, step 6, step 3 as a made it with a comment', lambda w: (
        append_bytes(w / 'JOURNAL.md', b'\n\n## B\nbeta\n'),
        (w / 'scripts' / 'run.sh').write_bytes(SCRIPT.replace(
            b'step_3 --flag\n', b'# shared fix\nstep_3 --a\n').replace(b'step_6 --flag', b'step_6 --b'))))

    receipts = settle(repo, 'task-a', 'task-b')

    for receipt in receipts:
        assert receipt['state'] == 'delivered', receipt.get('reason')
    journal = git(root, 'show', 'HEAD:JOURNAL.md')
    assert journal in {(JOURNAL + entries).decode().rstrip('\n') for entries in (
        b'\n\n## A\nalpha\n\n## B\nbeta\n', b'\n\n## B\nbeta\n\n## A\nalpha\n')}, journal
    script = git(root, 'show', 'HEAD:scripts/run.sh') + '\n'
    assert script == SCRIPT.decode().replace('step_3 --flag\n', '# shared fix\nstep_3 --a\n').replace(
        'step_6 --flag', 'step_6 --b'), script


def test_a_genuine_script_overlap_is_held_naming_the_file_and_lines(
        repo, monkeypatch, journal_and_script):
    first = reviewed_workspace(repo, monkeypatch, 'task-a')
    second = reviewed_workspace(repo, monkeypatch, 'task-b')
    commit(first, 'a: step 4', lambda w: (w / 'scripts' / 'run.sh').write_bytes(
        SCRIPT.replace(b'step_4 --flag', b'step_4 --a')))
    commit(second, 'b: step 4 and a journal entry', lambda w: (
        (w / 'scripts' / 'run.sh').write_bytes(SCRIPT.replace(b'step_4 --flag', b'step_4 --b')),
        append_bytes(w / 'JOURNAL.md', b'\n\n## B\nbeta\n')))

    receipts = settle(repo, 'task-a', 'task-b')

    held = [receipt for receipt in receipts if receipt['state'] != 'delivered']
    assert len(held) == 1, receipts
    assert held[0]['reason'] == ('task_delta_conflict: three-way merge conflicts at: '
                                 'scripts/run.sh (both sides changed line 4)'), held[0]
    assert held[0]['retry']['conflictPaths'] == ['scripts/run.sh']
