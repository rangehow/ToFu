"""Tracked files inside ignored directories deliver like every other tracked file.

Git keeps tracking a file whatever ignore pattern covers its directory
(``/benchmarks/*``, ``*.egg-info/``). A delivery observes the canonical checkout
at its delta's paths alone (``task_delta_delivery._canonical_path_index``): the
paths HEAD tracks are staged whatever the ignore rules say, as the whole
capture's ``add -A -- .`` stages them, and an untracked ignored file is never
staged. Git 2.34's ``add -A`` naming a tracked path inside an ignored directory
stages it yet exits 1, which failed every such delivery. Both publication modes,
working-tree bytes and a reviewed delivery's own commit, deliver exactly the
delta (docs/CONTINUOUS_DELIVERY.md, "Tracked paths under ignore rules").
"""
import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import delivery, task_delta_delivery
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit

MEASURE, PACKAGE_INFO = 'bench/index/measure.py', 'pkg/src/pkg.egg-info/PKG-INFO'
RENAMED = 'bench/index/measured.py'
TRACKED = {MEASURE: 'flags = ["--locks"]\n', 'bench/index/keep.py': 'keep = 1\n',
           PACKAGE_INFO: 'Version: 0.3.0\n'}
# Local output next to the tracked files: ignored, untracked, never delivered.
LOCAL_OUTPUT = 'bench/index/run.out'


def modify(work):
    (work / MEASURE).write_text('flags = ["--no-optional-locks"]\n')
    (work / PACKAGE_INFO).write_text('Version: 0.4.0\n')


def delete(work):
    (work / MEASURE).unlink()


def rename(work):
    # A Worker commits its rename; an uncommitted new path under an ignore
    # rule is an untracked ignored file, which no capture stages.
    git(work, 'mv', MEASURE, RENAMED)
    git(work, 'commit', '-m', 'rename a tracked file under an ignored directory')


EDITS = {'modify': modify, 'delete': delete, 'rename': rename}
DELIVERED = {
    'modify': {MEASURE: 'flags = ["--no-optional-locks"]\n', PACKAGE_INFO: 'Version: 0.4.0\n'},
    'delete': {MEASURE: None},
    'rename': {MEASURE: None, RENAMED: TRACKED[MEASURE]},
}


def track_under_ignored_directories(root):
    (root / '.gitignore').write_text('/bench/*\n*.egg-info/\n')
    for path, text in TRACKED.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, 'add', '.gitignore')
    git(root, 'add', '-f', *TRACKED)
    git(root, 'commit', '-m', 'track files under ignored directories')
    (root / LOCAL_OUTPUT).write_text('local measurement\n')


def checkout_files(root):
    return {path.relative_to(root).as_posix(): path.read_text()
            for path in root.rglob('*') if path.is_file() and '.git' not in path.parts}


def delivered_receipt(root, owner):
    receipt = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    assert (receipt['state'], receipt.get('reason') or '') == ('delivered', ''), receipt
    return receipt


def assert_exactly_delivered(before, after, edit):
    expected = dict(before)
    for path, text in DELIVERED[edit].items():
        if text is None:
            expected.pop(path)
        else:
            expected[path] = text
    assert after == expected


@pytest.mark.parametrize('edit', sorted(EDITS))
def test_working_tree_delivery_publishes_exactly_the_delta(repo, monkeypatch, edit):
    root, _work, owner = repo
    track_under_ignored_directories(root)
    work = automatic_workspace(repo, monkeypatch)
    EDITS[edit](work)
    before = checkout_files(root)

    delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()

    assert delivered_receipt(root, owner)['publicationMode'] == 'working_tree'
    assert_exactly_delivered(before, checkout_files(root), edit)


@pytest.mark.parametrize('edit', sorted(EDITS))
def test_reviewed_delivery_commits_exactly_the_delta(repo, monkeypatch, edit):
    root, _work, owner = repo
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    # Review and publication authority have their own owner; this starts authorized.
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    track_under_ignored_directories(root)
    work = automatic_workspace(repo, monkeypatch)
    state.set_workspace_meta(project_root=str(root), task_id='automatic', user_id=owner,
                             now=ic._now(), patch={KEY: {'assignmentId': 'assignment-1'}})
    EDITS[edit](work)
    head, before = git(root, 'rev-parse', 'HEAD'), checkout_files(root)

    delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()

    assert delivered_receipt(root, owner)['publicationMode'] == 'committed'
    assert git(root, 'rev-parse', 'HEAD^') == head
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delta = git(root, 'diff', '--no-renames', '--name-status',
                row['origin']['sourceSnapshot'], row['checkpoint_sha'])
    assert git(root, 'diff', '--no-renames', '--name-status', head, 'HEAD') == delta
    assert_exactly_delivered(before, checkout_files(root), edit)
    assert git(root, 'status', '--porcelain') == ''
    assert LOCAL_OUTPUT not in git(root, 'ls-files', '--cached', '--', 'bench')


def test_a_held_rename_conflict_is_rechecked_when_its_source_changes(repo, monkeypatch):
    """A rename's source is a delta path: its bytes are an input of the held conflict."""
    root, _work, owner = repo
    track_under_ignored_directories(root)
    work = automatic_workspace(repo, monkeypatch)
    rename(work)
    writer = 'flags = ["--locks", "--writer"]\n'
    (root / MEASURE).write_text(writer)

    delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    held = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    assert held['state'] == 'blocked', held
    assert held['retry']['condition'] == 'conflict_inputs_changed', held
    assert (root / MEASURE).read_text() == writer and not (root / RENAMED).exists()

    (root / MEASURE).write_text(TRACKED[MEASURE])  # The writer drops the edit.
    delivery.resume_pending_workspace_deliveries()

    delivered_receipt(root, owner)
    assert not (root / MEASURE).exists()
    assert (root / RENAMED).read_text() == TRACKED[MEASURE]


def test_path_observation_stages_tracked_paths_and_never_an_untracked_ignored_file(repo):
    """At the delta's paths, the path index equals a whole capture of the checkout."""
    root, _work, _owner = repo
    track_under_ignored_directories(root)
    head = git(root, 'rev-parse', 'HEAD')
    modify(root)
    (root / 'bench/index/keep.py').unlink()
    (root / 'notes.txt').write_text('untracked, not ignored\n')
    paths = [MEASURE, PACKAGE_INFO, 'bench/index/keep.py', LOCAL_OUTPUT, 'notes.txt']

    observed = task_delta_delivery._canonical_path_entries(root, head, paths)

    captured = task_delta_delivery._canonical_tree(root, head)
    assert observed == ic._git(root, ['ls-tree', '-z', captured, '--', *paths], check=True).stdout
    listed = {entry.split('\t', 1)[1] for entry in observed.split('\0') if entry}
    assert listed == {MEASURE, PACKAGE_INFO, 'notes.txt'}
    tree = task_delta_delivery._canonical_paths_tree(root, head, paths)
    assert git(root, 'show', f'{tree}:{MEASURE}') == 'flags = ["--no-optional-locks"]'
    assert LOCAL_OUTPUT not in git(root, 'ls-tree', '-r', '--name-only', tree)
    assert (root / LOCAL_OUTPUT).read_text() == 'local measurement\n'


def test_a_tracked_file_replaced_by_a_directory_has_no_path_entry(repo):
    root, _work, _owner = repo
    head = git(root, 'rev-parse', 'HEAD')
    (root / 'base.txt').unlink()
    (root / 'base.txt').mkdir()
    (root / 'base.txt' / 'inner.txt').write_text('inner\n')

    assert task_delta_delivery._canonical_path_entries(root, head, ['base.txt']) is None
