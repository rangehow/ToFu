"""Executable spec for the Amazon Q ``todo_list`` → ``todo_write`` compat layer.

Amazon Q Developer CLI's built-in checklist tool is named ``todo_list`` and
speaks a command vocabulary — ``{command: create|complete|load|add|remove|
lookup, tasks?, completed_indices?, new_tasks?, remove_indices?,
insert_indices?, todo_list_description?}`` (verified in
aws/amazon-q-developer-cli ``crates/chat-cli/src/cli/chat/tools/todo.rs``).
Three layers cooperate to serve it:

1. name alias — ``lib/tool_input_repair/_classify.py`` maps ``todo_list``
   onto ``todo_write``;
2. command-shape transform — ``lib/tool_input_repair/_transform.py`` injects
   the schema-required ``todos`` placeholder so contract validation passes;
3. stateful command interpreter — ``lib/tools/todo.apply_todo_operation``
   merges the command against the live checklist (the layers above are
   stateless and cannot).

This file pins layer 3 plus task-level integration; layers 1–2 have their
own specs in ``test_tool_input_repair.py`` / ``test_unified_tool_gateway.py``.
"""

import pytest

from lib.tools.todo import (
    TODO_MAX_ITEMS,
    apply_todo_operation,
    apply_todo_write_to_task,
)

pytestmark = pytest.mark.unit


def _create(state, tasks):
    return apply_todo_operation(state, {'command': 'create', 'tasks': tasks})


def test_create_builds_list_with_sequential_ids():
    out = _create(None, ['scan repo', 'patch', 'test'])
    assert out['rejected'] is False and out['no_op'] is False
    assert out['operation'] == 'sync'
    assert out['todos'] == [
        {'id': '1', 'content': 'scan repo', 'status': 'pending'},
        {'id': '2', 'content': 'patch', 'status': 'pending'},
        {'id': '3', 'content': 'test', 'status': 'pending'},
    ]


def test_create_identical_list_is_idempotent_noop():
    state = _create(None, ['a', 'b'])['state']
    out = _create(state, ['a', 'b'])
    assert out['rejected'] is False and out['no_op'] is True


def test_create_replacing_active_list_replans_with_description_reason():
    state = _create(None, ['old plan'])['state']
    out = apply_todo_operation(state, {
        'command': 'create', 'tasks': ['new plan'],
        'todo_list_description': 'pivot after scan'})
    assert out['operation'] == 'replan' and out['rejected'] is False
    assert [t['content'] for t in out['todos']] == ['new plan']
    history = out['state']['history']
    assert history[-1]['kind'] == 'replan'
    assert history[-1]['reason'] == 'pivot after scan'


def test_create_replacing_active_list_synthesizes_reason_when_absent():
    state = _create(None, ['old plan'])['state']
    out = _create(state, ['new plan'])
    assert out['operation'] == 'replan' and out['rejected'] is False
    assert out['state']['history'][-1]['reason']


def test_create_without_tasks_is_a_rejection_receipt():
    state = _create(None, ['keep me'])['state']
    out = apply_todo_operation(state, {'command': 'create', 'tasks': []})
    assert out['rejected'] is True
    assert 'tasks' in out['reason']
    assert [t['content'] for t in out['todos']] == ['keep me']


def test_create_over_max_items_hits_the_canonical_guard():
    out = _create(None, [f'task {i}' for i in range(TODO_MAX_ITEMS + 1)])
    assert out['rejected'] is True
    assert 'at most' in out['reason']


def test_create_tolerates_dict_task_items():
    out = _create(None, ['plain', {'description': 'from dict'}, {'nope': 1}, 42])
    assert [t['content'] for t in out['todos']] == ['plain', 'from dict']


def test_complete_marks_by_index_and_ignores_out_of_range():
    state = _create(None, ['a', 'b', 'c'])['state']
    out = apply_todo_operation(
        state, {'command': 'complete', 'completed_indices': [0, '2', 9, -1]})
    assert out['rejected'] is False
    assert [t['status'] for t in out['todos']] == [
        'completed', 'pending', 'completed']


def test_complete_without_active_list_is_a_rejection_receipt():
    out = apply_todo_operation(
        None, {'command': 'complete', 'completed_indices': [0]})
    assert out['rejected'] is True
    assert 'no active checklist' in out['reason']


def test_complete_everything_marks_root_completed():
    state = _create(None, ['only'])['state']
    out = apply_todo_operation(
        state, {'command': 'complete', 'completed_indices': [0]})
    assert out['rejected'] is False
    assert out['state']['root_completed'] is True


