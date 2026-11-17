"""Accepted deliveries that overlap in files land in sequence without a hold.

Receiver lanes serialize every landing of one canonical project, so each
reviewed delta merges onto the branch tip its predecessor published. Two
accepted checkpoints that edit the same files at different places, or that
both add a section at the same place of a shared document, land one after the
other: neither is held as ``task_delta_conflict``. A real conflict (both
sides rewrote the same lines differently) is still held, naming the exact
files.
"""
import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import delivery
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit

LINES = ''.join(f'line {number}\n' for number in range(1, 21))
DOC = '# Guide\n\n## Intro\n\nIntro text.\n\n## Usage\n\nUsage text.\n'


def reviewed_workspace(repo, monkeypatch, task_id):
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch, task_id=task_id)
    state.set_workspace_meta(project_root=str(root), task_id=task_id, user_id=owner,
                             now=ic._now(), patch={KEY: {'assignmentId': f'assignment-{task_id}'}})
    return work


@pytest.fixture
def shared(repo, monkeypatch):
    root, _work, _owner = repo
    (root / 'shared.txt').write_text(LINES)
    (root / 'GUIDE.md').write_text(DOC)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'shared files')
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    return root


def commit(work, message, edit):
    edit(work)
    git(work, 'add', '-A')
    git(work, 'commit', '-m', message)


def replace_line(path, number, text):
    path.write_text(path.read_text().replace(f'line {number}\n', f'{text}\n', 1))


def append(path, text):
    path.write_text(path.read_text() + text)


