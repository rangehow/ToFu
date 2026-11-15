"""Read-before-edit evidence survives a restart of its own Turn, and only its own.

RESTART-DRILL-1 (admission-cut-b, 2026-11-13): a Worker read a file, its
``edit_file`` was stopped before it started, the execution host restarted and
the resumed attempt of the SAME Turn repeated the edit. The read gate refused
it ("must read each target file first"): its evidence lived in the attempt
(``toolRounds``, ``messages``) and in the process (freshness tokens), not in
the Turn.

These specs drive the real project handler across a modelled restart: the
Turn's rounds go through the persistence trim and a JSON round trip, the
process-local token store and read-gate cache start empty (a new execution
host), and the next attempt receives the rounds as ``_checkpointToolRounds``,
exactly as ``apply_resume_state`` stashes a checkpoint's durable rounds.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.unit

_CONV = 'conv-turn-evidence'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE', raising=False)
    from lib import write_freshness
    from lib.tasks_pkg.handlers import _read_gate
    write_freshness._reset_for_tests()
    _read_gate._reset_satisfied_cache_for_tests()
    import lib.tasks_pkg.handlers.project as handler
    finalized = []

    def recorder(task, rn, round_entry, results, **kwargs):
        round_entry['results'] = results
        round_entry['status'] = kwargs.get('status', 'done')
        finalized.append(round_entry)

    monkeypatch.setattr(handler, '_finalize_tool_round', recorder)
    yield finalized
    write_freshness._reset_for_tests()
    _read_gate._reset_satisfied_cache_for_tests()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'proj'
    root.mkdir()
    (root / 'notes.txt').write_text('base\n')
    return root


def _attempt(turn_id, checkpoint_rounds=()):
    """One attempt's task: its own rounds, plus the Turn's earlier ones."""
    task = {'id': f'conversation-attempt:{uuid.uuid4()}', 'convId': _CONV,
            '_turnId': turn_id, 'messages': [], 'toolRounds': []}
    if checkpoint_rounds:
        task['_checkpointToolRounds'] = list(checkpoint_rounds)
    return task


def _drive(task, fn_name, fn_args, workspace):
    """Run the real handler on a round this attempt owns, as dispatch does."""
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    number = len(task['toolRounds']) + 1
    call_id = f'tc-{fn_name}-{number}'
    row = {'roundNum': number, 'toolName': fn_name, 'toolCallId': call_id,
           'toolArgs': json.dumps(fn_args), 'query': fn_name, 'status': 'searching'}
    task['toolRounds'].append(row)
    _tc, content, _aborted = _handle_project_tool(
        task, {}, fn_name, call_id, fn_args, number, row, None, str(workspace), True)
    return content, row


def _restart(*attempts):
    """Persist the Turn's rounds and start a new execution host without tokens."""
    from lib import write_freshness
    from lib.task_replay.persistence_projection import trim_tool_round_copy
    from lib.tasks_pkg.handlers import _read_gate
    rows = [trim_tool_round_copy(row) for attempt in attempts
            for row in (*attempt.get('_checkpointToolRounds', ()), *attempt['toolRounds'])]
    durable = json.loads(json.dumps(rows))
    write_freshness._reset_for_tests()
    _read_gate._reset_satisfied_cache_for_tests()
    return durable


_READ = {'reads': [{'path': 'notes.txt'}]}
_EDIT = {'edits': [{'path': 'notes.txt', 'anchor': 'base\n', 'content': 'base\nedited\n'}],
         'atomic': True}


def test_a_read_in_an_earlier_attempt_admits_the_repeated_edit_when_unchanged(workspace):
    first = _attempt('turn-1')
    content, _row = _drive(first, 'read_files', _READ, workspace)
    assert content.startswith('File: '), content

    second = _attempt('turn-1', _restart(first))
    content, row = _drive(second, 'edit_file', _EDIT, workspace)

    assert 'refused' not in content, content
    assert (workspace / 'notes.txt').read_text() == 'base\nedited\n'
    assert 'refusal' not in row['results'][0]


