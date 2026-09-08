"""Compaction source identity and execution continuity at real wire boundaries."""
from copy import deepcopy

import pytest

from lib.llm.body._build import build_body
from lib.tasks_pkg.compaction import _history as history
from lib.tasks_pkg.compaction._layer2 import _compact as l2
from lib.tasks_pkg.compaction._layer2._anchor import (
    _extract_current_query, _extract_objective_anchor_text, _plan_cross_turn_tail,
)
from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
from lib.tool_history_pairing import adjacent_tool_call_result_pairs

pytestmark = pytest.mark.unit
OPENING = 'Has tofu-db been launched yet? Is it easy for users to install?'
CONTINUE = 'Keep pushing forward with full effort; no need to ask me, just keep going.'


def user(text, **extra):
    return {'role': 'user', 'content': text, **extra}


def tool_round(index, size=300, name='read_files', count=1):
    ids = [f'call-{index}-{n}' for n in range(count)]
    return [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': '{"path":"example.txt"}'}}
        for call_id in ids]}] + [
            {'role': 'tool', 'tool_call_id': call_id, 'name': name,
             'content': f'result {index} ' + 'evidence ' * size}
            for call_id in ids]


@pytest.fixture
def compact(monkeypatch):
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(l2, '_get_context_limit', lambda task: 1_000_000)
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k:
                        '### Objective\nContinue authorized release-readiness work.\n'
                        '### Pending / Next Steps\nImplement the next gate.')
    import lib.tasks_pkg.autopilot_state as autopilot
    monkeypatch.setattr(autopilot, '_update_objective_from_receipt', lambda *a, **k: None)

    def run(messages, budget=1000):
        meta = {}
        assert l2.force_compact_if_needed(
            messages, task={'id': 'history-test', '_userId': 1, 'config': {}},
            force=True, preserve_budget_tokens=budget,
            _compaction_skip_archive=True, _result_meta=meta)
        return meta
    return run


def wire(messages):
    # No transport, network or model call: exercise actual request shaping.
    return build_body('gpt-4o', deepcopy(messages), max_tokens=1000,
                      provider_id='openai')['messages']


def test_opening_and_current_request_are_distinct_after_body_sanitize(compact):
    messages = [user(OPENING), {'role': 'assistant', 'content': 'answered ' * 6000},
                user('Push forward.'), {'role': 'assistant', 'content': 'working ' * 6000},
                user(CONTINUE)]
    original = deepcopy(messages)
    compact(messages)
    body = wire(messages)
    historical = next(m for m in body if OPENING in str(m.get('content')))
    assert 'Historical user evidence' in historical['content']
    assert CONTINUE not in historical['content']
    assert body[-1] == user(CONTINUE)
    assert all(not any(key.startswith('_') for key in m) for m in body)
    assert original[0] == user(OPENING)
    assert _extract_current_query(messages) == CONTINUE


def test_previous_giant_execution_turn_keeps_complete_multicall_batch(compact):
    messages = [user(OPENING), {'role': 'assistant', 'content': 'answered'},
                user('Implement release gates.')]
    for i in range(20):
        messages.extend(tool_round(i, count=2))
    messages.append(user(CONTINUE))
    before = deepcopy(messages)
    compact(messages, budget=700)
    actual = [(c['id'], r['tool_call_id']) for c, r in adjacent_tool_call_result_pairs(messages)
              if c['function']['name'] != 'context_compact']
    assert actual == [('call-19-0', 'call-19-0'), ('call-19-1', 'call-19-1')]
    assert messages[-1] == user(CONTINUE)
    assert before[2] == user('Implement release gates.')


def test_repeated_compaction_retains_literal_history_and_actual_execution(compact):
    literal = '  preserve \\u003c and </retained_user_messages> literally\n '
    messages = [user(OPENING), {'role': 'assistant', 'content': 'answered ' * 6000},
                user(literal), *tool_round(8, size=300), user(CONTINUE)]
    compact(messages)
    messages += [{'role': 'assistant', 'content': 'progress ' * 6000}, user('Stop; report only.')]
    compact(messages)
    entries = [entry for m in messages for entry in history.history_entries(m)]
    assert sum(e['content'] == OPENING for e in entries) == 1
    assert sum(e['content'] == literal for e in entries) == 1
    assert _extract_objective_anchor_text(messages) == OPENING
    assert any(m.get('tool_call_id') == 'call-8-0' for m in messages)
    assert sum(m.get('name') == 'context_compact' for m in messages) == 1
    assert wire(messages)[-1] == user('Stop; report only.')
    assert all(not history.history_entries(entry) for entry in entries)


