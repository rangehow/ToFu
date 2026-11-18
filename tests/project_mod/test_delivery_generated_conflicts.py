"""Reviewed deliveries whose only conflict is in generated outputs land by themselves.

Two Workers that each add i18n keys both regenerate the compact catalogs; the
second delivery conflicts in every generated output although the authored
locale sources merge. The publisher rebuilds those outputs from the merged
sources (``generated_integration.resolve_generated_conflicts``), so no repair
Worker is needed. A conflict in an authored source still holds the delivery
and names that source. A held delivery is rechecked when a publication moves
the branch (``checkout_landing.announce_branch_moved``), not on a timer.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import checkout_landing, delivery
from tests.storage.test_integration_delivery import ROOT, automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit

LINES = ''.join(f'line {number}\n' for number in range(1, 21))
LOCALE = 'frontend/src/i18n/locales/en.json'
CATALOG = 'frontend/src/i18n/generated/en.generated.json'


def generate(checkout):
    subprocess.run(['node', 'scripts/gen_i18n_contract.mjs'], cwd=checkout,
                   check=True, capture_output=True, timeout=60)


def commit_all(checkout, message):
    git(checkout, 'add', '-A')
    git(checkout, 'commit', '-m', message)


def add_key(checkout, key, value, *, first):
    source = json.loads((checkout / LOCALE).read_text())
    source = {key: value, **source} if first else {**source, key: value}
    (checkout / LOCALE).write_text(json.dumps(source, indent=2) + '\n')
    generate(checkout)


def replace_line(path, number, text):
    path.write_text(path.read_text().replace(f'line {number}\n', f'{text}\n', 1))


@pytest.fixture
def reviewed(repo, monkeypatch):
    """A canonical checkout with a one-language i18n catalog, landing reviewed deliveries as commits."""
    root, _work, _owner = repo
    for directory in ('frontend/src/i18n/locales', 'frontend/src/i18n/generated', 'scripts'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / 'frontend/src/i18n/languages.json').write_text(
        '{"languages":[{"code":"en","tag":"en","name":"English",'
        '"englishName":"English"}],"sources":["en"],"fallback":"en",'
        '"default":"en"}\n')
    (root / LOCALE).write_text('{\n  "key0": "Value0",\n  "key1": "Value1"\n}\n')
    (root / 'scripts/gen_i18n_contract.mjs').write_bytes(
        (ROOT / 'scripts/gen_i18n_contract.mjs').read_bytes())
    (root / 'shared.txt').write_text(LINES)
    generate(root)
    commit_all(root, 'shared base')
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    return repo


def worker(repo, monkeypatch, task_id):
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch, task_id)
    state.set_workspace_meta(project_root=str(root), task_id=task_id, user_id=owner,
                             now=ic._now(), patch={KEY: {'assignmentId': f'assignment-{task_id}',
                                                         'owner': owner, 'projectPath': str(root)}})
    return work


def settle(repo, *task_ids):
    """Complete the tasks, then run one receiver pass; rows publish in task order."""
    root, _work, owner = repo
    for task_id in task_ids:
        delivery.settle_task_workspace(str(root), task_id, user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    return [receipt(repo, task_id) for task_id in task_ids]


def receipt(repo, task_id):
    root, _work, owner = repo
    return state.get_workspace(str(root), task_id, user_id=owner)['origin']['delivery']


def test_two_deliveries_that_each_add_i18n_keys_both_land(reviewed, monkeypatch):
    root = reviewed[0]
    first, second = worker(reviewed, monkeypatch, 'first'), worker(reviewed, monkeypatch, 'second')
    add_key(first, 'first', 'First', first=True)
    commit_all(first, 'first: add a key')
    add_key(second, 'second', 'Second', first=False)
    commit_all(second, 'second: add a key')

    first_receipt, landed = settle(reviewed, 'first', 'second')

    assert first_receipt['state'] == 'delivered', first_receipt
    assert landed['state'] == 'delivered' and landed['publicationMode'] == 'committed', landed
    assert json.loads(git(root, 'show', f'HEAD:{CATALOG}')) == {
        'first': 'First', 'key0': 'Value0', 'key1': 'Value1', 'second': 'Second'}
    assert json.loads(git(root, 'show', f'HEAD:{LOCALE}')) == json.loads(git(root, 'show', f'HEAD:{CATALOG}'))
    # The landed outputs are exactly what the generator makes from the landed sources.
    subprocess.run(['node', 'scripts/gen_i18n_contract.mjs', '--check'], cwd=root,
                   check=True, capture_output=True, timeout=60)
    assert git(root, 'status', '--porcelain') == ''


def test_a_genuine_source_conflict_is_held_and_names_the_source(reviewed, monkeypatch):
    root = reviewed[0]
    first, second = worker(reviewed, monkeypatch, 'first'), worker(reviewed, monkeypatch, 'second')
    for checkout, name, key_first in ((first, 'first', True), (second, 'second', False)):
        add_key(checkout, name, name.title(), first=key_first)
        replace_line(checkout / 'shared.txt', 10, f'line 10 ({name})')
        commit_all(checkout, f'{name}: key and line 10')
    first_receipt, held = settle(reviewed, 'first', 'second')

    assert first_receipt['state'] == 'delivered', first_receipt
    assert git(root, 'show', 'HEAD:shared.txt').count('line 10 (first)') == 1

    assert held['state'] == 'blocked' and held['phase'] == 'commit_adoption', held
    assert held['reason'].startswith(
        'task_delta_conflict: three-way merge conflicts at: shared.txt (both sides changed line 10);')
    assert 'generated outputs rebuild from the merged sources' in held['reason']
    assert 'shared.txt' in held['retry']['conflictPaths']
    assert CATALOG in held['retry']['conflictPaths']
    assert git(root, 'log', '-1', '--format=%s') == 'first: key and line 10'
    assert git(root, 'status', '--porcelain') == ''


def land_commit(root, path, data, message):
    """Move the branch through the shared publication owner, as git_land does."""
    head = git(root, 'rev-parse', 'HEAD')
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
        run = lambda *args, **kwargs: subprocess.run(  # noqa: E731
            ['git', '-C', str(root), *args], env=env, check=True, text=True,
            capture_output=True, **kwargs).stdout.strip()
        run('read-tree', head)
        blob = run('hash-object', '-w', '--stdin', input=data)
        run('update-index', '--cacheinfo', f'100644,{blob},{path}')
        tree = run('write-tree')
    commit = git(root, 'commit-tree', tree, '-p', head, '-m', message)
    steps = checkout_landing.plan(root, head, tree, True)
    checkout_landing.publish(root, 'refs/heads/main', head, commit, steps, message)


def test_a_held_delivery_lands_when_another_landing_moves_the_branch(reviewed, monkeypatch):
    root = reviewed[0]
    work = worker(reviewed, monkeypatch, 'held')
    replace_line(root / 'shared.txt', 10, 'line 10 (canonical)')
    commit_all(root, 'canonical: line 10')
    replace_line(work / 'shared.txt', 10, 'line 10 (task)')
    (work / 'feature.txt').write_text('task feature\n')
    commit_all(work, 'task: line 10')
    assert settle(reviewed, 'held')[0]['state'] == 'blocked'
    # Idle receiver: no pass requested or outstanding, so no timer rechecks it.
    monkeypatch.setattr(delivery, '_SCAN_REQUESTED', False)
    monkeypatch.setattr(delivery, '_ACTION_OUTSTANDING', False)
    assert not delivery.resume_due_workspace_deliveries()
    assert receipt(reviewed, 'held')['state'] == 'blocked'

    # Another writer lands the task's version of line 10: the conflict is gone.
    land_commit(root, 'shared.txt', LINES.replace('line 10\n', 'line 10 (task)\n'),
                'other: line 10 as the task has it')
    delivery.resume_due_workspace_deliveries()

    landed = receipt(reviewed, 'held')
    assert landed['state'] == 'delivered', landed
    assert git(root, 'show', 'HEAD:feature.txt') == 'task feature'
    assert 'line 10 (task)' in git(root, 'show', 'HEAD:shared.txt')
