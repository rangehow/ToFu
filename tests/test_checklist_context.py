"""Authoritative checklist identity reaches the measured, provider-bound request."""

from __future__ import annotations

import copy
import json

import pytest

from lib.llm_errors import PromptTooLongError
from lib.tasks_pkg.compaction import _prompt_admission as admission
from lib.tools import todo

pytestmark = pytest.mark.unit


def _task(items=None):
    items = items or [
        {'id': 'inspect', 'content': 'Inspect the failure', 'status': 'completed'},
        {'id': 'repair', 'content': 'Repair the context boundary', 'status': 'in_progress'},
        {'id': 'verify', 'content': 'Verify the regression', 'status': 'pending'},
    ]
    task = {'id': 'checklist-context', '_userId': 7, 'config': {'model': 'test-model'}}
    todo.apply_todo_write_to_task(task, {'todos': items})
    return task


def _text(messages):
    return '\n'.join(
        message['content'] if isinstance(message.get('content'), str) else
        '\n'.join(block.get('text', '') for block in message.get('content') or []
                  if isinstance(block, dict))
        for message in messages
    )


def _assert_current(messages, task):
    assert todo.render_todo_list(task['_todos']) in _text(messages)


def _admit(messages, task, round_num=0):
    return admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=round_num, model='test-model')


def _measure_chars(monkeypatch, ceiling=100_000):
    monkeypatch.setattr(admission, '_resolved_ceiling',
                        lambda *a, **k: (ceiling, ceiling))
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda messages, *a, **k: (len(_text(messages)), 'test_chars'))


def test_round_zero_admission_restores_ids_before_measurement(monkeypatch):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue the interrupted repair.'}]
    original = copy.deepcopy(messages)
    state = copy.deepcopy(task['_todoState'])
    _measure_chars(monkeypatch)

    measured = _admit(messages, task)

    _assert_current(messages, task)
    assert measured['totalTokens'] == len(_text(messages))
    assert messages[:len(original)] == original
    assert task['_todoState'] == state


@pytest.mark.parametrize('compacted', [True, False])
def test_final_summary_cannot_remove_ids_before_the_same_dispatch(monkeypatch, compacted):
    task = _task()
    messages = [{'role': 'user', 'content': 'x' * 20_000}]
    _measure_chars(monkeypatch, ceiling=10_000)
    # Other provider recomposition is irrelevant to this required task block.
    monkeypatch.setattr(admission, 'recompose_context_after_compaction', lambda *a, **k: None)

    def summarize(working, **kwargs):
        _assert_current(working, task)
        working[:] = [{'role': 'user', 'content': 'Summary omitted all checklist calls.'}]
        return compacted

    monkeypatch.setattr(admission, 'force_compact_if_needed', summarize)
    measured = _admit(messages, task, round_num=272)

    _assert_current(messages, task)
    assert measured['totalTokens'] == len(_text(messages))
    from lib.llm.body import build_body
    _assert_current(build_body('test-model', messages, max_tokens=2048)['messages'], task)
    # Advance with the identities actually supplied on this request, not an
    # invented replacement followed by an unchanged repair submission.
    updated = copy.deepcopy(task['_todos'])
    updated[1]['status'] = 'completed'
    updated[2]['status'] = 'in_progress'
    _, _, outcome = todo.apply_todo_write_to_task(task, {'todos': updated})
    assert not outcome['rejected'] and not outcome['no_op']
    assert task['_todoState']['update_count'] == 2


def test_required_checklist_overflow_refuses_instead_of_dispatching_partial_state(monkeypatch):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _measure_chars(monkeypatch, ceiling=20)
    monkeypatch.setattr(admission, 'force_compact_if_needed', lambda *a, **k: False)

    with pytest.raises(PromptTooLongError):
        _admit(messages, task)

    _assert_current(messages, task)
    assert task['_lastPromptAdmission']['totalTokens'] == len(_text(messages))


def _restore(messages, task):
    from lib.tasks_pkg.context_composer.checklist import restore_checklist_context
    restore_checklist_context(messages, task=task, model='test-model')


