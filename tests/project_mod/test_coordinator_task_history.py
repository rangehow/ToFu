"""Task history: every task a Coordinator's board ever showed outlives later boards.

The board head keeps only the latest complete board; each changed write also
records its tasks under stable identities (docs/PROJECT_COORDINATION.md, "Task
history"). These specifications pin the identity rule, the dropped-task marker,
due outcomes, paging, the transcript seed of a head that predates the history,
and that only changed tasks are written.
"""
from copy import deepcopy
import threading
import time
from types import SimpleNamespace

import pytest

from lib.conversations.coordinator_progress import POLICY, CoordinatorTaskProgress
from lib.conversations.coordinator_task_history import (
    NAMESPACE, PAGE_SIZE, CoordinatorTaskHistory, boards_from_rounds, plan_transition, title_key)
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Store:
    """record.get/put/list over every namespace, with CAS versions."""

    def __init__(self):
        self.rows = {}; self.lock = threading.Lock(); self.history_puts = 0; self.lists = 0

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.get':
            return deepcopy(self.rows.get((payload['namespace'], payload['key'])))
        assert operation == 'record.list'
        self.lists += 1
        keys = sorted(key for namespace, key in self.rows
                      if namespace == payload['namespace'] and key.startswith(payload['prefix'])
                      and key > payload.get('after_key', ''))[:payload['limit']]
        return [{'key': key, **deepcopy(self.rows[(payload['namespace'], key)])} for key in keys]

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            if (old['version'] if old else 0) != payload['expected_version']:
                raise StorageError('database_conflict', 'CAS conflict')
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': payload['expected_version'] + 1}
            if payload['namespace'] == NAMESPACE:
                self.history_puts += 1
        return {'version': payload['expected_version'] + 1}


@pytest.fixture
def board():
    store = Store()
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    now = [1_000.0]

    def clock():
        now[0] += 60
        return now[0]

    def progress(owner=7, transcript=None):
        board = CoordinatorTaskProgress(owner, '/project', client=store, bindings=bindings,
                                        notify=lambda: None, clock=clock)
        return board

    def history(owner=7):
        return CoordinatorTaskHistory(owner, '/project', client=store, bindings=bindings, clock=lambda: now[0])

    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}

    def publish(*tasks, summary='Where things stand.'):
        return progress().publish(scope, {'summary': summary, 'tasks': [dict(task) for task in tasks]})

    def read(**query):
        return history().read(**query)

    def by_title(page):
        return {task['title']: task for task in page['tasks']}

    return SimpleNamespace(store=store, binding=binding, scope=scope, publish=publish, read=read,
                           history=history, by_title=by_title, now=now, bindings=bindings, clock=clock)


def task(title, status='in_progress', **extra):
    return {'title': title, 'status': status, **extra}


def test_a_task_dropped_from_a_later_board_stays_in_the_history(board):
    b = board
    b.publish(task('Fix the sidebar', 'completed', progress='Merged and verified.'),
              task('Research homestays', progress='Comparing four platforms.'),
              task('Summarize the article', 'pending'))
    # The next board omits the finished task and an unfinished one.
    b.publish(task('Summarize the article'))
    state = CoordinatorTaskProgress(7, '/project', client=b.store, bindings=b.bindings).read()
    assert [entry['title'] for entry in state['board']['tasks']] == ['Summarize the article']
    page = b.read()
    tasks = b.by_title(page)
    assert page['recorded'] is True and page['coverage'] == 'complete' and page['nextCursor'] is None
    assert set(tasks) == {'Fix the sidebar', 'Research homestays', 'Summarize the article'}
    done = tasks['Fix the sidebar']
    assert done['status'] == 'completed' and not done['onBoard'] and not done['dropped']
    assert done['progress'] == 'Merged and verified.' and done['departedAt'] > done['lastSeenAt']
    lost = tasks['Research homestays']
    assert lost['status'] == 'in_progress' and lost['dropped'] and not lost['onBoard']
    assert lost['progress'] == 'Comparing four platforms.'
    current = tasks['Summarize the article']
    assert current['onBoard'] and current['status'] == 'in_progress' and not current['dropped']
    assert current['firstSeenAt'] < current['changedAt'] == current['lastSeenAt']
    assert page['counts'] == {'in_progress': 1, 'pending': 0, 'dropped': 1, 'completed': 1, 'cancelled': 0}
    # Groups in contract order: in progress, pending, dropped, completed, cancelled.
    assert [entry['title'] for entry in page['tasks']] == [
        'Summarize the article', 'Research homestays', 'Fix the sidebar']


