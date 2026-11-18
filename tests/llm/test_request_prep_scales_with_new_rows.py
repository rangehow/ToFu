"""Per-round Anthropic translation and token recount visit only new rows.

Contract: ``docs/HARNESS_PERFORMANCE.md`` ("Round floor V") and
``lib/conversation_sync/settled_rows.py`` (``settled_prefix``). Translated
rows and their token counts are pure functions of the settled row and the
translation settings, so a round resumes after the leading run of rows the
previous round already handled: translation, the canonical admission count,
the Anthropic recount projection and its count visit only the rows that are
new or were replaced (an edit, compaction, a moved cache marker). The work
per round is therefore the same for a 1-, 10- or 100-row history, and every
round's sealed request equals a fresh preparation that holds nothing.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.body import build_body

pytestmark = pytest.mark.unit

TOOLS = [{'type': 'function', 'function': {
    'name': 'run_command', 'description': 'Run a command.',
    'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}}]


def _pair(index):
    call_id = f'call_{index:05d}'
    return [
        {'role': 'assistant', 'content': f'Running step {index}.',
         'reasoning_content': f'Reasoning about step {index}.',
         'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
             'name': 'run_command', 'arguments': json.dumps({'command': f'echo {index}'})}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'content': f'{index}\n[exit code: 0]'},
    ]


def _history(rows):
    messages = [{'role': 'user', 'content': 'Do the task.'}]
    if rows > 1:
        messages.insert(0, {'role': 'system', 'content': 'You are a coding agent. ' * 20})
    index = 0
    while len(messages) < rows:
        messages.extend(_pair(index))
        index += 1
    return messages[:rows]


def _new_task():
    return {'id': 'scale-task', 'convId': 'conv-scale', '_userId': 1}


def _prepare(messages, task, *, model='claude-sonnet-4'):
    from lib.conversation_sync.settled_rows import settle_items
    from lib.llm.anthropic_outbound._tool_inputs import ANTHROPIC_TOOL_INPUTS_KEY, ToolInputs
    from lib.llm.request import WIRE_MESSAGE_ROWS_KEY
    from lib.tasks_pkg.compaction._tokens import _task_text_count_memo
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY
    body = build_body(model, copy.deepcopy(messages), max_tokens=4096, provider_id='scale',
                      precomputed_input_tokens=1000, context_window=2_000_000,
                      tools=copy.deepcopy(TOOLS))
    rows = task.setdefault('_wireMessageRows', {})
    body['messages'] = rows['canonical'] = settle_items(
        body['messages'], previous=rows.get('canonical'))
    body[TEXT_COUNT_VIEW_KEY] = _task_text_count_memo(task).view('provider')
    body[ANTHROPIC_TOOL_INPUTS_KEY] = task.setdefault('_anthropicToolInputs', ToolInputs())
    body[WIRE_MESSAGE_ROWS_KEY] = rows
    body['_task_id'] = task['id']
    plan = prepare_request(body, api_protocol='anthropic', api_key='k',
                           base_url='https://scale.invalid/v1')
    sealed = plan.body
    return hashlib.sha256(sealed.payload).hexdigest(), sealed.input_tokens, sealed.local_tokens


@pytest.fixture
def visits(monkeypatch):
    """Rows each per-round stage visits, counted at the stage's per-row step."""
    import lib.llm.anthropic_outbound._to_anthropic as to_anthropic
    import lib.llm.request as request
    import lib.token_counter.tiktoken_counter as counter
    counts = {'translated': 0, 'counted': 0, 'projected': 0}
    memoized, count_images, memo_get = (
        to_anthropic._memoized, counter.count_images, request.memo_get)

    def counting_memoized(row, *args):
        counts['translated'] += 1
        return memoized(row, *args)

    def counting_count_images(messages):
        counts['counted'] += len(messages)
        return count_images(messages)

    def counting_memo_get(row, key, *args):
        if key == request._RECOUNT_PROJECTION_KEY:
            counts['projected'] += 1
        return memo_get(row, key, *args)

    monkeypatch.setattr(to_anthropic, '_memoized', counting_memoized)
    monkeypatch.setattr(counter, 'count_images', counting_count_images)
    monkeypatch.setattr(request, 'memo_get', counting_memo_get)
    return counts