def test_unchanged_state_preserves_cache_prefix_and_ignores_quoted_headings():
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    messages.extend([
        {'role': 'assistant', 'tool_calls': [{'id': 'read', 'function': {
            'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read',
         'content': 'Source example: ## Active Task Checklist\n<!-- chatui-todo-state:v1:wrong -->'},
    ])
    frozen = copy.deepcopy(messages)
    for _ in range(12):
        _restore(messages, task)
    assert messages == frozen
    assert len([row for row in task['_contextManifest'] if row.get('appended')]) == 1


@pytest.mark.parametrize('damage', ['tail_missing', 'status_changed', 'id_missing'])
def test_surviving_marker_does_not_prove_a_complete_current_carrier(damage):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    block = messages[-1]['content'][0]
    if damage == 'tail_missing':
        block['text'] = block['text'].split('- [ ] Verify')[0]
    elif damage == 'status_changed':
        block['text'] = block['text'].replace('Repair the context boundary ⏳', 'Repair the context boundary [blocked]')
    else:
        block['text'] = block['text'].replace(' · id="verify"', '')
    prefix = copy.deepcopy(messages)

    _restore(messages, task)

    assert messages[:len(prefix)] == prefix
    assert len(messages) == len(prefix) + 1
    _assert_current(messages[-1:], task)


def test_versioned_call_does_not_substitute_for_accepted_runtime_revision():
    task = _task()
    messages = [
        {'role': 'assistant', 'tool_calls': [{'id': 'plan', 'function': {
            'name': 'todo_write', 'arguments': json.dumps({'todos': task['_todos']})}}]},
        {'role': 'tool', 'tool_call_id': 'plan', 'content': todo.render_todo_list(task['_todos'])},
    ]
    frozen = copy.deepcopy(messages)
    _restore(messages, task)
    _assert_current(messages[-1:], task)
    assert 'revision 1' in _text(messages[-1:])
    assert messages[:len(frozen)] == frozen


def test_implicit_ids_in_call_cannot_mask_runtime_synthesized_ids():
    task = _task([{'content': 'Keep identity', 'status': 'in_progress'}])
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'plan', 'function': {
        'name': 'todo_write', 'arguments': json.dumps({'todos': [
            {'content': 'Keep identity', 'status': 'in_progress'}]})}}]}]
    _restore(messages, task)
    _assert_current(messages, task)


def test_nested_restore_preserves_active_ids_and_parent_identity_without_history_bulk():
    task = _task()
    todo.apply_todo_write_to_task(task, {'operation': 'push', 'parent_todo_id': 'repair',
        'todos': [{'id': 'child', 'content': 'Patch one boundary', 'status': 'in_progress'}]})
    task['_todoState']['history'].append({'reason': 'historical bulk must not be rendered'})
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    visible = _text(messages)
    _assert_current(messages, task)
    assert 'Checklist path: Root task > Repair the context boundary' in visible
    assert 'Parent item ID: "repair"' in visible
    assert 'revision 1, depth 2' in visible
    assert 'historical bulk' not in visible
    frozen = copy.deepcopy(messages)
    _restore(messages, task)
    assert messages == frozen


def test_maximum_active_list_is_complete_even_under_tiny_optional_context_budget():
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block

    items = [{'id': str(i).ljust(todo.TODO_MAX_ID_CHARS, '\U0001f9ea'),
              'content': '\U0001f9ea' * todo.TODO_MAX_CONTENT_CHARS,
              'status': 'pending'} for i in range(todo.TODO_MAX_ITEMS)]
    task = _task(items)
    for _ in range(todo.TODO_MAX_STACK_DEPTH - 1):
        _, _, outcome = todo.apply_todo_write_to_task(task, {
            'operation': 'push', 'parent_todo_id': items[0]['id'], 'todos': items})
        assert not outcome['rejected']
    messages = [{'role': 'user', 'content': 'Continue.'}]
    block = build_checklist_context_block(messages, task)
    result = render_context(messages, [block], ComposeRequest(
        task=task, model='test-model', global_budget_tokens=1))

    _assert_current(messages, task)
    assert not result.plan.entries[0].truncated
    assert result.plan.overflow_tokens > 0
    assert len(_text(messages).encode('utf-8')) < 80 * 1024