def test_an_in_place_title_edit_keeps_the_task_and_its_former_title(board):
    b = board
    b.publish(task('Ship the release (Open + Internal)'), task('Restart Tofu', 'pending'))
    b.publish(task('Ship the release (Open + Internal, private GitHub)'), task('Restart Tofu', 'pending'))
    page = b.read()
    assert len(page['tasks']) == 2
    shipped = b.by_title(page)['Ship the release (Open + Internal, private GitHub)']
    assert shipped['formerTitles'] == ['Ship the release (Open + Internal)'] and not shipped['dropped']
    # Searching finds a task by a title it had before.
    assert [entry['key'] for entry in b.read(query='open + internal)')['tasks']] == [shipped['key']]


def test_a_finished_task_replaced_in_place_ends_there(board):
    b = board
    b.publish(task('Upgrade the parser', 'completed'), task('Restart Tofu', 'pending'))
    b.publish(task('Write the migration guide'), task('Restart Tofu', 'pending'))
    tasks = b.by_title(b.read())
    assert tasks['Upgrade the parser']['status'] == 'completed' and not tasks['Upgrade the parser']['onBoard']
    assert 'formerTitles' not in tasks['Write the migration guide']


def test_identity_rule_is_title_first_then_one_for_one_in_place():
    previous = {'revision': 4, 'tasks': [task('A'), task('B'), task('C', 'completed'), task('D')]}
    keys = ['a' * 32, 'b' * 32, 'c' * 32, 'd' * 32]
    # B moved and C was reworded in place, keeping its finished status; E is new.
    board = {'revision': 5, 'tasks': [task('B'), task('A'), task('C, reworded', 'completed'),
                                      task('D'), task('E')]}
    assert plan_transition(previous, keys, board) == ['b' * 32, 'a' * 32, 'c' * 32, 'd' * 32, title_key('E')]
    # Two replaced by two between the same neighbours is ambiguous: both are new tasks.
    swapped = {'revision': 5, 'tasks': [task('A'), task('X'), task('Y'), task('D')]}
    assert plan_transition(previous, keys, swapped) == ['a' * 32, title_key('X'), title_key('Y'), 'd' * 32]
    # A title already on the board under another identity gets a key of its own.
    renamed = {'revision': 6, 'tasks': [task('Old name'), task('Renamed')]}
    clash = plan_transition({'revision': 5, 'tasks': [task('Renamed')]}, [title_key('Old name')], renamed)
    assert clash[1] == title_key('Old name') and clash[0] not in (title_key('Old name'), title_key('Renamed'))


def test_a_returning_title_continues_its_task(board):
    b = board
    b.publish(task('Router acts for you'), task('Release'))
    b.publish(task('Release'))
    assert b.by_title(b.read())['Router acts for you']['dropped']
    b.publish(task('Release'), task('Router acts for you', 'pending'))
    page = b.read()
    back = b.by_title(page)['Router acts for you']
    assert len(page['tasks']) == 2 and back['onBoard'] and not back['dropped'] and 'departedAt' not in back


def test_due_outcomes_compare_completion_with_the_promise(board):
    b = board
    base = b.now[0]

    def due(seconds):
        return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(base + seconds))
    b.publish(task('Kept', 'in_progress', due=due(1_000)), task('Missed', due=due(150)),
              task('Withdrawn', due=due(1_000)), task('Open', due=due(1_000)))
    b.publish(task('Kept', 'completed', due=due(1_000)), task('Missed', due=due(150)),
              task('Withdrawn', 'cancelled', due=due(1_000)), task('Open', due=due(1_000)))
    b.publish(task('Missed', 'completed', due=due(150)), task('Withdrawn', 'cancelled', due=due(1_000)),
              task('Open', due=due(1_000)))
    tasks = b.by_title(b.read())
    assert {title: entry['dueOutcome'] for title, entry in tasks.items()} == {
        'Kept': 'met', 'Missed': 'missed', 'Withdrawn': 'withdrawn', 'Open': 'open'}
    assert tasks['Kept']['completedAt'] <= tasks['Kept']['dueAt'] and not tasks['Kept']['onBoard']
    b.now[0] = base + 2_000
    assert b.by_title(b.read())['Open']['dueOutcome'] == 'missed'