def test_a_file_changed_after_the_restart_is_refused_as_changed_since_read(workspace):
    first = _attempt('turn-1')
    _drive(first, 'read_files', _READ, workspace)
    durable = _restart(first)
    (workspace / 'notes.txt').write_text('base\nsomeone else\n')

    second = _attempt('turn-1', durable)
    content, row = _drive(second, 'edit_file', _EDIT, workspace)

    assert 'file changed since your read; re-read it' in content, content
    assert row['results'][0]['refusal'] == {'kind': 'stale', 'paths': ['notes.txt']}
    assert (workspace / 'notes.txt').read_text() == 'base\nsomeone else\n'


def test_the_turns_own_edit_stays_its_knowledge_across_the_restart(workspace):
    """A successful edit renews the Turn's evidence; it is never 'changed since read'."""
    first = _attempt('turn-1')
    _drive(first, 'read_files', _READ, workspace)
    content, _row = _drive(first, 'edit_file', _EDIT, workspace)
    assert (workspace / 'notes.txt').read_text() == 'base\nedited\n', content

    second = _attempt('turn-1', _restart(first))
    again = {'edits': [{'path': 'notes.txt', 'anchor': 'edited\n', 'content': 'edited twice\n'}],
             'atomic': True}
    content, _row = _drive(second, 'edit_file', again, workspace)

    assert 'refused' not in content, content
    assert (workspace / 'notes.txt').read_text() == 'base\nedited twice\n'


def test_another_turns_read_never_counts(workspace):
    first = _attempt('turn-1')
    _drive(first, 'read_files', _READ, workspace)
    durable = _restart(first)
    assert any(row.get('_fileObservations') for row in durable), (
        'the read must leave durable Turn evidence for this guard to mean anything')

    other_turn = _attempt('turn-2', durable)
    content, row = _drive(other_turn, 'edit_file', _EDIT, workspace)

    assert 'must read each target file first' in content, content
    assert row['results'][0]['refusal']['kind'] == 'read_first'
    assert (workspace / 'notes.txt').read_text() == 'base\n'


def test_same_attempt_behaviour_is_unchanged(workspace):
    attempt = _attempt('turn-1')
    content, row = _drive(attempt, 'edit_file', _EDIT, workspace)
    assert 'must read each target file first' in content, content
    assert row['results'][0]['refusal']['kind'] == 'read_first'

    _drive(attempt, 'read_files', _READ, workspace)
    (workspace / 'notes.txt').write_text('base\nsomeone else\n')
    content, row = _drive(attempt, 'edit_file', _EDIT, workspace)
    assert 'changed on disk' in content, content
    assert row['results'][0]['refusal'] == {'kind': 'stale', 'paths': ['notes.txt']}

    _drive(attempt, 'read_files', _READ, workspace)
    content, _row = _drive(attempt, 'edit_file', _EDIT, workspace)
    assert 'refused' not in content, content
    assert (workspace / 'notes.txt').read_text() == 'base\nedited\nsomeone else\n'


def test_a_streamed_read_stamps_the_turn_evidence_of_the_bytes_it_read(workspace):
    """The drill's read was pre-executed while the model streamed (a prefetch hit)."""
    import hashlib
    import threading
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    task = _attempt('turn-1')
    # No conversation: the streamed Coordinator-policy lookup needs real storage.
    task.update({'convId': '', 'content': '', 'thinking': '', 'config': {}, 'events': [],
                 'content_lock': threading.Lock(), 'events_lock': threading.Lock()})
    accumulator = StreamingToolAccumulator(task, str(workspace), round_num=0,
                                           project_enabled=True)
    accumulator.on_tool_call_ready({'id': 'tc-read', 'type': 'function', 'function': {
        'name': 'read_files', 'arguments': json.dumps({'path': 'notes.txt'})}})
    accumulator.inject_into_cache(task)

    _rn, row = accumulator.announced_tc_map['tc-read']
    observations = row.get('_fileObservations') or []
    assert [(item['path'], item['turnId']) for item in observations] == [
        (str(workspace / 'notes.txt'), 'turn-1')]
    assert observations[0]['fingerprint'][2] == hashlib.sha256(b'base\n').hexdigest()