def settle(repo, *task_ids):
    """Complete every task, then run one receiver pass over their deliveries."""
    root, _work, owner = repo
    for task_id in task_ids:
        delivery.settle_task_workspace(str(root), task_id, user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    return [state.get_workspace(str(root), task_id, user_id=owner)['origin']['delivery']
            for task_id in task_ids]


def test_two_accepted_checkpoints_overlapping_in_files_land_in_sequence(repo, monkeypatch, shared):
    root = shared
    first = reviewed_workspace(repo, monkeypatch, 'task-a')
    second = reviewed_workspace(repo, monkeypatch, 'task-b')
    commit(first, 'a: edit line 3, add a guide section', lambda w: (
        replace_line(w / 'shared.txt', 3, 'line 3 (a)'),
        append(w / 'GUIDE.md', '\n## Landing A\n\nA text.\n')))
    commit(second, 'b: edit line 15, add a guide section', lambda w: (
        replace_line(w / 'shared.txt', 15, 'line 15 (b)'),
        append(w / 'GUIDE.md', '\n## Landing B\n\nB text.\n')))

    base = git(root, 'rev-parse', 'HEAD')

    receipts = settle(repo, 'task-a', 'task-b')

    for receipt in receipts:
        assert receipt['state'] == 'delivered', receipt.get('reason')
        assert receipt['publicationMode'] == 'committed' and not receipt.get('retry'), receipt
    # One landing each, the second merged onto the first's published result.
    assert git(root, 'rev-parse', 'HEAD^^') == base
    assert {receipt['targetSha'] for receipt in receipts} == {
        git(root, 'rev-parse', 'HEAD'), git(root, 'rev-parse', 'HEAD^')}
    shared_text = git(root, 'show', 'HEAD:shared.txt')
    assert 'line 3 (a)' in shared_text and 'line 15 (b)' in shared_text
    guide = git(root, 'show', 'HEAD:GUIDE.md')
    assert '## Landing A\n\nA text.\n' in guide + '\n' and '## Landing B\n\nB text.' in guide, guide
    assert guide.startswith(DOC.rstrip('\n'))
    assert git(root, 'status', '--porcelain') == ''


def test_a_real_overlap_is_held_naming_the_exact_files(repo, monkeypatch, shared):
    root = shared
    first = reviewed_workspace(repo, monkeypatch, 'task-a')
    second = reviewed_workspace(repo, monkeypatch, 'task-b')
    commit(first, 'a: rewrite line 7', lambda w: replace_line(w / 'shared.txt', 7, 'line 7 (a)'))
    commit(second, 'b: rewrite line 7', lambda w: (
        replace_line(w / 'shared.txt', 7, 'line 7 (b)'),
        (w / 'other.txt').write_text('b only\n')))

    landed, held = settle(repo, 'task-a', 'task-b')
    held_id = 'task-b'
    if landed['state'] != 'delivered':
        landed, held, held_id = held, landed, 'task-a'
    head = git(root, 'rev-parse', 'HEAD')

    assert landed['state'] == 'delivered', landed
    assert held['state'] == 'blocked' and held['phase'] == 'commit_adoption', held
    assert held['reason'] == 'task_delta_conflict: three-way merge conflicts at: shared.txt'
    assert held['retry']['conflictPaths'] == ['shared.txt']
    assert git(root, 'rev-parse', 'HEAD') == head == landed['targetSha']

    # The lead sees the exact files and a ready rework brief for the held delivery.
    row = state.get_workspace(str(root), held_id, user_id=1)
    monkeypatch.setattr(publication, '_associated_receipt',
                        lambda *args, **kwargs: (held_id, row, held))
    view = publication.delivery_view({}, {}, None)
    assert view['status'] == 'blocked'
    assert view['conflict']['paths'] == ['shared.txt'] and view['conflict']['pathCount'] == 1
    assert view['conflict']['canonicalSha'] == head
    rework = view['conflict']['rework']
    assert held['checkpointSha'] in rework and 'shared.txt' in rework
    monkeypatch.setattr(publication, '_associated_receipt',
                        lambda *args, **kwargs: (held_id, row, landed))
    assert 'conflict' not in publication.delivery_view({}, {}, None)


def _conflicted_merge(tmp_path, base, ours, theirs, name='doc.md'):
    """A repository mid-merge with NAME unmerged; returns (root, run port)."""
    import subprocess
    root = tmp_path / 'merge'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Merge test')
    git(root, 'config', 'user.email', 'merge@example.invalid')
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


def test_insertions_at_one_place_keep_both_the_earlier_landing_first(tmp_path):
    from lib.project_mod.insertion_conflicts import resolve_insertion_conflicts
    base = b'# Doc\n\n<<<<<<< not a marker\nend\n'
    root, run = _conflicted_merge(tmp_path, base, base + b'ours 1\nours 2\n',
                                  base + b'theirs 1\n=======\n')

    assert resolve_insertion_conflicts(['doc.md'], run) == []

    assert git(root, 'ls-files', '-u') == ''
    assert git(root, 'show', ':doc.md') + '\n' == (
        base + b'ours 1\nours 2\ntheirs 1\n=======\n').decode()


def test_a_rewrite_of_the_same_lines_stays_unmerged_with_its_insertions(tmp_path):
    from lib.project_mod.insertion_conflicts import resolve_insertion_conflicts
    base = b'one\ntwo\nthree\nfour\nfive\nsix\nseven\n'
    root, run = _conflicted_merge(
        tmp_path, base, b'one\nTWO ours\nthree\nfour\nfive\nsix\nseven\nours tail\n',
        b'one\nTWO theirs\nthree\nfour\nfive\nsix\nseven\ntheirs tail\n')
    before = git(root, 'ls-files', '-u')

    assert resolve_insertion_conflicts(['doc.md'], run) == ['doc.md']

    assert git(root, 'ls-files', '-u') == before


def test_a_landed_delivery_records_its_checkpoint_as_merged(repo, monkeypatch, shared):
    root = shared
    work = reviewed_workspace(repo, monkeypatch, 'task-a')
    commit(work, 'a: edit line 3', lambda w: replace_line(w / 'shared.txt', 3, 'line 3 (a)'))
    base = git(root, 'rev-parse', 'HEAD')

    [landed] = settle(repo, 'task-a')

    assert landed['state'] == 'delivered', landed
    assert git(root, 'rev-parse', 'HEAD^1') == base
    assert git(root, 'rev-parse', 'HEAD^2') == landed['checkpointSha']


def test_a_superseded_conflict_settles_once_its_integration_lands(repo, monkeypatch, shared):
    """The integration Worker merged the held checkpoint: its landing contains it."""
    root, _work, owner = repo
    first = reviewed_workspace(repo, monkeypatch, 'task-a')
    second = reviewed_workspace(repo, monkeypatch, 'task-b')
    commit(first, 'a: rewrite line 7', lambda w: replace_line(w / 'shared.txt', 7, 'line 7 (a)'))
    commit(second, 'b: rewrite line 7', lambda w: replace_line(w / 'shared.txt', 7, 'line 7 (b)'))
    receipts = dict(zip(('task-a', 'task-b'), settle(repo, 'task-a', 'task-b')))
    held_id = next(task for task, receipt in receipts.items() if receipt['state'] == 'blocked')
    held = receipts[held_id]
    assert held['reason'].startswith('task_delta_conflict'), held

    integration = reviewed_workspace(repo, monkeypatch, 'task-c')
    merged = __import__('subprocess').run(
        ['git', '-C', str(integration), 'merge', '--no-edit', held['checkpointSha']],
        capture_output=True, text=True)
    assert merged.returncode == 1, merged.stdout
    commit(integration, 'c: keep both rewrites', lambda w: (w / 'shared.txt').write_text(
        LINES.replace('line 7\n', 'line 7 (a)\nline 7 (b)\n')))
    [integrated] = settle(repo, 'task-c')
    assert integrated['state'] == 'delivered', integrated
    landed = git(root, 'rev-parse', 'HEAD')

    delivery.resume_pending_workspace_deliveries()

    settled = state.get_workspace(str(root), held_id, user_id=owner)['origin']['delivery']
    assert settled['state'] == 'delivered' and settled['phase'] == 'no_changes', settled
    assert settled['headSha'] == landed and settled['checkpointSha'] == held['checkpointSha']
    assert git(root, 'rev-parse', 'HEAD') == landed
    assert 'line 7 (a)\nline 7 (b)\n' in git(root, 'show', 'HEAD:shared.txt') + '\n'


def test_an_unaccepted_checkpoint_landed_by_git_land_settles_as_delivered(repo, monkeypatch, shared):
    """No review is needed to acknowledge what the branch already holds."""
    root, _work, owner = repo
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: (
        'project_publication_authority_missing' if row['task_id'] == 'task-a' else ''))
    work = reviewed_workspace(repo, monkeypatch, 'task-a')
    commit(work, 'a: add a guide section',
           lambda w: append(w / 'GUIDE.md', '\n## Landing A\n\nA text.\n'))
    [held] = settle(repo, 'task-a')
    assert held['state'] == 'blocked' and held['reason'] == 'project_publication_authority_missing'
    # The held refusal is remembered: only a landing of the checkpoint lifts it.
    monkeypatch.setattr(publication, 'awaits_assignment', lambda row: row['task_id'] == 'task-a')
    git(root, 'merge', '--no-ff', '--no-edit', held['checkpointSha'])
    landed = git(root, 'rev-parse', 'HEAD')

    delivery.resume_pending_workspace_deliveries()

    settled = state.get_workspace(str(root), 'task-a', user_id=owner)['origin']['delivery']
    assert settled['state'] == 'delivered' and settled['phase'] == 'no_changes', settled
    assert settled['headSha'] == landed and settled['publicationMode'] == 'committed'
    assert git(root, 'rev-parse', 'HEAD') == landed


def test_a_checkpoint_the_branch_does_not_contain_stays_held_by_its_assignment(repo, monkeypatch, shared):
    root, _work, owner = repo
    monkeypatch.setattr(publication, 'publication_reason',
                        lambda row, receipt: 'project_publication_authority_missing')
    work = reviewed_workspace(repo, monkeypatch, 'task-a')
    commit(work, 'a: edit line 3', lambda w: replace_line(w / 'shared.txt', 3, 'line 3 (a)'))
    [held] = settle(repo, 'task-a')
    head = git(root, 'rev-parse', 'HEAD')
    # Same bytes on the branch, but not this checkpoint: similarity is no evidence.
    replace_line(root / 'shared.txt', 3, 'line 3 (a)')
    git(root, 'commit', '-qam', 'same edit, other commit')

    delivery.resume_pending_workspace_deliveries()

    after = state.get_workspace(str(root), 'task-a', user_id=owner)['origin']['delivery']
    assert after['state'] == 'blocked' and after['reason'] == held['reason'], after
    assert git(root, 'rev-parse', 'HEAD^') == head