def test_only_changed_tasks_are_written(board):
    b = board
    b.publish(*(task(f'Task {index}') for index in range(20)))
    assert b.store.history_puts == 20
    b.publish(*(task(f'Task {index}') for index in range(19)), task('Task 19', 'completed'))
    assert b.store.history_puts == 21
    b.publish(*(task(f'Task {index}') for index in range(18)), task('Task 19', 'completed'))
    assert b.store.history_puts == 22
    head = b.store.rows[next(key for key in b.store.rows if key[0] == POLICY['namespace'])]['value']
    assert len(head['taskKeys']) == 19


def test_pages_group_search_and_cursor(board):
    b = board
    for start in range(0, 72, 24):
        b.publish(*(task(f'Item {index:03}', 'completed') for index in range(start, start + 24)))
    b.publish(task('Live item'))
    first = b.read()
    assert first['counts']['completed'] == 72 and first['counts']['in_progress'] == 1
    assert len(first['tasks']) == PAGE_SIZE and first['coverage'] == 'page' and first['nextCursor']
    second = b.read(cursor=first['nextCursor'])
    assert second['nextCursor'] is None and second['coverage'] == 'complete'
    keys = [entry['key'] for entry in first['tasks'] + second['tasks']]
    assert len(keys) == len(set(keys)) == 73
    # Newest activity first within a group.
    completed = [entry for entry in first['tasks'] + second['tasks'] if entry['status'] == 'completed']
    assert [entry['changedAt'] for entry in completed] == sorted(
        (entry['changedAt'] for entry in completed), reverse=True)
    only = b.read(group='in_progress')
    assert [entry['title'] for entry in only['tasks']] == ['Live item'] and only['counts']['completed'] == 72
    found = b.read(query='item 07')
    assert sorted(entry['title'] for entry in found['tasks']) == [f'Item {index:03}' for index in range(70, 72)]
    assert found['counts']['completed'] == 2
    with pytest.raises(ValueError):
        b.read(group='completed', cursor=first['nextCursor'])
    with pytest.raises(ValueError):
        b.read(group='blocked')


def test_history_belongs_to_its_owner_and_binding(board):
    b = board
    b.publish(task('Private task'))
    assert b.read()['tasks'] and not b.history(owner=8).read()['tasks']
    b.binding['revision'] = 2
    assert b.read()['tasks'] == [] and b.read()['bindingRevision'] == 2
    b.binding.update(availability='unbound', conversationId='', revision=3)
    assert b.read() == {'projectPath': '/project', 'conversationId': '', 'bindingRevision': 3,
                        'recorded': True, 'counts': {name: 0 for name in
                                                     ('in_progress', 'pending', 'dropped', 'completed', 'cancelled')},
                        'tasks': [], 'nextCursor': None, 'coverage': 'complete'}


def _legacy_head(b, *tasks, revision=3):
    """A head written before task history: no taskKeys."""
    head_key = CoordinatorTaskProgress(7, '/project', client=b.store, bindings=b.bindings).key
    b.store.rows[(POLICY['namespace'], head_key)] = {'version': 1, 'value': {
        'scope': b.scope, 'board': {'revision': revision, 'updatedAt': 5_000, 'summary': 'Before history.',
                                    'tasks': [dict(entry) for entry in tasks]}}}


def _round(name, arguments, content=None, at=1, results=None):
    import json
    entry = {'toolName': name, 'toolArgs': json.dumps(arguments), 'tEnd': at}
    if content is not None:
        entry['toolContent'] = json.dumps(content)
    if results is not None:
        entry['results'] = results
    return entry


def test_a_head_that_predates_history_seeds_it_from_the_transcript(board, monkeypatch):
    b = board
    _legacy_head(b, task('Current work'), task('Finished earlier', 'completed'))
    page = b.read()
    assert page['recorded'] is False and {entry['title'] for entry in page['tasks']} == {
        'Current work', 'Finished earlier'}
    transcript = boards_from_rounds([
        _round('update_task_progress', {'summary': 'One.', 'tasks': [task('Old done', 'completed'), task('Gone')]},
               {'changed': True, 'revision': 1}, at=1_000),
        _round('update_task_progress', {'summary': 'Two.', 'tasks': [task('Current work')]},
               {'changed': True, 'revision': 2}, at=2_000),
    ])
    reads = []
    monkeypatch.setattr('lib.conversations.coordinator_task_history.transcript_boards',
                        lambda scope: reads.append(scope) or transcript)
    b.publish(task('Current work', 'completed'), task('Finished earlier', 'completed'))
    assert len(reads) == 1
    page = b.read()
    tasks = b.by_title(page)
    assert page['recorded'] is True
    assert set(tasks) == {'Old done', 'Gone', 'Current work', 'Finished earlier'}
    assert tasks['Old done']['status'] == 'completed' and tasks['Old done']['firstSeenAt'] == 1_000
    assert tasks['Gone']['dropped'] and tasks['Current work']['firstSeenAt'] == 2_000
    assert tasks['Current work']['onBoard'] and tasks['Current work']['status'] == 'completed'