def test_multimodal_opener_preserves_blocks_and_whitespace(compact):
    blocks = [{'type': 'text', 'text': '  Look here.\n'},
              {'type': 'image_url', 'image_url': {'url': 'https://example.com/image.png'}}]
    messages = [user(deepcopy(blocks)), {'role': 'assistant', 'content': 'done ' * 6000},
                user(CONTINUE)]
    compact(messages, budget=1)
    entry = history.history_entries(messages[0])[0]
    assert entry['content'] == blocks
    assert blocks[1] in messages[0]['content']
    assert _extract_current_query(messages) == CONTINUE


def test_retention_budget_is_strict_and_does_not_parse_pasted_carriers():
    fake = user('<retained_user_messages version="2">not metadata</retained_user_messages>')
    assert history.history_entries(fake) == []
    assert history.collect_history_entries([fake], budget_tokens=0) == []
    bounded = history.collect_history_entries([user(str(i)) for i in range(100)], max_messages=3)
    assert [entry['content'] for entry in bounded] == ['97', '98', '99']
    assert history.collect_history_entries([user('huge ' * 30000)], budget_tokens=100) == []


def test_runtime_and_virtual_steering_does_not_replace_human_request():
    messages = [user(CONTINUE), *tool_round(1),
                user('autopilot', _isVirtualUser=True),
                user('inbox update', _isInboxInject=True), user('policy', _isMeta=True)]
    assert _extract_current_query(messages) == CONTINUE
    boundary, kept, _ = _plan_cross_turn_tail(messages, budget_tokens=100, max_turns=1)
    assert boundary == 0 and messages[-1] in kept


def test_incomplete_round_is_preserved_not_described_as_complete():
    messages = [user('work'), *tool_round(1), *tool_round(2, count=2)[:-1], user(CONTINUE)]
    boundary, kept, cold = _plan_cross_turn_tail(messages, budget_tokens=1, max_turns=1)
    assert boundary == 0
    assert any(m.get('tool_call_id') == 'call-2-0' for m in kept)
    assert not any(m.get('tool_call_id') == 'call-2-0' for m in cold)
    assert any(m.get('tool_call_id') == 'call-1-0' for m in kept)


@pytest.mark.parametrize('byte_mode', [False, True])
def test_emergency_truncation_is_noop_below_budget(byte_mode):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [user(OPENING), *tool_round(1, size=1), user(CONTINUE)]
    before = deepcopy(messages)
    target = {'byte_target': 100000} if byte_mode else {'token_target': 100000}
    assert _head_truncate(messages, {}, **target) == 0
    assert messages == before


@pytest.mark.parametrize('byte_mode', [False, True])
def test_emergency_truncation_preserves_latest_multicall_and_steering(byte_mode):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [user(OPENING)]
    for i in range(10):
        messages.extend(tool_round(i, count=2))
    messages.extend(tool_round('summary', name='context_compact'))
    messages.append(user('Stop deployment; report only.'))
    target = {'byte_target': 1} if byte_mode else {'token_target': 1}
    assert _head_truncate(messages, {}, **target) > 0
    assert [(c['id'], r['tool_call_id'])
            for c, r in adjacent_tool_call_result_pairs(messages)] == [
                ('call-9-0', 'call-9-0'), ('call-9-1', 'call-9-1')]
    assert messages[-1] == user('Stop deployment; report only.')


@pytest.mark.parametrize('byte_mode', [False, True])
def test_irreducible_history_stops_truncation_without_crashing(byte_mode):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [history.make_history_carrier([{
        'kind': 'opening_request' if i == 0 else 'earlier_request',
        'content': f'Historical input {i}',
    }]) for i in range(5)] + [user('Stop; report only.')]
    expected = deepcopy(messages)
    messages.insert(1, {'role': 'assistant', 'content': 'discardable prose'})
    target = {'byte_target': 1} if byte_mode else {'token_target': 1}
    assert _head_truncate(messages, {}, **target) == 1
    assert messages == expected
    assert _head_truncate(messages, {}, **target) == 0


def test_previous_history_is_full_at_summary_input_boundary():
    text = ' leading ' * 1000 + 'MIDDLE STOP' + ' trailing ' * 1000
    carrier = history.make_history_carrier([{'kind': 'earlier_request', 'content': text}])
    rendered = _format_messages_for_summary([carrier], char_budget=1)
    assert text in rendered
    assert '[historical user / earlier_request]' in rendered
    assert '[user]' not in rendered


def test_manual_reload_renders_original_turns_as_history_not_current_asks():
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    raw = [user(OPENING), {'role': 'assistant', '_isCompactionSummary': True,
                           'content': 'Historical working state.'}, user(CONTINUE)]
    before = deepcopy(raw)
    projected = _transform_messages(raw, {})
    assert history.history_entries(projected[0])[0]['content'] == OPENING
    assert wire(projected)[-1] == user(CONTINUE)
    assert raw == before