def test_progress_refresh_appends_once_without_rewriting_old_revision():
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    frozen = copy.deepcopy(messages)
    updated = copy.deepcopy(task['_todos'])
    updated[1]['status'] = 'completed'
    updated[2]['status'] = 'blocked'
    todo.apply_todo_write_to_task(task, {'todos': updated})
    _restore(messages, task)
    _assert_current(messages[-1:], task)
    assert messages[:len(frozen)] == frozen
    assert len(messages) == len(frozen) + 1
    _restore(messages, task)
    assert len(messages) == len(frozen) + 1


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_composition_preserves_state_independent_of_optional_guidance_and_tool_availability(mode):
    from lib.tasks_pkg.context_composer import compose_task_context

    task = _task()
    messages = [{'role': 'system', 'content': 'Operator-owned policy.'},
                {'role': 'user', 'content': 'Continue.'}]
    compose_task_context(messages, user_id=7, task=task, model='test-model',
        has_real_tools=False, tool_names=set(), system_prompt_mode=mode,
        disabled_blocks={'active_checklist', 'todo', 'task_state_v1'})
    _assert_current(messages, task)
    assert messages[0]['content'] == 'Operator-owned policy.'
    assert 'Use only currently available tools' in _text(messages)
    assert task['_todoState']['update_count'] == 1


def test_owner_isolation_and_no_state_do_not_read_another_task():
    first = _task([{'id': 'owner-a', 'content': 'Private task A', 'status': 'pending'}])
    second = _task([{'id': 'owner-b', 'content': 'Private task B', 'status': 'pending'}])
    messages_a, messages_b = [], []
    _restore(messages_a, first)
    _restore(messages_b, second)
    assert 'Private task B' not in _text(messages_a)
    assert 'Private task A' not in _text(messages_b)
    empty = [{'role': 'user', 'content': 'No checklist.'}]
    frozen = copy.deepcopy(empty)
    _restore(empty, {'_userId': 9})
    assert empty == frozen


def test_optional_file_attachment_cannot_truncate_required_list():
    from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments

    task = _task([{'id': str(i), 'content': '验证' * 256, 'status': 'pending'}
                  for i in range(todo.TODO_MAX_ITEMS)])
    messages = [{'role': 'user', 'content': 'Continue.'}]
    attachments = ['<system-reminder>\n## Recently Modified Files\n' + 'file.py\n' * 4000 + '</system-reminder>']
    attachments += compute_turn_attachments(messages, task, round_num=1, conv_id='')
    inject_attachments(messages, attachments, task=task, model='test-model')
    _assert_current(messages, task)
    assert not compute_turn_attachments(messages, task, round_num=2, conv_id='')


def test_continue_resume_and_final_body_receive_all_current_ids(monkeypatch):
    import threading
    from lib.llm.body import build_body
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    checkpoint = _task()['_todoState']
    task = {'id': 'resume', '_userId': 7, 'content': '',
            'content_lock': threading.Lock(), 'config': {'model': 'test-model'}}
    messages = [{'role': 'user', 'content': 'Continue from summary without tool calls.'}]
    apply_resume_state(task=task, cfg={'checkpointTodoState': checkpoint},
                       messages=messages, model='test-model', tid='resume')
    _measure_chars(monkeypatch)
    measurement = _admit(messages, task, round_num=0)
    body = build_body('test-model', messages, max_tokens=2048,
                      precomputed_input_tokens=measurement['totalTokens'])
    _assert_current(body['messages'], task)
    assert task['_todoState'] == checkpoint
    assert all('_contextComposer' not in row for row in body['messages'])