def test_an_unreadable_transcript_keeps_the_head_unseeded_until_the_next_write(board, monkeypatch):
    b = board
    _legacy_head(b, task('Current work'))

    def unreadable(scope):
        raise StorageError('database_unavailable', 'busy', retryable=True)
    monkeypatch.setattr('lib.conversations.coordinator_task_history.transcript_boards', unreadable)
    b.publish(task('Current work', 'completed'))
    head_key = CoordinatorTaskProgress(7, '/project', client=b.store, bindings=b.bindings).key
    assert 'taskKeys' not in b.store.rows[(POLICY['namespace'], head_key)]['value']
    assert b.read()['recorded'] is False
    monkeypatch.setattr('lib.conversations.coordinator_task_history.transcript_boards', lambda scope: [])
    b.publish(task('Current work', 'completed'), task('Next'))
    assert b.read()['recorded'] is True and len(b.read()['tasks']) == 2


def test_transcript_rounds_yield_each_published_revision_once():
    boards = boards_from_rounds([
        _round('update_task_progress', {'summary': 'A.', 'tasks': [task('One', progress='Started.', due='2030-01-01T10:00:00+08:00')]},
               {'changed': True, 'revision': 1}, at=10),
        # Refused and unchanged writes publish nothing.
        _round('update_task_progress', {'summary': 'A.', 'tasks': []}, {'error': 'refused'}, at=20),
        _round('update_task_progress', {'summary': 'A.', 'tasks': [task('One')]},
               {'changed': False, 'revision': 1}, at=30),
        # A checklist write keeps the note and due of a task whose title and status stay.
        _round('todo_write', {'todos': [{'content': 'One', 'status': 'in_progress'},
                                        {'content': 'Two', 'status': 'pending'}], 'explanation': 'x'},
               at=40, results=[{'coordinatorProgress': {'revision': 2}}]),
        _round('todo_write', {'todos': []}, at=50, results=[{'todos': []}]),
    ])
    assert [(entry['revision'], entry['updatedAt']) for entry in boards] == [(1, 10), (2, 40)]
    assert boards[1]['tasks'][0] == task('One', progress='Started.', due='2030-01-01T10:00:00+08:00')
    # A new binding's board starts again at revision 1.
    again = boards_from_rounds([
        _round('update_task_progress', {'summary': 'A.', 'tasks': [task('One')]}, {'changed': True, 'revision': 5}),
        _round('update_task_progress', {'summary': 'B.', 'tasks': [task('Two')]}, {'changed': True, 'revision': 1}),
    ])
    assert [entry['summary'] for entry in again] == ['B.']


def test_projection_of_five_thousand_tasks_stays_fast(board):
    """Performance specification: one page of a 5,000-task history."""
    from lib.conversations.coordinator_task_history import fold_boards, scope_prefix
    b = board
    boards = [{'revision': revision, 'updatedAt': revision * 1_000, 'summary': 'Batch.',
               'tasks': [task(f'Task {revision:04}-{index:02}', 'completed', progress='Done and verified.')
                         for index in range(24)]} for revision in range(1, 210)]
    records, keys = fold_boards(b.scope, boards)
    assert len(records) == 209 * 24
    for key, value in records.items():
        b.store.rows[(NAMESPACE, scope_prefix(b.scope) + key)] = {'value': value, 'version': 1}
    head_key = CoordinatorTaskProgress(7, '/project', client=b.store, bindings=b.bindings).key
    b.store.rows[(POLICY['namespace'], head_key)] = {'version': 1, 'value': {
        'scope': b.scope, 'board': boards[-1], 'taskKeys': keys}}
    started = time.perf_counter()
    page = b.read()
    first = time.perf_counter() - started
    started = time.perf_counter()
    found = b.read(query='task 0105-1')
    searched = time.perf_counter() - started
    assert page['counts']['completed'] == 5016 and len(page['tasks']) == PAGE_SIZE
    assert len(found['tasks']) == 10
    print(f'history read 5016 tasks: first page {first * 1000:.0f} ms, search {searched * 1000:.0f} ms')
    # The bound documents the measured cost with room for a loaded CI host.
    assert first < 2.0 and searched < 2.0