def test_add_inserts_at_positions_with_fresh_ids():
    state = _create(None, ['a', 'b'])['state']
    out = apply_todo_operation(state, {
        'command': 'add', 'new_tasks': ['x', 'y'], 'insert_indices': [0, 2]})
    assert out['rejected'] is False
    assert [(t['id'], t['content']) for t in out['todos']] == [
        ('3', 'x'), ('1', 'a'), ('4', 'y'), ('2', 'b')]


def test_add_appends_when_positions_missing():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(state, {'command': 'add', 'new_tasks': ['b']})
    assert [t['content'] for t in out['todos']] == ['a', 'b']


def test_add_to_empty_state_acts_as_create():
    out = apply_todo_operation(
        None, {'command': 'add', 'new_tasks': ['first']})
    assert out['rejected'] is False
    assert [t['content'] for t in out['todos']] == ['first']


def test_add_after_root_completion_replans_and_reopens():
    state = _create(None, ['done'])['state']
    state = apply_todo_operation(
        state, {'command': 'complete', 'completed_indices': [0]})['state']
    assert state['root_completed'] is True
    out = apply_todo_operation(state, {'command': 'add', 'new_tasks': ['more']})
    assert out['operation'] == 'replan' and out['rejected'] is False
    assert out['state']['root_completed'] is False
    assert [t['content'] for t in out['todos']] == ['done', 'more']


def test_remove_deletes_by_index_via_replan():
    state = _create(None, ['a', 'b', 'c'])['state']
    out = apply_todo_operation(
        state, {'command': 'remove', 'remove_indices': [0, 2]})
    assert out['operation'] == 'replan' and out['rejected'] is False
    assert [t['content'] for t in out['todos']] == ['b']
    assert out['state']['history'][-1]['kind'] == 'replan'


def test_remove_without_matching_index_is_a_rejection_receipt():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(
        state, {'command': 'remove', 'remove_indices': [7]})
    assert out['rejected'] is True
    assert [t['content'] for t in out['todos']] == ['a']


def test_remove_everything_empties_the_checklist():
    state = _create(None, ['a', 'b'])['state']
    out = apply_todo_operation(
        state, {'command': 'remove', 'remove_indices': [0, 1]})
    assert out['rejected'] is False
    assert out['todos'] == []
    assert out['state']['root_completed'] is True


def test_load_and_lookup_are_read_only_and_report_current_list():
    state = _create(None, ['a', 'b'])['state']
    revision = state['stack'][-1]['revision']
    for command in ('load', 'lookup'):
        out = apply_todo_operation(state, {'command': command})
        assert out['rejected'] is False and out['no_op'] is True
        assert out['todos'] == state['stack'][-1]['todos']
        assert out['note'].startswith(f'todo_list {command}:')
        assert out['state']['stack'][-1]['revision'] == revision


def test_unknown_command_falls_through_to_native_protocol():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(state, {'command': 'restart'})
    # No command translation: a bare payload is native sync with an empty
    # list, which the canonical machine honestly rejects.
    assert out['rejected'] is True
    assert 'sync cannot remove unfinished items' in out['reason']


def test_todos_array_wins_over_command():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(state, {
        'command': 'complete',
        'todos': [{'id': '1', 'content': 'a', 'status': 'completed'}]})
    assert out['rejected'] is False
    assert out['todos'][0]['status'] == 'completed'


def test_task_level_round_trip_updates_mirror_and_receipt_text():
    task = {'_todos': [], '_todoState': None}
    todos, text, outcome = apply_todo_write_to_task(
        task, {'command': 'create', 'tasks': ['a', 'b']})
    assert task['_todos'] == todos
    assert '0/2 completed' in text
    todos, text, outcome = apply_todo_write_to_task(
        task, {'command': 'complete', 'completed_indices': [0]})
    assert task['_todos'][0]['status'] == 'completed'
    assert '1/2 completed' in text
    _, text, outcome = apply_todo_write_to_task(task, {'command': 'load'})
    assert outcome['no_op'] is True
    assert text.startswith('todo_list load:')


def test_create_spec_is_genuine(monkeypatch):
    """Neutralization: bypass the command interpreter and the same payload
    must NOT build a checklist — proving the spec exercises the layer."""
    from lib.tools import todo as todo_mod
    monkeypatch.setattr(todo_mod, '_todo_list_command_name', lambda args: '')
    out = todo_mod.apply_todo_operation(
        None, {'command': 'create', 'tasks': ['a', 'b']})
    assert out['todos'] == []