@pytest.mark.parametrize('memory_pressure', [False, True])
@pytest.mark.parametrize('compacted', [False, True])
def test_reactive_retry_restores_state_before_building_the_same_model_body(monkeypatch, memory_pressure, compacted):
    from lib.cgroup_guard import MemoryPressureError
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.llm_fallback import _call as fallback

    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    state = copy.deepcopy(task['_todoState'])
    observed = []

    def recover(working, **kwargs):
        observed.append(kwargs.get('byte_target'))
        working[:] = [{'role': 'user', 'content': 'Compacted without checklist.'}]
        return compacted

    monkeypatch.setattr(compaction, 'reactive_compact', recover)
    cause = MemoryPressureError('memory headroom') if memory_pressure else PromptTooLongError('too long')
    body = fallback._prepare_reactive_retry_body(
        task, {'temperature': 0.3}, 'test-model', messages, [], 'medium', False,
        cause, 'test')
    assert body is not None
    _assert_current(body['messages'], task)
    assert body['model'] == 'test-model'
    assert body['_task_id'] == task['id']
    assert task['_todoState'] == state
    assert bool(observed[0]) is memory_pressure


def test_reactive_restoration_failure_never_builds_an_incomplete_body(monkeypatch):
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.context_composer import checklist
    from lib.tasks_pkg.llm_fallback import _call as fallback

    monkeypatch.setattr(compaction, 'reactive_compact', lambda *a, **k: True)

    def broken(*args, **kwargs):
        raise ValueError('invalid state')

    monkeypatch.setattr(checklist, 'restore_checklist_context', broken)
    monkeypatch.setattr(fallback, 'build_body', lambda *a, **k: pytest.fail('must not build without state'))
    cause = PromptTooLongError('original provider limit')
    assert fallback._prepare_reactive_retry_body(
        _task(), {}, 'test-model', [], [], 'medium', False, cause, 'test') is None


def test_cleared_versioned_state_replaces_stale_active_carrier_without_rewriting_history():
    task = _task()
    messages = [{'role': 'user', 'content': 'Change the plan.'}]
    _restore(messages, task)
    frozen = copy.deepcopy(messages)
    _, _, outcome = todo.apply_todo_write_to_task(task, {
        'operation': 'replan', 'reason': 'The user cancelled this scope.', 'todos': []})
    assert not outcome['rejected']
    _restore(messages, task)
    assert 'No active checklist items.' in _text(messages[-1:])
    assert messages[:len(frozen)] == frozen
    assert len(messages) == len(frozen) + 1


@pytest.mark.parametrize('profile', ['default', 'codex'])
def test_gpt56_responses_wire_preserves_complete_checklist_and_does_not_grant_tools(profile):
    from lib.llm.body import build_body
    from lib.llm.responses_outbound._to_responses import openai_body_to_responses

    task = _task()
    messages = [{'role': 'user', 'content': 'Continue the authorized work.'}]
    _restore(messages, task)
    body = build_body('gpt-5.6-sol', messages, max_tokens=2048, tools=[])
    wire, _ = openai_body_to_responses(body, profile=profile)
    _assert_current(wire['input'], task)
    assert not wire.get('tools')
    assert all('_contextComposer' not in row for row in wire['input'])


def test_reactive_restored_checklist_is_counted_by_final_transport_limit(monkeypatch):
    from lib.llm.body._admission import enforce_body_input_limit
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.compaction import _tokens
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from lib.token_counter import count_tokens

    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    monkeypatch.setattr(compaction, 'reactive_compact', lambda *a, **k: False)
    body = fallback._prepare_reactive_retry_body(
        task, {}, 'test-model', messages, [], 'medium', False,
        PromptTooLongError('too long'), 'test')
    expected = count_tokens(body['messages'], model='test-model',
                            tools=body.get('tools'), mode='tiktoken')['tokens']
    assert enforce_body_input_limit(body, model='test-model') == expected
    assert expected > 1
    monkeypatch.setattr(_tokens, 'resolve_model_context_limit', lambda *a, **k: 1)
    with pytest.raises(PromptTooLongError):
        enforce_body_input_limit(body, model='test-model')
    _assert_current(body['messages'], task)
