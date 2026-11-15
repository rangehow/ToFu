"""Integration radar: cross-checkout change awareness from Git ground truth.

Every test builds its own repository, linked worktrees and writer records
under tmp_path; nothing touches the checkout running the suite.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lib.project_mod import integration_radar as radar

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/shared.py', 'lib/other.py', 'lib/third.py', 'lib/new.py', 'lib/x.py'}


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    main = tmp_path / 'main'
    (main / 'lib').mkdir(parents=True)
    git(main, 'init', '-q', '-b', 'trunk')
    git(main, 'config', 'user.name', 'Writer')
    git(main, 'config', 'user.email', 'writer@example.invalid')
    (main / 'lib' / 'shared.py').write_text('VALUE = 1\n')
    (main / 'lib' / 'other.py').write_text('OTHER = 1\n')
    git(main, 'add', '-A')
    git(main, 'commit', '-qm', 'base')
    return main


def worktree(main: Path, name: str) -> Path:
    path = main.parent / name
    git(main, 'worktree', 'add', '-q', '-b', name, str(path))
    return path


def task(conversation: str, title: str = '') -> dict:
    return {'id': f'task-{conversation}', 'convId': conversation,
            'config': {'convTitle': title}}


def write(root: Path, relative: str, text: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return relative


def texts(advice) -> list[str]:
    return [text for _key, text in advice]


def records(main: Path) -> list[dict]:
    directory = main / '.git' / radar.RADAR_DIR / 'writers'
    return [json.loads(path.read_text()) for path in sorted(directory.glob('*.json'))]


def test_unlanded_work_in_another_worktree_is_named_both_ways(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    left, right = task('conv-a', 'Fix wakes'), task('conv-b', 'Queue view')

    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    assert radar.note_writes(left, str(left_root), ['lib/shared.py']) == []
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    advice = texts(radar.note_writes(right, str(right_root), ['lib/shared.py']))

    assert len(advice) == 1
    assert '"Fix wakes" (conv-a) also has unlanded changes to lib/shared.py' in advice[0]
    assert 'branch feature-a' in advice[0]
    assert f'git -C {left_root.resolve()} diff trunk -- lib/shared.py' in advice[0]
    # The earlier writer learns about the later one at its next round boundary, once.
    notices = radar.take_notices(left, str(left_root))
    assert len(notices) == 1
    assert '"Queue view" (conv-b) started changing lib/shared.py' in notices[0]
    assert radar.take_notices(left, str(left_root)) == []
    radar.note_writes(right, str(right_root), ['lib/shared.py'])
    assert radar.take_notices(left, str(left_root)) == []


@pytest.mark.parametrize('writer_holds_lock', [False, True])
def test_reading_another_checkout_never_writes_or_locks_its_index(repo, writer_holds_lock):
    """The other writer's own `git add`/`commit` must never meet radar's index.lock.

    Porcelain diff meets stat-only drift by rewriting the index it reads under
    index.lock even with --no-optional-locks (Git 2.34). A lock the writer
    holds is honored: radar still answers and leaves it in place.
    """
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py', 'lib/other.py'])
    unchanged = left_root / 'lib' / 'other.py'
    before_stat = unchanged.stat()
    os.utime(unchanged, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns - 2_000_000_000))
    admin = Path(git(left_root, 'rev-parse', '--absolute-git-dir').strip())
    index, lock = admin / 'index', admin / 'index.lock'
    if writer_holds_lock:
        lock.write_bytes(b'')
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ino)

    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    write(right_root, 'lib/other.py', 'OTHER = 2\n')
    advice = texts(radar.note_writes(task('conv-b'), str(right_root),
                                     ['lib/shared.py', 'lib/other.py']))

    assert len(advice) == 1
    assert 'also has unlanded changes to lib/shared.py in' in advice[0]
    assert (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ino) == before
    assert lock.exists() == writer_holds_lock
    if writer_holds_lock:
        assert lock.read_bytes() == b''


def test_reverted_work_stops_counting(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    write(left_root, 'lib/shared.py', 'VALUE = 1\n')

    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    assert radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py']) == []


def test_landed_work_becomes_a_trunk_change_since_the_branch_point(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    git(left_root, 'commit', '-qam', 'fix(shared): two')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'land feature-a', 'feature-a')

    right = task('conv-b')
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    advice = texts(radar.note_writes(right, str(right_root), ['lib/shared.py']))

    assert len(advice) == 1, advice
    assert 'trunk has landed changes to lib/shared.py since your branch point' in advice[0]
    assert 'fix(shared): two' in advice[0]
    assert 'Merge trunk into your branch before you verify' in advice[0]
    assert radar.note_writes(right, str(right_root), ['lib/shared.py']) == []


def test_untracked_new_files_count_as_unlanded(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/new.py', 'x = 1\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/new.py'])
    write(right_root, 'lib/new.py', 'x = 2\n')

    advice = texts(radar.note_writes(task('conv-b'), str(right_root), ['lib/new.py']))
    assert len(advice) == 1 and 'lib/new.py' in advice[0]


def test_two_writers_of_the_shared_checkout(repo):
    write(repo, 'lib/shared.py', 'VALUE = 5\n')
    radar.note_writes(task('conv-x'), str(repo), [str(repo / 'lib' / 'shared.py')])
    advice = texts(radar.note_writes(task('conv-y'), str(repo), ['lib/shared.py']))

    assert len(advice) == 1
    assert 'in the shared checkout' in advice[0]
    assert 'You both edit the same working tree' in advice[0]


def test_landing_notifies_writers_with_live_changes_and_retires_landed_records(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    third_root = worktree(repo, 'feature-c')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py'])
    write(third_root, 'lib/other.py', 'OTHER = 3\n')
    radar.note_writes(task('conv-c'), str(third_root), ['lib/other.py'])
    radar.take_notices(task('conv-a'), str(left_root))

    git(left_root, 'commit', '-qam', 'fix(shared): two')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'land feature-a', 'feature-a')
    commit = git(repo, 'rev-parse', 'HEAD').strip()
    notified = radar.notify_landing(repo, ['lib/shared.py'], commit=commit,
                                    subject='fix(shared): two', branch='feature-a',
                                    landed_root=left_root)

    assert [item['writer'] for item in notified] == ['conv-b']
    notices = radar.take_notices(task('conv-b'), str(right_root))
    assert len(notices) == 1
    assert 'feature-a landed on trunk' in notices[0] and 'lib/shared.py' in notices[0]
    assert 'Merge trunk into your branch before you verify or land' in notices[0]
    assert 'git_land runs no tests' in notices[0]
    assert radar.take_notices(task('conv-c'), str(third_root)) == []
    assert sorted(item['conversation'] for item in records(repo)) == ['conv-b', 'conv-c']


def test_shared_checkout_writer_is_told_its_edits_were_merged_on_top(repo):
    branch_root = worktree(repo, 'feature-a')
    write(repo, 'lib/other.py', 'OTHER = 9\n')
    radar.note_writes(task('conv-direct'), str(repo), ['lib/other.py'])
    write(branch_root, 'lib/other.py', 'OTHER = 1\nEXTRA = 2\n')
    git(branch_root, 'commit', '-qam', 'feat(other): extra')

    notified = radar.notify_landing(repo, ['lib/other.py'], commit='0' * 40,
                                    subject='feat(other): extra', branch='feature-a',
                                    landed_root=branch_root)
    assert [item['writer'] for item in notified] == ['conv-direct']
    notices = radar.take_notices(task('conv-direct'), str(repo))
    assert 'Your uncommitted edits in the shared checkout were merged on top' in notices[0]


def test_records_of_removed_checkouts_are_pruned(repo):
    left_root = worktree(repo, 'feature-a')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    git(repo, 'worktree', 'remove', '--force', str(left_root))

    write(repo, 'lib/shared.py', 'VALUE = 4\n')
    assert radar.note_writes(task('conv-x'), str(repo), ['lib/shared.py']) == []
    assert [item['conversation'] for item in records(repo)] == ['conv-x']


def test_a_new_task_keeps_only_unlanded_paths_of_earlier_tasks(repo):
    root = worktree(repo, 'feature-a')
    write(root, 'lib/shared.py', 'VALUE = 2\n')
    write(root, 'lib/other.py', 'OTHER = 2\n')
    radar.note_writes(task('conv-a'), str(root), ['lib/shared.py', 'lib/other.py'])
    write(root, 'lib/shared.py', 'VALUE = 1\n')

    write(root, 'lib/third.py', 'THIRD = 1\n')
    radar.note_writes(task('conv-a'), str(root), ['lib/third.py'])
    assert sorted(records(repo)[0]['paths']) == ['lib/other.py', 'lib/third.py']


def test_paths_are_literal_not_pathspec_patterns(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/[x].py', 'x = 1\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/[x].py'])
    write(left_root, 'lib/x.py', 'x = 1\n')
    write(right_root, 'lib/[x].py', 'x = 2\n')

    advice = texts(radar.note_writes(task('conv-b'), str(right_root), ['lib/[x].py']))
    assert len(advice) == 1 and 'lib/[x].py' in advice[0]


def test_generated_outputs_are_not_reported(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    bundle = 'frontend/src/runtime/app-runtime.js'
    write(left_root, bundle, 'left build\n')
    radar.note_writes(task('conv-a'), str(left_root), [bundle])
    write(right_root, bundle, 'right build\n')

    assert radar.note_writes(task('conv-b'), str(right_root), [bundle]) == []
    assert radar.notify_landing(repo, [bundle], commit='0' * 40, subject='build',
                                branch='feature-a') == []


def test_outside_git_or_without_a_conversation_is_silent(tmp_path, repo):
    plain = tmp_path / 'plain'
    plain.mkdir()
    write(plain, 'x.py', '1\n')
    assert radar.note_writes(task('conv-z'), str(plain), ['x.py']) == []
    assert radar.take_notices(task('conv-z'), str(plain)) == []
    assert radar.note_writes({'config': {}}, str(repo), ['lib/shared.py']) == []
    assert not (repo / '.git' / radar.RADAR_DIR).exists()


def _counting_record_reads(monkeypatch) -> list[str]:
    reads = []
    original = radar._read_json

    def read(path):
        if path.parent.name == 'writers':
            reads.append(path.name)
        return original(path)

    monkeypatch.setattr(radar, '_read_json', read)
    return reads


def test_an_ordinary_write_reads_only_the_records_naming_its_paths(repo, monkeypatch):
    # Records accumulate for every conversation that ever wrote in the
    # repository; on the shared volume each read costs a round trip, so a
    # write must not pay for the writers of unrelated files.
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    for index in range(30):
        write(repo, f'lib/unrelated_{index}.py', f'x = {index}\n')
        radar.note_writes(task(f'conv-{index}'), str(repo), [f'lib/unrelated_{index}.py'])
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])

    reads = _counting_record_reads(monkeypatch)
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    advice = texts(radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py']))

    assert len(advice) == 1 and 'conv-a also has unlanded changes' in advice[0]
    others = {name for name in reads if not name.startswith('conv-b--')}
    assert len(others) == 1 and next(iter(others)).startswith('conv-a--')


def test_a_new_task_indexes_the_paths_its_earlier_tasks_recorded(repo):
    # A record written before the path index existed (or an index lost with
    # its files) is indexed again by the writer's next task.
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    import shutil
    shutil.rmtree(repo / '.git' / radar.RADAR_DIR / 'paths')

    write(left_root, 'lib/other.py', 'OTHER = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/other.py'])
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    advice = texts(radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py']))
    assert len(advice) == 1 and 'conv-a also has unlanded changes' in advice[0]


def test_a_path_the_record_no_longer_names_drops_its_marker(repo):
    root = worktree(repo, 'feature-a')
    write(root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(root), ['lib/shared.py'])
    write(root, 'lib/shared.py', 'VALUE = 1\n')  # reverted: pruned by the next task
    write(root, 'lib/third.py', 'THIRD = 1\n')
    radar.note_writes(task('conv-a'), str(root), ['lib/third.py'])
    marker_dir = radar._path_marker_dir(repo / '.git', 'lib/shared.py')
    assert len(os.listdir(marker_dir)) == 1

    write(repo, 'lib/shared.py', 'VALUE = 4\n')
    assert radar.note_writes(task('conv-x'), str(repo), ['lib/shared.py']) == []
    assert [name for name in os.listdir(marker_dir) if name.startswith('conv-a--')] == []


def test_an_announced_overlap_is_not_requeried_on_later_writes(repo, monkeypatch):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    write(left_root, 'lib/other.py', 'OTHER = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py', 'lib/other.py'])
    right = task('conv-b')
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    assert len(radar.note_writes(right, str(right_root), ['lib/shared.py'])) == 1

    queried = []
    original = radar.live_paths

    def counting(checkout, trunk, paths):
        queried.append((checkout.root, sorted(paths)))
        return original(checkout, trunk, paths)

    monkeypatch.setattr(radar, 'live_paths', counting)
    write(right_root, 'lib/shared.py', 'VALUE = 4\n')
    assert radar.note_writes(right, str(right_root), ['lib/shared.py']) == []
    assert queried == []

    # A path not yet announced is still queried and named on its own.
    write(right_root, 'lib/other.py', 'OTHER = 3\n')
    advice = texts(radar.note_writes(right, str(right_root),
                                     ['lib/shared.py', 'lib/other.py']))
    assert queried == [(left_root.resolve(), ['lib/other.py'])]
    assert len(advice) == 1
    assert 'unlanded changes to lib/other.py in' in advice[0]
    # A new task announces again.
    assert len(radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py'])) == 1
