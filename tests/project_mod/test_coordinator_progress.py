"""The Coordinator task board is one validated, binding-scoped, idempotent head.

Its display translation rides on that head: carried forward for texts a changed
board still shows, merged per text into the board the human currently sees, and
derived by an optional job that never fails or blocks the board.
"""
from copy import deepcopy
import json
import logging
from types import SimpleNamespace
import threading

import pytest

from lib.conversations.coordinator_progress import (
    CoordinatorTaskProgress, POLICY, board_texts, validate_progress)
from lib.conversations.coordinator_progress_translation import schedule_progress_translation
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Store:
    """Records of every namespace; ``puts``, ``reads`` and ``head_key`` concern only the board head."""

    def __init__(self):
        self.rows = {}; self.lock = threading.Lock(); self.lost_ack = False; self.puts = 0; self.reads = 0
        # Runs once before the next CAS check, like a writer racing that write.
        self.before_put = None
        self.after_put = None
        self.receipts = {}

    def __call__(self, **kwargs):
        return self

    @property
    def head_key(self):
        return next(key for key in self.rows if key[0] == POLICY['namespace'])

    def query(self, operation, payload, **kwargs):
        if operation == 'record.list':
            keys = sorted(key for namespace, key in self.rows
                          if namespace == payload['namespace'] and key.startswith(payload['prefix'])
                          and key > payload.get('after_key', ''))[:payload['limit']]
            return [{'key': key, **deepcopy(self.rows[(payload['namespace'], key)])} for key in keys]
        assert operation == 'record.get'
        if payload['namespace'] == POLICY['namespace']:
            self.reads += 1
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        if self.before_put is not None and payload['namespace'] == POLICY['namespace']:
            race, self.before_put = self.before_put, None
            race()
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            if command_id in self.receipts:
                submitted, result = self.receipts[command_id]
                assert submitted == payload
                return result
            old = self.rows.get(identity)
            if (old['version'] if old else 0) != payload['expected_version']:
                raise StorageError('database_conflict', 'CAS conflict')
            result = {'version': payload['expected_version'] + 1}
            self.rows[identity] = {'value': deepcopy(payload['value']), **result}
            self.receipts[command_id] = (deepcopy(payload), result)
            head = payload['namespace'] == POLICY['namespace']
            lose_ack = head and self.lost_ack
            if head:
                self.puts += 1
                self.lost_ack = False
        if head and self.after_put is not None:
            race, self.after_put = self.after_put, None
            race()
        if lose_ack:
            raise StorageError('database_unavailable', 'ACK lost', retryable=True)
        return result


@pytest.fixture
def board():
    store = Store(); notices = []
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    clock = iter(range(1000, 10_000, 10))

    def factory(owner=7, path='/project', **seams):
        return CoordinatorTaskProgress(owner, path, client=store, bindings=bindings,
                                       notify=lambda: notices.append('push'),
                                       clock=lambda: next(clock), **seams)
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    request = {'summary': 'The folder fix is verified; the trip research is underway.', 'tasks': [
        {'title': 'Fix folder creation in the sidebar', 'status': 'completed', 'progress': 'Merged and verified.'},
        {'title': 'Research New Year homestays', 'status': 'in_progress', 'progress': 'Comparing four platforms.'},
        {'title': 'Summarize the WeChat article', 'status': 'pending',
         'blocker': {'kind': 'next_action', 'action': 'Read the saved article and write the summary'}},
    ]}
    return SimpleNamespace(store=store, binding=binding, factory=factory, scope=scope,
                           request=request, notices=notices,
                           head=lambda: deepcopy(store.rows[store.head_key]['value']))


def _translated(texts):
    return {text: '译:' + text for text in texts}


def test_board_round_trips_for_the_current_binding_only(board):
    b = board
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': 'coordinator',
                                  'bindingRevision': 1, 'board': None,
                                  'workEvidence': {'scope': 'latest_request', 'coverage': 'unknown',
                                                   'requestId': None, 'sourceTurnId': None, 'rows': []}}
    published = b.factory().publish(b.scope, b.request)
    assert published['changed'] and published['board']['revision'] == 1
    state = b.factory().read()
    assert state['board']['summary'] == b.request['summary']
    assert state['board']['tasks'] == b.request['tasks'] and state['board']['updatedAt'] > 0
    assert b.notices == ['push']
    # A new Coordinator binding starts with an empty board; the old one never leaks.
    b.binding['revision'] = 2
    assert b.factory().read()['board'] is None
    with pytest.raises(PermissionError):
        b.factory().publish(b.scope, b.request)
    newer = dict(b.scope, bindingRevision=2)
    assert b.factory().publish(newer, {'summary': 'Fresh start.', 'tasks': []})['board']['revision'] == 1
    # An older binding cannot overwrite the newer team's board even if it is current again.
    b.binding['revision'] = 1
    with pytest.raises(PermissionError):
        b.factory().publish(b.scope, b.request)


def test_unchanged_board_is_a_quiet_noop_and_revisions_advance(board):
    b = board
    b.factory().publish(b.scope, b.request)
    again = b.factory().publish(b.scope, deepcopy(b.request))
    assert not again['changed'] and again['board']['revision'] == 1
    assert b.store.puts == 1 and b.notices == ['push']
    changed = deepcopy(b.request); changed['tasks'][1]['status'] = 'completed'
    assert b.factory().publish(b.scope, changed)['board']['revision'] == 2
    assert b.factory().read()['board']['tasks'][1]['status'] == 'completed'


def test_lost_acknowledgement_replays_without_a_second_revision(board):
    b = board
    b.store.lost_ack = True
    published = b.factory().publish(b.scope, b.request)
    assert published['board']['revision'] == 1 and b.store.puts == 1


def test_lost_patch_ack_does_not_replay_over_a_newer_edit(board):
    b = board
    progress = b.factory()
    progress.publish(b.scope, b.request)
    title = b.request['tasks'][1]['title']
    b.store.lost_ack = True
    b.store.after_put = lambda: progress.publish(b.scope, {'expected_revision': 2, 'changes': [
        {'title': title, 'progress': 'Newer note.'}]})
    receipt = progress.publish(b.scope, {'changes': [{'title': title, 'progress': 'Earlier note.'}]})
    assert receipt['changed'] and receipt['board']['revision'] == 2
    head = progress.read()['board']
    assert head['revision'] == b.store.puts == 3 and head['tasks'][1]['progress'] == 'Newer note.'


@pytest.mark.parametrize('request_change', [
    {'summary': ''},
    {'summary': 'two\nlines'},
    {'tasks': [{'title': 'x', 'status': 'blocked'}]},
    {'tasks': [{'title': 'x', 'status': 'pending', 'owner': 'worker-1'}]},
    {'tasks': [{'title': 'x', 'status': 'pending', 'progress': ' '}]},
])
def test_invalid_boards_are_refused_with_the_failing_field(board, request_change):
    b = board
    with pytest.raises(ValueError, match='Invalid task board'):
        b.factory().publish(b.scope, {**b.request, **request_change})
    assert b.store.puts == 0


