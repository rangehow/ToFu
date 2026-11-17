"""Incremental request building is byte-identical to a full rebuild.

Contract: ``docs/CONTEXT_INJECTIONS.md`` ("Incremental request sealing") and
``lib/llm/request.py`` ``detach_input``. A task's provider request for round
N+1 reuses the settled messages of round N (translation, sealed encodes,
content digest, lineage span hashes, recount and transport diagnostics are
memoized per settled row). Every piece of request evidence must equal what a
fresh full rebuild of the same canonical body produces: the sealed payload,
semantic/provider-prefix identities, admitted tokens, sealed context lineage,
transport fingerprints, the durable raw-archive request bytes and the Request
Inspector snapshot. The incremental arm keeps one task across all rounds; the
reference arm builds every round on a fresh task with no reusable rows.

Histories are long and real-shaped (system policy carriers, chronological
injections, reasoning with signatures, native Anthropic blocks, images,
rejected tool-call IDs) and change the way the root loop changes them:
appended tool rounds and user Turns, compaction of a prefix, an injection
inserted mid-history, an in-place edit of an old tool result and a changed
system prompt.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from lib.llm.body import build_body
from lib.llm._sse_core import prepare_request

pytestmark = pytest.mark.unit

TOOLS_SPEC = ('run_command', 'read_files', 'write_file')


def _tools():
    from lib.conversation_sync.settled_rows import settle
    return list(settle([
        {'type': 'function', 'function': {
            'name': name, 'description': f'{name} tool',
            'parameters': {'type': 'object', 'properties': {
                'command': {'type': 'string'}}, 'required': ['command']}}}
        for name in TOOLS_SPEC]))


TOOLS = _tools()
PNG = 'data:image/png;base64,' + 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='


def _initial(system_text='You are a coding agent. ' * 50):
    from lib.tasks_pkg.context_composer import append_injection
    messages = [{'role': 'system', 'content': system_text}]
    append_injection(messages, 'worker_policy', 'Worker policy text. ' * 20)
    messages.append({'role': 'user', 'content': [
        {'type': 'text', 'text': 'Look at this screenshot and run the command.'},
        {'type': 'image_url', 'image_url': {'url': PNG}}]})
    return messages


def _tool_round(messages, index, *, native):
    # Every seventh call carries an ID the Anthropic contract rejects.
    call_id = f'watchdog_snapshot:{index}' if index % 7 == 3 else f'call_{index:04d}'
    assistant = {'role': 'assistant', 'content': f'Running step {index}.',
                 'reasoning_content': f'Reasoning about step {index}.',
                 'thinking_signature': f'sig-{index}-' + 'x' * 40,
                 '_reasoning_continuity': 'completed_turn',
                 'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                     'name': 'run_command',
                     'arguments': json.dumps({'command': f'echo {index}'})}}]}
    if native and index % 9 == 5:
        assistant['_anthropic_content_blocks'] = [
            {'type': 'thinking', 'thinking': f'Native thought {index}.', 'signature': f'nsig{index}'},
            {'type': 'text', 'text': f'Running step {index}.'},
            {'type': 'tool_use', 'id': call_id, 'name': 'run_command',
             'input': {'command': f'echo {index}'}}]
    messages.append(assistant)
    messages.append({'role': 'tool', 'tool_call_id': call_id, 'name': 'run_command',
                     'content': f'$ echo {index}\n{index}\n[exit code: 0]'})


def _user_turn(messages, index):
    from lib.tasks_pkg.context_composer import append_injection
    messages.append({'role': 'assistant', 'content': f'Finished step {index}.'})
    messages.append({'role': 'user', 'content': f'Continue with step {index + 1}.'})
    append_injection(messages, 'tool_progress', f'progress note {index}')


def _attach(body, task, *, incremental, round_num):
    from lib.llm.anthropic_outbound._tool_inputs import ANTHROPIC_TOOL_INPUTS_KEY, ToolInputs
    from lib.llm.request import WIRE_MESSAGE_ROWS_KEY
    from lib.tasks_pkg.compaction._tokens import _task_text_count_memo
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY
    body[TEXT_COUNT_VIEW_KEY] = _task_text_count_memo(task).view('provider')
    body[ANTHROPIC_TOOL_INPUTS_KEY] = task.setdefault('_anthropicToolInputs', ToolInputs())
    body['_wire_tool_rows'] = task.setdefault('_wireToolRows', {})
    if incremental:
        body[WIRE_MESSAGE_ROWS_KEY] = task.setdefault('_wireMessageRows', {})
    body['_task_id'] = task['id']
    body['_conv_id'] = task['convId']
    body['_raw_archive_context'] = {
        'userId': 1, 'conversationId': task['convId'], 'turnId': 'turn-1',
        'attemptId': 'attempt-1', 'taskId': task['id'], 'roundNum': round_num + 1,
        'model': body['model']}


def _evidence(messages, task, *, protocol, model, incremental, round_num):
    from lib.tasks_pkg.compaction._tokens import task_round_text_identities
    from lib.tasks_pkg.round_evidence import build_round_evidence
    body = build_body(model, messages, max_tokens=4096, provider_id='bench',
                      precomputed_input_tokens=1000, context_window=200000, tools=TOOLS)
    snapshot = build_round_evidence(
        task, messages, ref={'roundNum': round_num + 1, 'turn': 't'}, kind='request',
        model=model, prepared_messages=body['messages'],
        prepared_lineage=body.get('_context_lineage'), tools=TOOLS)
    _attach(body, task, incremental=incremental, round_num=round_num)
    with task_round_text_identities(task):
        plan = prepare_request(body, api_protocol=protocol, api_key='k',
                               base_url='https://bench.invalid/v1')
    sealed = plan.body
    lineage = sealed.context_diagnostics()
    if lineage is not None:
        lineage.pop('traceId', None)
    dumper = plan.raw_dumper
    capture = plan.raw_archive_capture
    if capture is not None:
        capture.discard()
    return {
        'payload': hashlib.sha256(sealed.payload).hexdigest(),
        'identity': sealed.diagnostics(),
        'local_tokens': sealed.local_tokens,
        'lineage': lineage,
        'usage_evidence': {key: value for key, value in sealed.usage_evidence('t').items()},
        'wire': {name: getattr(dumper, name, None) for name in (
            'wire_fp', 'wire_static', 'wire_system', 'wire_markers', 'wire_bytes',
            'wire_field_bytes', 'wire_region', 'wire_controls', 'wire_tool_surface',
            'context_lineage')},
        'raw_archive': (hashlib.sha256(capture.request_bytes).hexdigest()
                        if capture is not None else None),
        'snapshot': json.dumps({key: snapshot[key] for key in ('messages', 'contextLineage')
                                if key in snapshot}, sort_keys=True),
        'hdrs': {key: value for key, value in plan.hdrs.items() if key != 'M-TraceId'},
    }


def _strip_trace(evidence):
    if evidence['wire']['context_lineage']:
        evidence['wire']['context_lineage'] = {
            key: value for key, value in evidence['wire']['context_lineage'].items()
            if key != 'traceId'}
    return evidence


def _changes(messages, index):
    """The root loop's ways of changing history, at fixed rounds."""
    from lib.tasks_pkg.context_composer import append_injection
    if index == 25:  # compaction: a summary replaces an old prefix
        del messages[3:30]
        messages.insert(3, {'role': 'user', 'content': '[Compacted summary of earlier work]'})
    elif index == 33:  # an injection lands in the middle of history
        injected = []
        append_injection(injected, 'tool_progress', 'late mid-history note')
        messages[len(messages) // 2:len(messages) // 2] = injected
    elif index == 41:  # micro-compaction edits an old tool result in place
        old = next(m for m in messages[5:] if m['role'] == 'tool')
        old['content'] = '[result elided]'
    elif index == 47:  # the system prompt changes (another digest)
        messages[0] = {'role': 'system', 'content': 'You are a careful coding agent. ' * 50}


@pytest.mark.parametrize('protocol,model', [
    ('anthropic', 'claude-sonnet-4'),
    ('openai', 'claude-sonnet-4'),
    ('openai', 'gpt-4o'),
    ('responses', 'gpt-4o'),
])
def test_incremental_request_evidence_equals_full_rebuild(protocol, model):
    messages = _initial()
    task = {'id': 'incremental-task', 'convId': 'conv-incremental', '_userId': 1}
    for index in range(56):
        incremental = _strip_trace(_evidence(
            messages, task, protocol=protocol, model=model, incremental=True,
            round_num=index))
        fresh_task = {'id': 'incremental-task', 'convId': 'conv-incremental', '_userId': 1}
        reference = _strip_trace(_evidence(
            copy.deepcopy(messages), fresh_task, protocol=protocol, model=model,
            incremental=False, round_num=index))
        for key in reference:
            assert incremental[key] == reference[key], (index, key)
        _tool_round(messages, index, native=protocol == 'anthropic')
        if index % 6 == 5:
            _user_turn(messages, index)
        _changes(messages, index)
    rows = task['_wireMessageRows']['messages']
    from lib.conversation_sync.settled_rows import is_settled
    assert all(map(is_settled, rows))


def test_unchanged_history_keeps_its_settled_rows_and_changes_replace_them():
    from lib.llm.request import WIRE_MESSAGE_ROWS_KEY, detach_input
    holder = {}
    first = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]
    body = {'messages': copy.deepcopy(first), WIRE_MESSAGE_ROWS_KEY: holder}
    detach_input(body)
    kept = body['messages']
    body = {'messages': copy.deepcopy(first) + [{'role': 'user', 'content': 'c'}], WIRE_MESSAGE_ROWS_KEY: holder}
    detach_input(body)
    assert body['messages'][0] is kept[0] and body['messages'][1] is kept[1]
    edited = copy.deepcopy(first)
    edited[1]['content'] = 'B'
    body = {'messages': edited, WIRE_MESSAGE_ROWS_KEY: holder}
    detach_input(body)
    assert body['messages'][0] is kept[0] and body['messages'][1] is not kept[1]
    # Interchangeable means byte-identical JSON: True is not 1, key order counts.
    body = {'messages': [{'role': 'user', 'content': 'a', 'n': True}], WIRE_MESSAGE_ROWS_KEY: holder}
    detach_input(body)
    one = {'messages': [{'role': 'user', 'content': 'a', 'n': 1}], WIRE_MESSAGE_ROWS_KEY: holder}
    detach_input(one)
    assert one['messages'][0] is not body['messages'][0]
    assert json.dumps(one['messages']) == '[{"role": "user", "content": "a", "n": 1}]'
    reordered = {'messages': [{'content': 'a', 'role': 'user', 'n': 1}], WIRE_MESSAGE_ROWS_KEY: holder}
    detach_input(reordered)
    assert list(reordered['messages'][0]) == ['content', 'role', 'n']