def _rounds(rows, visits, *, rounds=6):
    messages = _history(rows)
    task = _new_task()
    per_round = []
    for index in range(rounds):
        messages.extend(_pair(10_000 + index))
        for key in visits:
            visits[key] = 0
        incremental = _prepare(messages, task)
        per_round.append(dict(visits))
        assert incremental == _prepare(messages, _new_task()), (rows, index)
    return per_round


def test_translation_and_recount_visit_only_new_rows_at_any_history_length(visits):
    observed = {rows: _rounds(rows, visits) for rows in (1, 10, 100)}
    # From the second round on, a round appends two rows and moves the tail
    # cache marker; the visits are the same whatever the history length.
    steady = {rows: per_round[1:] for rows, per_round in observed.items()}
    assert steady[1] == steady[10] == steady[100], observed
    for per_round in steady[100]:
        assert 0 < per_round['translated'] <= 4
        assert 0 < per_round['projected'] <= 4
        # Two counts (canonical admission, Anthropic recount), each visiting
        # the new rows once to count and once to extend its running sums.
        assert 0 < per_round['counted'] <= 16
    # The first round over a 100-row history visits all of it.
    assert observed[100][0]['translated'] >= 100


def test_a_replaced_row_resumes_translation_and_counting_at_that_row(visits):
    messages = _history(60)
    task = _new_task()
    _prepare(messages, task)
    messages[30] = {**messages[30], 'content': messages[30]['content'] + ' (edited)'}
    for key in visits:
        visits[key] = 0
    assert _prepare(messages, task) == _prepare(messages, _new_task())
    # The fresh arm visited all 60 rows; the incremental arm resumed at row 30.
    assert visits['translated'] < 60 + 40


def test_compaction_and_a_changed_system_prompt_are_recomputed_exactly():
    messages = _history(80)
    task = _new_task()
    _prepare(messages, task)
    del messages[3:40]
    messages.insert(3, {'role': 'user', 'content': '[Compacted summary]'})
    assert _prepare(messages, task) == _prepare(messages, _new_task())
    messages[0] = {'role': 'system', 'content': 'You are a careful agent. ' * 20}
    assert _prepare(messages, task) == _prepare(messages, _new_task())
    messages.extend(_pair(99))
    assert _prepare(messages, task) == _prepare(messages, _new_task())


def test_changed_translation_settings_never_reuse_held_rows():
    """The same rows on a model that replays unsigned reasoning, and on one
    whose tool-call ID contract rejects the history's IDs, translate afresh."""
    messages = _history(30)
    task = _new_task()
    for model in ('claude-sonnet-4', 'deepseek-v4-pro', 'mistral-large-latest',
                  'claude-sonnet-4'):
        messages.extend(_pair(len(messages)))
        assert _prepare(messages, task, model=model) == _prepare(
            messages, _new_task(), model=model), model


def test_settled_prefix_matches_the_held_rows_by_identity():
    from lib.conversation_sync.settled_rows import (
        settle, settled_prefix, settled_run_length, store_settled_prefix,
    )
    rows = [settle({'n': index}) for index in range(5)]
    assert settled_prefix(rows, 'k') == (None, 0)
    store_settled_prefix(rows, 'k', tuple(range(5)), settings='s', payload='p')
    held, length = settled_prefix(rows + [settle({'n': 9})], 'k')
    assert length == 5 and held.states == (0, 1, 2, 3, 4) and held.settings == 's'
    replaced = list(rows)
    replaced[3] = settle({'n': 3})  # equal content, another row: not held
    assert settled_prefix(replaced, 'k')[1] == 3
    assert settled_prefix(rows[:2], 'k')[1] == 2
    assert settled_prefix([settle({'n': 0})] + rows[1:], 'k') == (None, 0)
    assert settled_run_length(rows + [{'n': 5}] + rows, 2) == 5