def test_large_board_round_trips_full_tasks_promises_and_translations(board):
    b = board
    due = '2032-05-04T08:00:00+00:00'
    tasks = [{'title': f'Task {i}: ' + '长' * 620, 'status': 'pending', 'due': due,
              'progress': f'Note {i}: ' + 'p' * 590,
              'blocker': {'kind': 'next_action', 'action': f'Start task {i}'}} for i in range(150)]
    request = {'summary': '完整摘要' * 600, 'tasks': tasks}
    progress = b.factory(); progress.arm = lambda *args: None
    progress.publish(b.scope, request, translation_target='Chinese')
    texts = board_texts(request)
    assert progress.attach_translation(b.scope, 'Chinese', _translated(texts))
    state = progress.read()
    assert state['board']['tasks'] == tasks and state['board']['summary'] == request['summary']
    assert len(set(state['taskKeys'])) == len(state['commitments']) == len(tasks)
    assert state['translation']['texts'] == [{'source': text, 'text': '译:' + text} for text in texts]


def test_changes_preserve_order_identity_notes_and_open_promises(board):
    b = board
    task = dict(b.request['tasks'][1], due='2032-05-04T08:00:00+00:00')
    tasks = [b.request['tasks'][0], task, b.request['tasks'][2]]
    progress = b.factory(); progress.arm = lambda *args: None
    progress.publish(b.scope, dict(b.request, tasks=tasks))
    keys = progress.read()['taskKeys']
    new_task = {'title': 'A new task', 'status': 'pending',
                'blocker': {'kind': 'needs_owner_decision', 'questionId': 'turn-9:q1',
                            'question': 'Should the archive be kept?'}}
    changes = {'changes': [{'title': task['title'], 'status': 'completed'}, new_task],
               'expected_revision': 1}
    after = progress.publish(b.scope, changes)
    assert after['board']['revision'] == 2
    assert after['board']['summary'] == b.request['summary']
    assert after['board']['tasks'] == [tasks[0], dict(task, status='completed'), tasks[2], new_task]
    assert progress.read()['taskKeys'][:3] == keys
    assert progress.read()['commitments'][0]['state'] == 'kept'
    with pytest.raises(ValueError, match='revision'):
        progress.publish(b.scope, {'expected_revision': 1, 'changes': [
            {'title': task['title'], 'status': 'pending'}]})
    assert progress.read()['board'] == after['board']


def test_patch_preserves_a_closed_promise_after_its_due_time(board):
    b = board
    progress = b.factory(); progress.arm = lambda *args: None
    task = dict(b.request['tasks'][1], due='1970-01-01T00:17:00+00:00')
    progress.publish(b.scope, dict(b.request, tasks=[task]))  # t=1000
    progress.publish(b.scope, {'changes': [{'title': task['title'], 'status': 'completed'}]})  # t=1010
    kept = progress.read()['commitments']
    assert kept[0]['state'] == 'kept'
    progress.publish(b.scope, {'summary': 'Next item is underway.', 'changes': [
        {'title': 'Next', 'status': 'in_progress'}]})  # t=1020 (the old due)
    assert progress.read()['commitments'] == kept
    with pytest.raises(ValueError, match='must be in the future'):
        progress.publish(b.scope, {'changes': [{'title': task['title'], 'status': 'pending',
                                                'blocker': {'kind': 'next_action',
                                                            'action': 'Re-run the comparison'}}]})
    assert progress.read()['commitments'] == kept


def test_change_rebases_on_an_independent_board_write_without_dropping_tasks(board):
    b = board
    progress = b.factory()
    progress.publish(b.scope, b.request)
    first, second, third = b.request['tasks']
    b.store.before_put = lambda: progress.publish(b.scope, {'changes': [
        {'title': third['title'], 'status': 'in_progress'}]})
    result = progress.publish(b.scope, {'changes': [{'title': second['title'], 'progress': 'New evidence.'}]})
    assert result['board']['revision'] == 3
    # A status change clears the typed wait: an in-progress row is no longer pending on it.
    assert result['board']['tasks'] == [first, dict(second, progress='New evidence.'),
                                       {'title': third['title'], 'status': 'in_progress'}]


def test_unbound_project_reads_empty_and_corrupt_heads_fail_closed(board):
    b = board
    b.factory().publish(b.scope, b.request)
    key = b.store.head_key
    b.store.rows[key]['value']['scope']['owner'] = 8
    with pytest.raises(StorageError):
        b.factory().read()
    b.binding.update(availability='unbound', conversationId='', revision=3)
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': '',
                                  'bindingRevision': 3, 'board': None,
                                  'workEvidence': {'scope': 'latest_request', 'coverage': 'unknown',
                                                   'requestId': None, 'sourceTurnId': None, 'rows': []}}


def _bind_default_progress(monkeypatch, b, notify):
    """Production's port-less construction uses the fake head; explicit ports still win."""
    original = CoordinatorTaskProgress.__init__
    defaults = {'client': b.store, 'bindings': SimpleNamespace(read=lambda _: deepcopy(b.binding)),
                'notify': notify}
    monkeypatch.setattr(CoordinatorTaskProgress, '__init__', lambda self, owner, path, **ports: original(
        self, owner, path, **{**defaults, **ports}))


@pytest.fixture
def coordinator_tool(board, monkeypatch):
    """The real tool branch over the fake head, stored Coordinator settings and a recorded scheduler."""
    from lib.conversations import coordinator_progress_translation, coordinator_tools
    from lib.conversations.repository import ConversationRepository
    b = board
    monkeypatch.setattr(coordinator_tools, '_scope', lambda task: {
        'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1})
    _bind_default_progress(monkeypatch, b, lambda: b.notices.append('push'))
    targets, scheduled, settings = [], [], {}
    original_publish = CoordinatorTaskProgress.publish

    def publish(self, scope, request, *, translation_target=None):
        targets.append(translation_target)
        return original_publish(self, scope, request, translation_target=translation_target)

    def stored(self, conversation_id, *, user_id, include_messages=True, **_):
        assert (conversation_id, user_id, include_messages) == ('coordinator', 7, False)
        return SimpleNamespace(metadata={'title': 'Coordinator', 'settings': deepcopy(settings)})

    monkeypatch.setattr(CoordinatorTaskProgress, 'publish', publish)
    monkeypatch.setattr(ConversationRepository, 'get', stored)
    monkeypatch.setattr(coordinator_progress_translation, 'schedule_progress_translation',
                        lambda *args: scheduled.append(args) or True)

    def run(task, arguments=None):
        return json.loads(coordinator_tools.execute_coordinator_tool(
            task, 'update_task_progress', b.request if arguments is None else arguments))
    return SimpleNamespace(run=run, targets=targets, scheduled=scheduled, settings=settings)


def test_coordinator_tool_publishes_through_the_bound_scope(coordinator_tool):
    result = coordinator_tool.run({})
    assert result['revision'] == 1 and result['task_count'] == 3 and result['changed'] is True
    refused = coordinator_tool.run({}, {'summary': 'ok', 'tasks': [{'title': 'x', 'status': 'waiting_on_user'}]})
    assert 'Invalid task board at tasks/0/status' in refused['error'] and refused['execution_started'] is False


def test_board_texts_are_the_summary_then_each_title_and_note_once(board):
    assert board_texts(board.request) == [
        'The folder fix is verified; the trip research is underway.',
        'Fix folder creation in the sidebar', 'Merged and verified.',
        'Research New Year homestays', 'Comparing four platforms.', 'Summarize the WeChat article']
    repeated = {'summary': 'Waiting on you.', 'tasks': [
        {'title': 'Book a homestay', 'status': 'pending', 'progress': 'Waiting on you.'},
        {'title': 'Book a homestay', 'status': 'cancelled'}]}
    assert board_texts(repeated) == ['Waiting on you.', 'Book a homestay']


def test_a_changed_board_keeps_translations_of_the_texts_it_still_shows(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert 'translation' not in b.head()
    assert b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    changed = deepcopy(b.request)
    changed['tasks'][1].update(status='completed', progress='Booked the quietest one.')
    del changed['tasks'][2]
    assert b.factory().publish(b.scope, changed, translation_target='Chinese')['changed']
    head = b.head()
    kept = [text for text in board_texts(changed) if text != 'Booked the quietest one.']
    assert head['board']['revision'] == 2 and len(kept) == 4
    assert head['translation'] == {'target': 'Chinese', 'texts': [
        {'source': text, 'text': '译:' + text} for text in kept]}
    validate_progress(head, 'progressHead')


@pytest.mark.parametrize('target', ['Japanese', None])
def test_translations_are_not_carried_into_another_language_or_without_auto_translate(board, target):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    changed = dict(b.request, summary='The trip research is underway.')
    assert b.factory().publish(b.scope, changed, translation_target=target)['changed']
    assert 'translation' not in b.head() and 'translation' not in b.factory().read()


def test_a_replaced_binding_never_inherits_translations(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    b.binding['revision'] = 2
    b.factory().publish(dict(b.scope, bindingRevision=2), b.request, translation_target='Chinese')
    assert 'translation' not in b.head()


def test_attach_merges_normalized_translations_of_the_current_board_texts(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    summary = b.request['summary']
    assert b.factory().attach_translation(b.scope, 'Chinese', {
        summary: '  文件夹修复已验证；\n行程调研\t 进行中。 ',
        'Fix folder creation in the sidebar': '修复侧边栏的文件夹创建',
        'Research New Year homestays': '长' * 560,
        'Comparing four platforms.': '长' * 561,
        'Merged and verified.': ' \n ',
        'A text this board never showed': '不应保存',
    })
    assert b.head()['translation'] == {'target': 'Chinese', 'texts': [
        {'source': summary, 'text': '文件夹修复已验证； 行程调研 进行中。'},
        {'source': 'Fix folder creation in the sidebar', 'text': '修复侧边栏的文件夹创建'},
        {'source': 'Research New Year homestays', 'text': '长' * 560},
        {'source': 'Comparing four platforms.', 'text': '长' * 561}]}
    assert b.notices == ['push', 'push']
    # A later pass adds to the stored entries, kept in board order.
    assert b.factory().attach_translation(b.scope, 'Chinese', {'Merged and verified.': '已合并并验证。'})
    assert [entry['source'] for entry in b.head()['translation']['texts']] == [
        summary, 'Fix folder creation in the sidebar', 'Merged and verified.', 'Research New Year homestays',
        'Comparing four platforms.']
    # What is already stored is neither written nor pushed again.
    puts = b.store.puts
    assert not b.factory().attach_translation(b.scope, 'Chinese', {'Merged and verified.': ' 已合并并验证。\n'})
    assert not b.factory().attach_translation(b.scope, 'Chinese', {})
    assert b.store.puts == puts and len(b.notices) == 3
    validate_progress(b.head(), 'progressHead')


def test_attach_refuses_a_missing_head_and_another_bindings_board(board):
    b = board
    summary = b.request['summary']
    assert not b.factory().attach_translation(b.scope, 'Chinese', {summary: '摘要'})
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.binding['revision'] = 2
    b.factory().publish(dict(b.scope, bindingRevision=2), b.request, translation_target='Chinese')
    # A job of the replaced binding cannot translate the newer team's board.
    assert not b.factory().attach_translation(b.scope, 'Chinese', {summary: '摘要'})
    assert 'translation' not in b.head() and b.store.puts == 2 and b.notices == ['push', 'push']


def test_another_target_replaces_the_stored_translations(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    summary = b.request['summary']
    assert b.factory().attach_translation(b.scope, 'Japanese', {summary: 'フォルダの修正を確認しました。'})
    assert b.head()['translation'] == {'target': 'Japanese', 'texts': [
        {'source': summary, 'text': 'フォルダの修正を確認しました。'}]}
    assert b.factory().untranslated_texts(b.scope, 'Japanese') == board_texts(b.request)[1:]
    assert b.factory().untranslated_texts(b.scope, 'Chinese') == board_texts(b.request)


def test_read_projects_the_translation_of_the_current_board_only(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert 'translation' not in b.factory().read()
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    state = b.factory().read()
    assert state['board'] == b.head()['board'] and state['translation'] == b.head()['translation']
    b.binding['revision'] = 2
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': 'coordinator',
                                  'bindingRevision': 2, 'board': None,
                                  'workEvidence': {'scope': 'latest_request', 'coverage': 'unknown',
                                                   'requestId': None, 'sourceTurnId': None, 'rows': []}}


@pytest.mark.parametrize('corrupt', [
    lambda translation: translation['texts'][0].update(text='two\nlines'),
    lambda translation: translation['texts'][0].update(text=' '),
    lambda translation: translation['texts'][0].update(source=123),
    lambda translation: translation.update(owner='worker-1'),
])
def test_stored_translations_are_schema_checked_and_fail_closed(board, corrupt):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    validate_progress(b.head(), 'progressHead')
    corrupt(b.store.rows[b.store.head_key]['value']['translation'])
    with pytest.raises(StorageError):
        b.factory().read()


def test_a_translation_racing_a_board_change_lands_only_on_texts_still_shown(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    changed = dict(b.request, summary='Everything is done.')
    b.store.before_put = lambda: b.factory().publish(b.scope, changed, translation_target='Chinese')
    assert b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    head = b.head()
    assert head['board']['summary'] == 'Everything is done.'
    assert [entry['source'] for entry in head['translation']['texts']] == board_texts(changed)[1:]


_PROGRESS_SCOPE = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}


def test_auto_translating_coordinator_publishes_for_its_reading_language(coordinator_tool):
    t = coordinator_tool
    task = {'config': {'autoTranslate': True, 'uiLang': 'zh'}}
    result = t.run(task)
    assert sorted(result) == ['changed', 'next_step', 'plan', 'revision', 'task_count'] and result['changed']
    assert t.targets == ['Chinese'] and t.scheduled == [(7, '/project', _PROGRESS_SCOPE, 'Chinese')]
    # An unchanged board schedules again: that retries a translation that failed before.
    assert t.run(task)['changed'] is False
    assert t.targets == ['Chinese', 'Chinese'] and len(t.scheduled) == 2


def test_coordinator_without_auto_translate_publishes_untranslated(coordinator_tool):
    t = coordinator_tool
    # The turn's explicit choice wins over the stored settings.
    t.settings.update(autoTranslate=True, uiLang='ja')
    t.run({'config': {'autoTranslate': False, 'uiLang': 'zh'}})
    t.settings.clear()
    t.run({'config': {}})
    assert t.targets == [None, None] and t.scheduled == []


def test_automatic_turns_follow_the_conversations_reading_language(coordinator_tool):
    """A dispatch continuation's config pins autoTranslate off for its synthetic input."""
    from lib.project_dispatch.admission import KEY
    t = coordinator_tool
    t.settings.update(autoTranslate=True, uiLang='ja')
    t.run({'config': {KEY: {'assignmentId': 'continuation'}, 'autoTranslate': False}})
    t.run({'config': {'model': 'without-translation-keys'}})
    assert t.targets == ['Japanese', 'Japanese']
    assert [call[3] for call in t.scheduled] == ['Japanese', 'Japanese']


@pytest.fixture
def board_job(board, monkeypatch):
    """Scheduled board jobs run inline on the fake head with a recorded translator."""
    from lib.conversations import coordinator_progress_translation
    from lib.translate import display_texts, execution
    b = board
    _bind_default_progress(monkeypatch, b, lambda: b.notices.append('push'))
    jobs, calls, warnings = [], [], []

    def submit(job_id, *, owner_user_id, function):
        jobs.append((job_id, owner_user_id))
        function()

    def translate(texts, target, *, log_tag, abort_check=None):
        calls.append((list(texts), target))
        return _translated(texts)

    class Warnings(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    monkeypatch.setattr(execution, 'submit_reconstructible_translation', submit)
    monkeypatch.setattr(display_texts, 'translate_display_texts', translate)
    handler = Warnings(logging.WARNING)
    coordinator_progress_translation.logger.addHandler(handler)
    yield SimpleNamespace(jobs=jobs, calls=calls, warnings=warnings)
    coordinator_progress_translation.logger.removeHandler(handler)


def test_board_job_translates_only_missing_texts_and_hints(board, board_job):
    b, j = board, board_job
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is True
    assert j.calls == [(board_texts(b.request), 'Chinese')]
    assert b.factory().read()['translation'] == {'target': 'Chinese', 'texts': [
        {'source': text, 'text': '译:' + text} for text in board_texts(b.request)]}
    assert b.notices == ['push', 'push'] and not j.warnings
    # Fully translated: an unchanged re-publish costs one head read and no provider work.
    reads = b.store.reads
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese')
    assert b.store.reads == reads + 1 and len(j.calls) == 1
    # After a change only the new text is sent; the others were carried forward.
    changed = deepcopy(b.request)
    changed['tasks'][2].update(status='in_progress', progress='Reading it now.')
    b.factory().publish(b.scope, changed, translation_target='Chinese')
    schedule_progress_translation(7, '/project', b.scope, 'Chinese')
    assert j.calls[-1] == (['Reading it now.'], 'Chinese')
    assert len(b.factory().read()['translation']['texts']) == len(board_texts(changed))
    # Each schedule is its own lane job, so a queued or running one never swallows a later publish.
    assert len({job_id for job_id, _ in j.jobs}) == len(j.jobs) == 3
    assert {owner for _, owner in j.jobs} == {7}


def test_board_job_of_a_replaced_binding_writes_nothing(board, board_job):
    b, j = board, board_job
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', dict(b.scope, coordinatorId='replaced'), 'Chinese')
    assert j.calls == [] and b.store.puts == 1 and b.notices == ['push'] and not j.warnings


def test_board_job_failure_is_logged_and_leaves_the_board_as_written(board, board_job, monkeypatch):
    from lib.translate import display_texts
    b, j = board, board_job

    def unavailable(texts, target, *, log_tag, abort_check=None):
        raise RuntimeError('translation provider unavailable')

    monkeypatch.setattr(display_texts, 'translate_display_texts', unavailable)
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is True
    assert 'translation' not in b.factory().read() and b.store.puts == 1
    assert len(j.warnings) == 1 and all(part in j.warnings[0] for part in (
        'owner=7', 'project=/project', 'coordinator=coordinator', 'target=Chinese'))


def test_refused_lane_admission_is_logged_not_raised(board, board_job, monkeypatch):
    from lib.translate import execution
    b, j = board, board_job

    def refuse(job_id, *, owner_user_id, function):
        raise RuntimeError('cannot submit after lane shutdown')

    monkeypatch.setattr(execution, 'submit_reconstructible_translation', refuse)
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is False
    assert len(j.warnings) == 1 and 'coordinator=coordinator' in j.warnings[0]


# One progress source (owner decision 2026-11-08): a board-keeping Coordinator's
# checklist is its task board. todo_write and update_task_progress write that one
# head; the task's checklist state is its plan projection, never a second list.

_COORDINATOR_SCOPE = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator',
                      'binding_revision': 1}


@pytest.fixture
def coordinator_task(coordinator_tool, monkeypatch):
    """A task of the bound, board-keeping Coordinator plus both real progress write paths."""
    from lib.conversations.coordinator_progress import plan_publication
    from lib.tasks_pkg import coordinator_policy
    from lib.tools import todo
    monkeypatch.setattr(coordinator_policy, 'coordinator_scope', lambda task, project_path=None: (
        dict(_COORDINATOR_SCOPE) if task.get('convId') == 'coordinator' else None))
    task = {'id': 'coordinator-turn', 'convId': 'coordinator', '_userId': 7, 'config': {}}

    def checklist(arguments):
        return todo.apply_todo_write_to_task(task, arguments, publish=plan_publication(task))
    return SimpleNamespace(task=task, checklist=checklist, run=coordinator_tool.run,
                           scheduled=coordinator_tool.scheduled, settings=coordinator_tool.settings)


def _shown_board(board):
    return board.factory().read()['board']


def _assert_one_progress_source(b, task):
    from lib.conversations.coordinator_progress import plan_from_board
    shown = _shown_board(b)
    assert task['_todoState']['todos'] == plan_from_board(shown)['todos'] == task['_todos']
    return shown


def test_checklist_and_task_board_are_one_progress_source(board, coordinator_task):
    from lib.tools.todo import render_todo_snapshot
    b, c = board, coordinator_task
    _, text, outcome = c.checklist({'explanation': 'Release first, then the notes.', 'todos': [
        {'content': 'Release 0.19.0', 'status': 'in_progress'},
        {'content': 'Write the release notes', 'status': 'pending'}]})
    assert not outcome['rejected'] and text.startswith('Checklist updated: 0/2 completed.')
    assert 'task board' in text.partition('\n')[0]
    shown = _assert_one_progress_source(b, c.task)
    # The first board is summarized by the checklist's explanation.
    assert shown['revision'] == 1 and shown['summary'] == 'Release first, then the notes.'
    assert shown['tasks'] == [{'title': 'Release 0.19.0', 'status': 'in_progress'},
                              {'title': 'Write the release notes', 'status': 'pending'}]
    assert outcome['publication']['coordinatorProgress'] == {
        'projectPath': '/project', 'conversationId': 'coordinator', 'bindingRevision': 1, 'revision': 1}

    # The board tool writes the same list; dropped work is no longer part of the plan.
    result = c.run(c.task, {'summary': 'Released; writing the notes.', 'tasks': [
        {'title': 'Release 0.19.0', 'status': 'completed', 'progress': 'Pushed to all three remotes.'},
        {'title': 'Write the release notes', 'status': 'in_progress', 'progress': 'Drafting highlights.'},
        {'title': 'Announce in the group', 'status': 'cancelled'}]})
    assert result['revision'] == 2 and result['changed'] is True
    _assert_one_progress_source(b, c.task)
    assert c.task['_todoState']['explanation'] == 'Released; writing the notes.'
    assert c.task['_todos'] == [{'content': 'Release 0.19.0', 'status': 'completed'},
                                {'content': 'Write the release notes', 'status': 'in_progress'}]
    # The receipt shows the board as the checklist the model now holds.
    assert result['plan'] == render_todo_snapshot(c.task['_todoState'])

    # A checklist rewrite keeps the summary and the notes of tasks it leaves unchanged.
    c.checklist({'todos': [
        {'content': 'Release 0.19.0', 'status': 'completed'},
        {'content': 'Write the release notes', 'status': 'completed'},
        {'content': 'Update the changelog', 'status': 'in_progress'}]})
    shown = _assert_one_progress_source(b, c.task)
    assert shown['revision'] == 3 and shown['summary'] == 'Released; writing the notes.'
    assert shown['tasks'] == [
        {'title': 'Release 0.19.0', 'status': 'completed', 'progress': 'Pushed to all three remotes.'},
        {'title': 'Write the release notes', 'status': 'completed'},
        {'title': 'Update the changelog', 'status': 'in_progress'}]
    # An identical checklist is a no-op for both views.
    puts = b.store.puts
    _, _, again = c.checklist({'todos': c.task['_todos']})
    assert again['no_op'] and b.store.puts == puts


def test_large_board_tool_keeps_complete_checkpoint_and_a_compact_receipt(board, coordinator_task):
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
    c = coordinator_task
    tasks = [{'title': f'Outcome {index}: ' + '长' * 620, 'status': 'pending',
              'blocker': {'kind': 'next_action', 'action': f'Start outcome {index}'}} for index in range(150)]
    request = {'summary': '完整摘要' * 600, 'tasks': tasks}
    result = c.run(c.task, request)
    assert result['task_count'] == 150 and result['revision'] == 1
    assert tasks[-1]['title'] not in result['plan'] and request['summary'] not in result['plan']
    assert 'update_task_progress(read={})' in result['plan']
    checkpoint = c.task['_todoState']
    restored = prepare_resume_state({'checkpointTodoState': checkpoint}).checkpoint_todo_state
    assert restored == checkpoint and restored['explanation'] == request['summary']
    assert [row['content'] for row in restored['todos']] == [row['title'] for row in tasks]
    block = build_checklist_context_block([], {'_todoState': restored})
    assert result['plan'] in block.content and tasks[-1]['title'] not in block.content
    puts, state = board.store.puts, deepcopy(checkpoint)
    assert c.run(c.task, {'read': {}})['tasks'] == tasks
    assert c.run(c.task, {'read': {'title': tasks[-1]['title']}})['tasks'] == tasks[-1:]
    assert board.store.puts == puts and c.task['_todoState'] == state


def test_changes_reject_ambiguous_targets_without_touching_the_board(board):
    b = board
    progress = b.factory()
    tasks = [b.request['tasks'][0]] * 2
    progress.publish(b.scope, dict(b.request, tasks=tasks))
    with pytest.raises(ValueError, match='ambiguous'):
        progress.publish(b.scope, {'changes': [{'title': tasks[0]['title'], 'status': 'pending'}]})
    with pytest.raises(ValueError, match='one change per title'):
        progress.publish(b.scope, {'changes': [{'title': 'New', 'status': 'pending'},
                                              {'title': 'New', 'status': 'completed'}]})
    assert progress.read()['board']['tasks'] == tasks and b.store.puts == 1


def test_both_progress_tools_hand_the_browser_one_view_of_the_write(board, coordinator_task):
    from lib.conversations import coordinator_tools
    c = coordinator_task
    receipt = coordinator_tools.execute_coordinator_tool(c.task, 'update_task_progress', board.request)
    view = receipt.progress_view
    assert view['todos'] == c.task['_todos'] and view['todoState'] == c.task['_todoState']
    assert view['coordinatorProgress'] == {'projectPath': '/project', 'conversationId': 'coordinator',
                                           'bindingRevision': 1, 'revision': 1}
    assert json.loads(receipt)['revision'] == 1
    refused = coordinator_tools.execute_coordinator_tool(
        c.task, 'update_task_progress', {'summary': 'ok', 'tasks': [{'title': 'x', 'status': 'stuck'}]})
    assert 'error' in json.loads(refused) and not getattr(refused, 'progress_view', None)


@pytest.mark.parametrize('item, reason', [
    ({'content': 'first line\nsecond line', 'status': 'pending'}, 'one line'),
    ({'content': 'Wait for the owner', 'status': 'blocked'}, 'blocked'),
])
def test_a_checklist_the_board_cannot_show_is_rejected_without_a_write(board, coordinator_task, item, reason):
    b, c = board, coordinator_task
    c.checklist({'todos': [{'content': 'Release 0.19.0', 'status': 'in_progress'}]})
    accepted, puts = deepcopy(c.task['_todoState']), b.store.puts
    _, text, outcome = c.checklist({'todos': [{'content': 'Release 0.19.0', 'status': 'completed'}, item]})
    assert outcome['rejected'] and reason in outcome['reason'] and 'task board' in outcome['reason']
    assert text.startswith('Checklist update rejected:')
    assert c.task['_todoState'] == accepted and b.store.puts == puts
    _assert_one_progress_source(b, c.task)


def test_a_replaced_binding_refuses_the_stale_coordinators_checklist(board, coordinator_task):
    b, c = board, coordinator_task
    b.binding['revision'] = 2
    _, _, outcome = c.checklist({'todos': [{'content': 'Release 0.19.0', 'status': 'in_progress'}]})
    assert outcome['rejected'] and 'Coordinator binding changed' in outcome['reason']
    assert b.store.puts == 0 and '_todoState' not in c.task


def test_a_checklist_write_follows_the_boards_translation(board, coordinator_task):
    c = coordinator_task
    c.task['config'] = {'autoTranslate': True, 'uiLang': 'zh'}
    c.checklist({'todos': [{'content': 'Release 0.19.0', 'status': 'in_progress'}]})
    assert c.scheduled == [(7, '/project', _PROGRESS_SCOPE, 'Chinese')]


def test_board_from_plan_summarizes_a_first_board_and_writes_nothing_without_one():
    from lib.conversations.coordinator_progress import board_from_plan, plan_from_board
    plan = [{'content': 'Draft', 'status': 'completed'}, {'content': 'Review', 'status': 'in_progress'}]
    assert board_from_plan(None, plan, '  Drafted;\n reviewing now. ')['summary'] == 'Drafted; reviewing now.'
    assert board_from_plan(None, plan, '')['summary'] == 'Review'
    assert board_from_plan(None, plan, 'x' * 281)['summary'] == 'x' * 281
    assert board_from_plan(None, [{'content': 'Draft', 'status': 'pending'}], '')['summary'] == 'Draft'
    assert board_from_plan(None, [], 'Nothing yet.') is None
    previous = {'revision': 4, 'updatedAt': 1, 'summary': 'Kept.', 'tasks': [
        {'title': 'Draft', 'status': 'completed', 'progress': 'Done.'},
        {'title': 'Old', 'status': 'cancelled'}]}
    assert board_from_plan(previous, [], 'Cleared.') == {'summary': 'Kept.', 'tasks': []}
    assert plan_from_board(previous) == {'todos': [{'content': 'Draft', 'status': 'completed'}],
                                         'explanation': 'Kept.'}


def test_tasks_without_a_board_keep_their_own_checklist(board, coordinator_task, monkeypatch):
    from lib.conversations.coordinator_progress import plan_publication
    from lib.router.watchdog.policy import WATCHDOG_TOOLS
    from lib.tasks_pkg import coordinator_policy
    b = board
    ordinary = {'id': 'ordinary-turn', 'convId': 'ordinary', '_userId': 7, 'config': {}}
    assert plan_publication(ordinary) is None
    # The Router Watchdog is a bound Coordinator without a board.
    monkeypatch.setattr(coordinator_policy, 'coordinator_tool_ceiling', lambda scope, task=None: WATCHDOG_TOOLS)
    assert plan_publication(coordinator_task.task) is None
    # A failed role lookup is never a Coordinator's: those are pinned for the turn.
    monkeypatch.setattr(coordinator_policy, 'coordinator_scope', lambda task, project_path=None: 1 / 0)
    assert plan_publication(ordinary) is None
    assert b.store.puts == 0


def test_a_board_receipt_proves_the_checklist_visible_until_it_leaves_the_context(board, coordinator_task):
    from lib.tasks_pkg.context_composer.checklist import CHECKLIST_HEADING, restore_checklist_context
    from lib.conversations import coordinator_tools
    c = coordinator_task
    receipt = str(coordinator_tools.execute_coordinator_tool(c.task, 'update_task_progress', board.request))
    call = {'role': 'assistant', 'tool_calls': [{'id': 'board-1', 'function': {
        'name': 'update_task_progress', 'arguments': json.dumps(board.request)}}]}
    direct = [{'role': 'user', 'content': 'Continue.'}, call,
              {'role': 'tool', 'tool_call_id': 'board-1', 'content': receipt}]
    gateway = [{'role': 'user', 'content': 'Continue.'},
               {'role': 'assistant', 'tool_calls': [{'id': 'batch', 'function': {
                   'name': 'execute_tools', 'arguments': '{}'}}]},
               {'role': 'tool', 'tool_call_id': 'batch', 'content': json.dumps({'results': [
                   {'name': 'update_task_progress', 'status': 'done', 'output_truncated': False,
                    'output': receipt}]})}]
    for messages in (direct, gateway):
        frozen = deepcopy(messages)
        restore_checklist_context(messages, task=c.task, model='test-model')
        assert messages == frozen
    # Once the receipt is gone (compaction), the board is restored as the checklist.
    lost = [{'role': 'user', 'content': 'Continue.'}]
    restore_checklist_context(lost, task=c.task, model='test-model')
    restored = lost[-1]['content'] if isinstance(lost[-1]['content'], str) else json.dumps(lost[-1]['content'])
    assert CHECKLIST_HEADING in restored and 'update_task_progress(read={})' in restored
    assert 'Research New Year homestays' not in restored


def test_board_job_runs_on_the_shared_translation_lane(board, monkeypatch):
    from lib.translate import display_texts
    b = board
    attached = threading.Event()
    _bind_default_progress(monkeypatch, b, attached.set)
    monkeypatch.setattr(display_texts, 'translate_display_texts',
                        lambda texts, target, *, log_tag, abort_check=None: _translated(texts))
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is True
    assert attached.wait(30)
    assert b.factory().read()['translation']['texts'][0]['text'] == '译:' + b.request['summary']


def test_status_reason_is_explicit_and_never_follows_a_status_change(board):
    progress = board.factory()
    progress.publish(board.scope, {'summary': 'Two outcomes', 'tasks': [
        {'title': 'Evaluate', 'status': 'pending',
         'blocker': {'kind': 'needs_external_resource', 'resource': 'the held-out labels',
                     'check': 'the label file opens and has one row per evaluation item',
                     'ownerAction': 'download the label file and share it' },
         'reason': 'Waiting on the evaluation owner.'},
        {'title': 'Compare fees', 'status': 'in_progress'},
    ]})
    updated = progress.publish(board.scope, {'changes': [{'title': 'Compare fees', 'status': 'completed'}]})
    assert updated['board']['tasks'][0]['reason'] == 'Waiting on the evaluation owner.'
    with pytest.raises(ValueError, match='reason is only for pending'):
        progress.publish(board.scope, {'changes': [
            {'title': 'Evaluate', 'status': 'in_progress', 'reason': 'Wait for the held-out labels.'}]})
    cleared = progress.publish(board.scope, {'changes': [{'title': 'Evaluate', 'status': 'in_progress'}]})
    assert 'reason' not in cleared['board']['tasks'][0]
    cancelled = progress.publish(board.scope, {'changes': [
        {'title': 'Evaluate', 'status': 'cancelled', 'reason': 'The owner chose the alternate evaluation.'}]})
    assert cancelled['board']['tasks'][0]['reason'] == 'The owner chose the alternate evaluation.'
    cleared = progress.publish(board.scope, {'changes': [{'title': 'Evaluate', 'reason': None}]})
    assert 'reason' not in cleared['board']['tasks'][0]

# Typed blockers: a new or changed pending row must say what it waits for as one
# verifiable claim. Free-text "waiting for" prose is refused; rows already on the
# board keep reading and are flagged idle instead of being deleted or restatused.

def _assignment(assignment_id, *, status='running', revoked=False, coordinator='coordinator',
                revision=1, project='/project', owner=7):
    return {'id': assignment_id,
            'request': {'projectPath': project, 'coordinatorId': coordinator},
            'grant': {'owner': owner, 'bindingRevision': revision, 'revoked': revoked},
            'execution': {'status': status}}


@pytest.fixture
def pending_board(board):
    """The board's one pending row with no blocker: the legacy shape this rule refuses to create."""
    b = board
    b.factory().publish(b.scope, b.request)
    head = b.store.rows[b.store.head_key]['value']
    head['board']['tasks'][2].pop('blocker')
    return b


def test_a_pending_row_with_only_a_prose_reason_is_refused(board):
    with pytest.raises(ValueError) as refused:
        board.factory().publish(board.scope, {'summary': 'Parked.', 'tasks': [
            {'title': 'Recover the snapshot', 'status': 'pending',
             'reason': 'needs a trusted receipt'}]})
    # The refusal names what is missing: one of the typed claims, not a note.
    assert 'pending without a typed blocker' in str(refused.value)
    assert all(kind in str(refused.value) for kind in (
        'active_dispatch', 'blocked_by_dependency', 'needs_external_resource',
        'needs_owner_decision', 'next_action'))
    assert board.store.puts == 0


def test_the_progress_tool_refuses_a_prose_only_wait_with_its_named_reason(board, coordinator_task):
    from lib.conversations import coordinator_tools
    c = coordinator_task
    refused = coordinator_tools.execute_coordinator_tool(c.task, 'update_task_progress', {
        'summary': 'Parked.', 'tasks': [
            {'title': 'Recover the snapshot', 'status': 'pending', 'reason': 'needs a trusted receipt'}]})
    assert 'pending without a typed blocker' in str(json.loads(refused))
    assert not getattr(refused, 'progress_view', None) and board.store.puts == 0


def test_a_blocker_without_its_payload_is_refused_naming_the_empty_field(board):
    with pytest.raises(ValueError, match='needs_external_resource without check'):
        board.factory().publish(board.scope, {'summary': 'Parked.', 'tasks': [
            {'title': 'Recover the snapshot', 'status': 'pending',
             'blocker': {'kind': 'needs_external_resource', 'resource': 'a read-only snapshot'}}]})
    assert board.store.puts == 0


def test_an_internal_evidence_only_resource_is_refused_naming_the_missing_field(board):
    # The shape that parked rows on evidence nobody could produce: resource + check, no actor.
    with pytest.raises(ValueError, match='needs_external_resource without ownerAction or unavailable'):
        board.factory().publish(board.scope, {'summary': 'Parked.', 'tasks': [
            {'title': 'Recover the snapshot', 'status': 'pending',
             'blocker': {'kind': 'needs_external_resource', 'resource': 'internal telemetry history',
                         'check': 'the telemetry store has the window'}}]})
    assert board.store.puts == 0


def test_an_unavailable_resource_without_its_limit_is_refused_naming_the_field(board):
    with pytest.raises(ValueError, match='needs_external_resource unavailable without limit'):
        board.factory().publish(board.scope, {'summary': 'Parked.', 'tasks': [
            {'title': 'Recover the snapshot', 'status': 'pending',
             'blocker': {'kind': 'needs_external_resource', 'resource': 'the deleted archive',
                         'check': 'the archive lists the month', 'unavailable': True}}]})
    assert board.store.puts == 0


def test_an_owner_decision_without_its_question_is_refused_naming_the_field(board):
    with pytest.raises(ValueError, match='needs_owner_decision without question'):
        board.factory().publish(board.scope, {'summary': 'Parked.', 'tasks': [
            {'title': 'Publish the result', 'status': 'pending',
             'blocker': {'kind': 'needs_owner_decision', 'questionId': 'turn-7:q1', 'question': '  '}}]})
    assert board.store.puts == 0


@pytest.mark.parametrize('blocker', [
    {'kind': 'active_dispatch', 'assignmentId': 'assignment-1'},
    {'kind': 'blocked_by_dependency', 'assignmentId': 'assignment-2', 'revision': 3},
    {'kind': 'needs_external_resource', 'resource': 'the read-only storage snapshot',
     'check': 'the snapshot opens read-only and lists the 4,000 events',
     'ownerAction': 'approve the read-only snapshot in the storage console'},
    {'kind': 'needs_external_resource', 'resource': 'the deleted upstream archive',
     'check': 'the archive lists the missing month', 'unavailable': True,
     'limit': 'replay before March stays unverified'},
    {'kind': 'needs_owner_decision', 'questionId': 'turn-7:question-1',
     'question': 'May I publish this externally?'},
    {'kind': 'next_action', 'action': 'Run the regression suite on the merged commit'},
])
def test_each_blocker_kind_is_accepted_with_its_payload(board, blocker):
    progress = board.factory(dispatch=lambda assignment_id: _assignment(assignment_id))
    published = progress.publish(board.scope, {'summary': 'One wait.', 'tasks': [
        {'title': 'Ship the folder fix', 'status': 'pending', 'blocker': blocker}]})
    assert published['board']['tasks'] == [
        {'title': 'Ship the folder fix', 'status': 'pending', 'blocker': blocker}]
    assert progress.read()['board']['tasks'][0]['blocker'] == blocker


def test_an_in_progress_row_may_carry_a_typed_blocker_too(board):
    progress = board.factory(dispatch=lambda assignment_id: _assignment(assignment_id))
    blocker = {'kind': 'active_dispatch', 'assignmentId': 'assignment-1'}
    published = progress.publish(board.scope, {'summary': 'One wait.', 'tasks': [
        {'title': 'Ship the folder fix', 'status': 'in_progress', 'progress': 'Running.',
         'blocker': blocker}]})
    assert published['board']['tasks'][0]['blocker'] == blocker


@pytest.mark.parametrize('status, revoked, why', [('completed', False, 'is not running'),
                                              ('running', True, 'is revoked')])
def test_a_row_naming_an_assignment_that_is_not_running_is_refused(board, status, revoked, why):
    progress = board.factory(dispatch=lambda assignment_id: _assignment(
        assignment_id, status=status, revoked=revoked))
    with pytest.raises(ValueError, match='active_dispatch assignment .* ' + why):
        progress.publish(board.scope, {'summary': 'One wait.', 'tasks': [
            {'title': 'Ship the folder fix', 'status': 'pending',
             'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-9'}}]})
    assert board.store.puts == 0


def test_a_row_naming_an_assignment_that_does_not_exist_is_refused(board):
    from lib.project_dispatch.repository import ProjectDispatchError

    def missing(assignment_id):
        raise ProjectDispatchError('Dispatch assignment not found in owned project')

    progress = board.factory(dispatch=missing)
    with pytest.raises(ValueError, match="assignment 'assignment-9' does not exist"):
        progress.publish(board.scope, {'summary': 'One wait.', 'tasks': [
            {'title': 'Ship the folder fix', 'status': 'pending',
             'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-9'}}]})
    assert board.store.puts == 0


@pytest.mark.parametrize('row', [
    {'coordinator': 'other'},
    {'revision': 2},
    {'project': '/other'},
    {'owner': 8},
])
def test_a_row_waiting_on_another_bindings_worker_is_refused(board, row):
    progress = board.factory(dispatch=lambda assignment_id: _assignment(assignment_id, **row))
    with pytest.raises(ValueError, match='belongs to another Coordinator, project or binding revision'):
        progress.publish(board.scope, {'summary': 'One wait.', 'tasks': [
            {'title': 'Ship the folder fix', 'status': 'pending',
             'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-1'}}]})
    assert board.store.puts == 0


def test_an_idle_legacy_row_is_flagged_but_preserved(pending_board):
    from lib.conversations.coordinator_progress import idle_task_keys, task_is_idle
    b = pending_board
    legacy = {'title': b.request['tasks'][2]['title'], 'status': 'pending'}
    # Reading is never refused: the head written before typed blockers still shows.
    state = b.factory().read()
    assert state['board']['tasks'][2] == legacy and task_is_idle(state['board']['tasks'][2])
    assert idle_task_keys(b.store.rows[b.store.head_key]['value']) == [state['taskKeys'][2]]
    # A write that leaves it byte-identical neither deletes nor restatuses it.
    published = b.factory().publish(b.scope, {'changes': [
        {'title': b.request['tasks'][0]['title'], 'progress': 'Still merged.'}]})
    assert published['board']['tasks'][2] == legacy
    # Changing that same row into a new unverifiable wait is a new write: refused.
    with pytest.raises(ValueError, match='pending without a typed blocker'):
        b.factory().publish(b.scope, {'changes': [
            {'title': legacy['title'], 'reason': 'Waiting for a trusted receipt.'}]})
    assert b.factory().read()['board']['tasks'][2] == legacy


@pytest.mark.parametrize('blocker, idle', [
    (None, True),
    ({'kind': 'next_action', 'action': 'Keep waiting'}, True),
    ({'kind': 'next_action', 'action': 'continue'}, True),
    ({'kind': 'next_action', 'action': 'Run the regression suite'}, False),
    ({'kind': 'next_action', 'action': '提炼公众号文章要点'}, False),
    ({'kind': 'blocked_by_dependency', 'assignmentId': 'assignment-2', 'revision': 1}, False),
    ({'kind': 'needs_external_resource', 'resource': 'the snapshot', 'check': 'it opens',
      'ownerAction': 'confirm receipt on the phone'}, False),
    ({'kind': 'needs_external_resource', 'resource': 'the snapshot', 'check': 'it opens',
      'unavailable': True, 'limit': 'the snapshot cannot prove recovery'}, False),
    ({'kind': 'needs_owner_decision', 'questionId': 'turn-7:q1',
      'question': 'May I restart the host?'}, False),
    ({'kind': 'active_dispatch', 'assignmentId': 'assignment-1'}, False),
    ({'kind': 'active_dispatch', 'assignmentId': 'assignment-2'}, True),
])
def test_idle_is_only_a_pending_row_nothing_would_start(blocker, idle):
    from lib.conversations.coordinator_progress import task_is_idle
    task = {'title': 'Recover the snapshot', 'status': 'pending'}
    if blocker:
        task['blocker'] = blocker
    assert task_is_idle(task, {'assignment-1'}) is idle
    assert task_is_idle(dict(task, status='in_progress'), {'assignment-1'}) is False


def test_an_unavailable_resource_is_a_limit_not_a_wait_and_not_idle():
    from lib.conversations.coordinator_progress import task_is_idle, task_is_limit
    limit = {'title': 'Replay the lost month', 'status': 'pending', 'blocker': {
        'kind': 'needs_external_resource', 'resource': 'the deleted archive',
        'check': 'the archive lists the month', 'unavailable': True,
        'limit': 'replay before March stays unverified'}}
    assert task_is_limit(limit) is True
    assert task_is_idle(limit) is False
    actionable = {**limit, 'blocker': {**limit['blocker']}}
    actionable['blocker'].pop('unavailable')
    actionable['blocker'].pop('limit')
    actionable['blocker']['ownerAction'] = 'request the archive from the vendor'
    assert task_is_limit(actionable) is False
    assert task_is_idle(actionable) is False


def test_the_read_projection_flags_parked_pending_rows(pending_board, monkeypatch):
    from lib.conversations import coordinator_work_evidence
    from lib.identity import PrincipalContext
    b = pending_board
    monkeypatch.setattr(coordinator_work_evidence, 'read_evidence', lambda *args, **kwargs: {
        'scope': 'latest_request', 'coverage': 'unknown', 'requestId': 'request-1',
        'sourceTurnId': 'turn-1', 'rows': [
            {'assignmentId': 'assignment-1', 'conversationId': 'worker-1', 'taskKeys': [], 'phase': 'active'}]})
    principal = PrincipalContext(kind='user', subject_id='7', owner_user_id=7, tenant_id=None,
                                 scopes=frozenset({'chat'}))
    progress = b.factory()
    state = progress.read(principal=principal)
    assert state['idleTaskKeys'] == [state['taskKeys'][2]]
    # A row naming the running assignment is not idle; one naming another is.
    head = b.store.rows[b.store.head_key]['value']
    head['board']['tasks'][2]['blocker'] = {'kind': 'active_dispatch', 'assignmentId': 'assignment-1'}
    assert progress.read(principal=principal)['idleTaskKeys'] == []
    head['board']['tasks'][2]['blocker'] = {'kind': 'active_dispatch', 'assignmentId': 'assignment-2'}
    assert progress.read(principal=principal)['idleTaskKeys'] == [state['taskKeys'][2]]


def test_ordinary_in_progress_and_completed_updates_are_unchanged(board):
    progress = board.factory()
    published = progress.publish(board.scope, board.request)
    assert published['board']['tasks'] == board.request['tasks']
    updated = progress.publish(board.scope, {'changes': [
        {'title': board.request['tasks'][1]['title'], 'status': 'completed', 'progress': 'Compared.'}]})
    assert updated['board']['tasks'][1]['status'] == 'completed'
    # A status change clears the pending wait instead of carrying it forward.
    assert 'blocker' not in updated['board']['tasks'][1]


def test_a_pending_checklist_item_is_accepted_and_flagged_idle(board, coordinator_task):
    from lib.conversations.coordinator_progress import task_is_idle
    b, c = board, coordinator_task
    _, _, outcome = c.checklist({'todos': [
        {'content': 'Release 0.19.0', 'status': 'in_progress'},
        {'content': 'Write the release notes', 'status': 'pending'}]})
    assert not outcome['rejected'] and b.store.puts == 1
    pending = b.factory().read()['board']['tasks'][1]
    # todo_write has no blocker field: the row claims no wait, and idle surfaces it.
    assert pending == {'title': 'Write the release notes', 'status': 'pending'} and task_is_idle(pending)
