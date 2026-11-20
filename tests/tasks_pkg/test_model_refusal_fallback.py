"""Model refusals retry once on the configured fallback and settle typed.

A provider refusal is an HTTP 200 ``content_filter`` finish (Claude's
``stop_reason: refusal`` carries ``stop_details``). Anthropic documents that
the partial output must be discarded and that the same model usually refuses
again, so the round moves to the configured fallback model; without one it
settles ``truncated`` with cause ``model_refusal`` and a typed envelope instead
of canned answer text. The HTTP 450 gateway block never switches models.
"""
from __future__ import annotations

import lib.tasks_pkg.llm_fallback._pool as _fallback_pool
import lib.tasks_pkg.llm_fallback._retry as _fallback_retry
import lib.tasks_pkg.llm_fallback._transport as _fallback_transport
import lib.tasks_pkg.llm_fallback._usage as _fallback_usage
import lib.tasks_pkg.manager._events as _fallback_events

import threading

import pytest

import lib.tasks_pkg.llm_fallback._call as call_mod
import lib.tasks_pkg.round_retry as round_retry_mod
from lib.agent_core.events import EventType
from lib.conversation_sync.validation import decode
from lib.llm.llm_errors import ContentFilterError, RateLimitError
from lib.llm.stream_result import ProviderStreamResult
from tests.conversation.test_durable_attempt_execution import claimed as claimed
from tests.support.semantic_authorities import semantic_authority as semantic_authority
from lib.turn_lifecycle import _settlement
from lib.turn_verdict import (
    CONTENT_FILTER_SETTLEMENT_KEY,
    TurnStatus,
    derive_task_verdict,
    require_deliverable_task,
)

pytestmark = pytest.mark.unit

_REFUSAL = {'category': 'cyber',
            'explanation': 'This request was declined because it could enable cyber harm.'}


def _result(finish, *, content='', refusal=None, prompt_tokens=250_000):
    usage = {'prompt_tokens': prompt_tokens, 'completion_tokens': 3,
             'total_tokens': prompt_tokens + 3}
    if refusal is not None:
        usage['_model_refusal'] = dict(refusal)
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': content}, finish, usage)


def _task(**config):
    return {
        'id': 'refusal-task-0001', 'convId': 'conv-refusal', '_userId': 1,
        'config': dict(config), 'events': [], 'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'content': 'Earlier narration.Partial refused text',
        'thinking': 'refused thinking',
        '_round_base_content': 'Earlier narration.',
        '_round_base_thinking': '',
    }


class _SpeculativeTools:
    def __init__(self):
        self.restarts = []

    def on_tool_call_ready(self, *_args, **_kwargs):
        return None

    def on_provider_attempt_restart(self, *, reason=''):
        self.restarts.append(reason)


@pytest.fixture
def harness(monkeypatch):
    state = {'events': [], 'reset_events': [], 'streams': [], 'rescues': 0,
             'script': [], 'fallback': 'kimi-k3'}

    def fake_stream(task, body, tag='', on_tool_call_ready=None, **_kwargs):
        from types import SimpleNamespace
        from lib.llm.request import mark_model_request_dispatched
        mark_model_request_dispatched(SimpleNamespace(
            on_model_request_dispatching=body.get('_on_model_request_dispatching'),
            on_model_request_dispatched=body.get('_on_model_request_dispatched')))
        state['streams'].append((body.get('model'), tag))
        outcome = state['script'].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def no_rescue(*_args, **_kwargs):
        state['rescues'] += 1
        return None

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', fake_stream)
    monkeypatch.setattr(_fallback_retry, '_get_fallback_model',
                        lambda task: state['fallback'])
    monkeypatch.setattr(_fallback_usage, '_emit_round_usage', lambda *a, **k: None)
    monkeypatch.setattr(_fallback_events, 'append_event',
                        lambda task, event: state['events'].append(event))
    monkeypatch.setattr(_fallback_pool, '_attempt_pool_rescue', no_rescue)
    monkeypatch.setattr(round_retry_mod, 'append_event',
                        lambda task, event: state['reset_events'].append(event))
    return state


def _call(task, model='claude-opus-5.5', *, round_num=6, messages=None,
          on_tool_call_ready=None, api_rounds=None, tool_list=None,
          preset='medium', thinking_enabled=True):
    return call_mod._llm_call_with_fallback(
        task, {'model': model}, model, round_num, 4096, True, tool_list,
        messages or [{'role': 'user', 'content': 'Summarize the results.'}],
        preset, thinking_enabled, {}, api_rounds if api_rounds is not None else [],
        on_tool_call_ready=on_tool_call_ready)


def test_refusal_discards_partial_output_and_answers_on_fallback(harness):
    harness['script'] = [
        _result('content_filter', content='Partial refused text', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
    ]
    tools = _SpeculativeTools()
    task = _task()
    api_rounds = []

    result = _call(task, on_tool_call_ready=tools.on_tool_call_ready,
                   api_rounds=api_rounds)

    assert [model for model, _tag in harness['streams']] == ['claude-opus-5.5', 'kimi-k3']
    assert result['model'] == 'kimi-k3'
    assert result['_loop_action'] is None
    assert result['assistant_msg']['content'] == 'Fallback answer.'
    # The refused attempt is billed but never adopted.
    refused = [row for row in api_rounds if row['tag'] == 'R7-REFUSED']
    assert len(refused) == 1 and refused[0]['responseAuthoring'] is False
    assert refused[0]['model'] == 'claude-opus-5.5'
    assert task['content'] == 'Earlier narration.' and task['thinking'] == ''
    assert any(event.get('type') == EventType.DELTA_RESET
               and event.get('discard') for event in harness['reset_events'])
    assert tools.restarts == ['model refusal']
    switch = [event for event in harness['events']
              if event.get('type') == EventType.MODEL_FALLBACK]
    assert switch and switch[0]['fallbackKind'] == 'content_filter'
    assert switch[0]['fallbackFrom'] == 'claude-opus-5.5'
    assert any(event.get('detailKey') == 'stream.phase.refusalFallback'
               for event in harness['events'])
    assert CONTENT_FILTER_SETTLEMENT_KEY not in task


def test_refusal_fallback_and_its_tool_rounds_stay_on_the_fallback_model(harness):
    harness['script'] = [
        _result('content_filter', content='Partial refused text', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
    ]
    result = _call(_task())

    assert [model for model, _tag in harness['streams']] == ['claude-opus-5.5', 'kimi-k3']
    assert result['model'] == 'kimi-k3'

    task = _task()
    task['_fallback_model'] = 'kimi-k3'
    task['_fallback_from'] = 'claude-opus-5.5'
    task['_fallback_kind'] = 'content_filter'
    refused = [{'role': 'user', 'content': 'x'}]
    from lib.tasks_pkg.llm_fallback import _switch as switch_mod
    switch_mod.record_refused_history(task, refused)
    harness['script'] = [_result('tool_calls', content='')]
    _call(task, model='kimi-k3', messages=refused + [
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': '1'}]},
        {'role': 'tool', 'tool_call_id': '1', 'content': 'result'}])
    # The refused attempt's own tool round stays on the fallback.
    assert harness['streams'][-1][0] == 'kimi-k3'

    task = _task()
    task['_fallback_model'] = 'kimi-k3'
    task['_fallback_from'] = 'claude-opus-5.5'
    task['_fallback_kind'] = 'user_switch'
    harness['script'] = [_result('stop', content='Deliberate switch.')]
    _call(task, model='kimi-k3')
    assert harness['streams'][-1][0] == 'kimi-k3'


def test_sensitive_word_http_500_skips_retry_and_uses_configured_fallback(harness):
    from lib.llm.llm_errors import _classify_http_error

    try:
        _classify_http_error(
            500,
            'API HTTP 500: user sensitive words detected: example_flag (request id: test)',
            'claude-opus-5.5',
            '[test]',
        )
    except Exception as error:
        rejection = error
    harness['script'] = [rejection, _result('stop', content='Fallback answer.')]

    result = _call(_task())

    assert [model for model, _tag in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3']
    assert result['model'] == 'kimi-k3'
    assert result['assistant_msg']['content'] == 'Fallback answer.'
    assert harness['rescues'] == 0


def test_fallback_wire_tools_match_native_model_round(harness, monkeypatch):
    """A fallback round on model M must send the SAME wire tools a native
    round on M would send.

    Anthropic's prompt cache is keyed per upstream credential; when a refusal
    fails over opus→kimi, the fallback body was rebuilt via ``build_body``
    directly and dispatched WITHOUT the shared ``preflight_wire_tool_body``
    boundary. The fallback wire kept the canonical root-oneOf/anyOf schemas
    while every native kimi round MFJS-projects them, so the fallback round's
    hoisted ``<bytes>tools`` diverged from the kimi namespace it landed in —
    a client-caused prefix break + full re-bill on the NEXT kimi round
    (observed live 2026-10-25 18:25, conv mvl5wxpj rid 7f0c4531/52b1c908).
    The rebuilt fallback body carries the round's request metadata, and the
    shared final projection that ``prepare_request`` applies to every physical
    attempt (``project_tool_wire``, ending in ``preflight_wire_tool_body``)
    must turn it into the bytes of a native round on the fallback model
    ("Fallback wire-byte stability" in docs/modules/llm_io.md).
    """
    import json
    from copy import deepcopy
    from types import SimpleNamespace

    import lib.context_telemetry as context_telemetry
    import lib.tasks_pkg.orchestrator._round_request_prep as round_request_prep
    import lib.tools.gateway as gateway
    from lib.llm.tool_wire import project_tool_wire, provider_tool_name

    dispatched = {}
    scripted_stream = _fallback_transport.stream_llm_response

    def record_dispatch(task, body, tag='', **kwargs):
        dispatched[tag] = body
        return scripted_stream(task, body, tag=tag, **kwargs)

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', record_dispatch)

    preflighted = []
    shared_preflight = gateway.preflight_wire_tool_body

    def capture_preflight(body, **kwargs):
        preflighted.append(body.get('model'))
        return shared_preflight(body, **kwargs)

    monkeypatch.setattr(gateway, 'preflight_wire_tool_body', capture_preflight)

    def final_wire(body):
        projected = deepcopy(body)
        preflighted.clear()
        project_tool_wire(projected, api_protocol='openai')
        assert preflighted == [body['model']], (
            'never passed through the wire-tool preflight: %r' % preflighted)
        return projected['tools']

    root_oneof = {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': 'Write a file',
            'parameters': {'oneOf': [
                {'type': 'object', 'properties': {
                    'path': {'type': 'string'},
                    'content': {'type': 'string'}},
                 'required': ['path', 'content']},
                {'type': 'object', 'properties': {
                    'path': {'type': 'string'},
                    'append': {'type': 'boolean'}}}]}},
    }

    harness['script'] = [
        _result('content_filter', content='x', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
    ]
    task = _task()
    messages = [{'role': 'user', 'content': 'go'}]

    _call(task, messages=messages, api_rounds=[], tool_list=[root_oneof],
          on_tool_call_ready=None)

    fallback_body = dispatched.get('R7-FALLBACK')
    assert fallback_body is not None and fallback_body['model'] == 'kimi-k3', (
        'no fallback attempt was dispatched for kimi-k3: %r' % sorted(dispatched))
    fallback_wire = final_wire(fallback_body)

    # What a NATIVE kimi round sends: the ordinary round request for the same
    # model through the same final projection.
    monkeypatch.setattr(round_request_prep, 'emit_messages_snapshot_event',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(context_telemetry, 'capture_round_context',
                        lambda *args, **kwargs: None)
    _tools, native_body = round_request_prep.build_round_request(
        task, SimpleNamespace(model='kimi-k3', preset='kimi-k3', thinking_enabled=True),
        messages, [root_oneof], round_num=6, tid=task['id'][:8],
        thinking_depth='medium', temperature=1.0, max_tokens=4096,
        response_format=None)
    native_wire = final_wire(native_body)

    projected_write = [tool for tool in fallback_wire
                       if provider_tool_name(tool) == 'write_file']
    assert projected_write and projected_write[0] != root_oneof, (
        'the kimi projection must relax the canonical root oneOf')
    assert json.dumps(fallback_wire, sort_keys=True) == json.dumps(
        native_wire, sort_keys=True), (
        'fallback wire tools differ from a native kimi round:\n'
        ' fallback=%s\n native =%s' % (
            json.dumps(fallback_wire, sort_keys=True)[:400],
            json.dumps(native_wire, sort_keys=True)[:400]))


@pytest.mark.parametrize('fallback, config, note', [
    ('', {}, 'No fallback model is configured.'),
    ('', {'disableModelFallback': True}, 'Model fallback is disabled for this request.'),
    ('claude-opus-5.5', {}, 'claude-opus-5.5 is already the configured fallback model.'),
])
def test_refusal_without_distinct_fallback_settles_typed_never_resends(
        harness, fallback, config, note):
    harness['fallback'] = fallback
    harness['script'] = [_result('content_filter', refusal=_REFUSAL)]
    task = _task(**config)

    result = _call(task)

    assert len(harness['streams']) == 1, 'a refused request must not be resent'
    assert harness['rescues'] == 0, 'a refusal is not unavailability'
    assert result['_loop_action'] == 'break'
    assert result['finish_reason'] == 'content_filter'
    assert result['_loop_exit_reason'] == 'model_refusal_round_6'
    assert 'error' not in task
    stamped = task[CONTENT_FILTER_SETTLEMENT_KEY]
    assert stamped['cause'] == 'model_refusal'
    envelope = stamped['error']
    assert envelope['kind'] == 'content_filter'
    assert envelope['context'] == 'model-refusal'
    assert envelope['titleKey'] == 'err.k.content_filter.titleRefusal'
    assert envelope['hintKey'] == 'err.k.content_filter.hintRefusal'
    assert 'category: cyber' in envelope['detail'] and note in envelope['detail']


def test_fallback_refusal_also_settles_with_both_attempts_billed(harness):
    harness['script'] = [
        _result('content_filter', refusal=_REFUSAL),
        _result('content_filter', content='fallback partial'),
    ]
    task = _task()
    api_rounds = []

    result = _call(task, round_num=0, api_rounds=api_rounds)

    assert [tag for _model, tag in harness['streams']] == ['R1', 'R1-FALLBACK']
    assert {row['tag'] for row in api_rounds} == {'R1-REFUSED', 'R1-FALLBACK-REFUSED'}
    assert result['_loop_action'] == 'break' and result['model'] == 'kimi-k3'
    assert '_fallback_model' not in task, 'a refused fallback produced nothing'
    envelope = task[CONTENT_FILTER_SETTLEMENT_KEY]['error']
    assert envelope['model'] == 'kimi-k3'
    assert 'no category reported' in envelope['detail']
    assert 'claude-opus-5.5 declined first.' in envelope['detail']


def test_automatic_turn_refusal_does_not_blame_the_user(harness):
    harness['fallback'] = ''
    harness['script'] = [_result('content_filter', refusal=_REFUSAL)]
    task = _task()
    messages = [
        {'role': 'user', 'content': 'Original human request'},
        {'role': 'assistant', 'content': 'Delegated work.'},
        {'role': 'user', 'content': 'Settled swarm updates: 2 completed',
         '_initiator': 'swarm'},
        {'role': 'user', 'content': '<project context>', '_contextComposer': True},
    ]

    _call(task, messages=messages)

    envelope = task[CONTENT_FILTER_SETTLEMENT_KEY]['error']
    assert envelope['hintKey'] == 'err.k.content_filter.hintRefusalAutomatic'
    assert 'nothing you typed was refused' in envelope['hint']


def test_gateway_block_never_switches_models(harness):
    harness['script'] = [ContentFilterError('HTTP 450: blocked by policy')]
    task = _task()

    result = _call(task)

    assert len(harness['streams']) == 1
    assert result['_loop_action'] == 'break'
    assert result['finish_reason'] == 'content_filter'
    stamped = task[CONTENT_FILTER_SETTLEMENT_KEY]
    assert stamped['cause'] == 'content_filter'
    assert stamped['error']['context'] == 'gateway-content-filter'
    assert stamped['error']['hintKey'] == 'err.k.content_filter.hintGateway'


@pytest.mark.parametrize('error, kind', [
    (RateLimitError('API HTTP 402: credit exhausted', is_quota=True,
                    status_code=402), 'quota'),
    (RateLimitError('API HTTP 429: rate limit reached', status_code=429),
     'ratelimit'),
])
def test_credit_and_rate_limit_stay_out_of_the_refusal_lane(
        harness, error, kind):
    """402/429 are routing/quota failures, never a content-policy refusal."""
    harness['script'] = [error, _result('stop', content='Fallback answer.')]
    task = _task()
    api_rounds = []

    result = _call(task, api_rounds=api_rounds)

    assert result['assistant_msg']['content'] == 'Fallback answer.'
    assert CONTENT_FILTER_SETTLEMENT_KEY not in task
    assert not any(str(row['tag']).endswith('-REFUSED') for row in api_rounds)
    assert any(event.get('type') == EventType.MODEL_FALLBACK
               and event.get('fallbackKind') == kind
               for event in harness['events'])


def _terminal_task(**extra):
    task = {'status': 'done', 'finishReason': 'content_filter',
            'streamState': 'provider_finished', 'model': 'claude-opus-5.5',
            'content': '', 'toolRounds': [], 'config': {}}
    task.update(extra)
    return task


def test_next_request_after_refusal_returns_to_primary_and_audits_it(
        harness, monkeypatch, tmp_path):
    """A content-filter refusal is per-request: the next request returns.

    Deterministic fake provider: opus refuses once (content_filter), kimi
    serves that round, then the NEXT user request (new turn on the same
    conversation, history extended with the fallback round's rows) must go
    back to opus, which answers. The return is a direction=primary
    model_switch row for a lead conversation. This is the exact 723a42af
    incident shape: R56 refused, R57+ all ran on kimi with no primary row.
    """
    from lib.logging_pkg import lead_audit
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='fallback r56'),
        _result('stop', content='primary r57'),
    ]
    task = _task()
    task['convId'] = 'coordinator-723a42af833283b7f777b246d29da590'
    task['config']['_turnId'] = 'turn-1'
    api_rounds = []
    history = [
        {'role': 'system', 'content': 'research system prompt'},
        {'role': 'user', 'content': 'research objective'},
    ]
    _call(task, round_num=55, messages=history, api_rounds=api_rounds)

    # Next request of the same conversation: the served fallback round is
    # appended to the history, then the lead's next Turn message arrives.
    history = history + [
        {'role': 'assistant', 'content': 'fallback r56'},
        {'role': 'user', 'content': "the lead's next request"},
    ]
    result = _call(task, model='kimi-k3', round_num=56,
                   messages=history, api_rounds=api_rounds)

    assert result['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'claude-opus-5.5']
    switches = [e for e in harness['events']
                if e.get('type') == EventType.MODEL_FALLBACK]
    assert [e['fallbackModel'] for e in switches] == ['kimi-k3', 'claude-opus-5.5']
    assert switches[-1]['fallbackKind'] == 'primary_retry'
    import json
    rows = [json.loads(l) for l in
            next(tmp_path.glob('lead-audit/lead-audit-*.jsonl')).read_text().splitlines()]
    kinds = [(r['kind'], r.get('to'), r.get('direction'), r.get('round')) for r in rows]
    assert ('model_switch', 'claude-opus-5.5', 'primary', 57) in kinds


def test_refusal_on_the_return_probe_falls_back_for_that_request_only(harness):
    """A repeated refusal on the primary falls back again, never sticks."""
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='fallback r6'),
        _result('content_filter', content='Partial again', refusal=_REFUSAL),
        _result('stop', content='fallback r7'),
        _result('stop', content='primary r8'),
    ]
    task = _task()
    api_rounds = []
    history = [{'role': 'user', 'content': 'research objective'}]
    _call(task, messages=history, api_rounds=api_rounds)
    history = history + [{'role': 'assistant', 'content': 'fallback r6'},
                         {'role': 'user', 'content': 'next request'}]
    _call(task, model='kimi-k3', round_num=7, messages=history,
          api_rounds=api_rounds)
    history = history + [{'role': 'assistant', 'content': 'fallback r7'},
                         {'role': 'user', 'content': 'next request again'}]
    result = _call(task, model='kimi-k3', round_num=8, messages=history,
                   api_rounds=api_rounds)
    assert result['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'claude-opus-5.5', 'kimi-k3',
        'claude-opus-5.5']
    # Return probes are visible in both directions, including the re-refusal.
    assert [(e['fallbackFrom'], e['fallbackModel']) for e in harness['events']
            if e.get('type') == EventType.MODEL_FALLBACK] == [
        ('claude-opus-5.5', 'kimi-k3'), ('kimi-k3', 'claude-opus-5.5'),
        ('claude-opus-5.5', 'kimi-k3'), ('kimi-k3', 'claude-opus-5.5')]


def test_changed_history_retries_primary_unchanged_history_stays_on_fallback(harness):
    """A refusal tracks the history: retry primary only when history changed.

    The first fallback round serves on kimi. The next round has DIFFERENT
    history (a compaction/cleared results) → probe opus, which answers. A
    later round with the SAME history as the refusal stays on kimi (no
    pointless refused probe).
    """
    harness['script'] = [
        _result('content_filter', content='Partial refused text', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),   # round 6 served on kimi
        _result('stop', content='Primary again.'),     # round 7 changed history → opus
    ]
    task = _task()
    api_rounds = []
    _call(task, api_rounds=api_rounds)  # default messages → refusal on opus, serve on kimi

    # Next round: history CHANGED (new content) → primary probe.
    second = _call(task, model='kimi-k3', round_num=7,
                   messages=[{'role': 'user', 'content': 'a different, compacted history'}],
                   api_rounds=api_rounds)
    assert second['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'claude-opus-5.5']
    switches = [e for e in harness['events'] if e.get('type') == EventType.MODEL_FALLBACK]
    assert [e['fallbackModel'] for e in switches] == ['kimi-k3', 'claude-opus-5.5']
    assert switches[-1]['fallbackFrom'] == 'kimi-k3'


def test_same_attempt_appended_tool_rounds_stay_on_fallback(harness):
    """Only the round answering the SAME refused payload skips the probe.

    The first appended tool round repeats exactly the refused request plus
    the fallback's own answer, so re-probing opus there would just refuse the
    same bytes again. The following round returns to the primary: the refusal
    is per-request, not a latch on the whole tool loop.
    """
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
        _result('stop', content='Still fallback.'),   # appended tool rows → kimi
        _result('stop', content='Primary again.'),    # next round → opus
    ]
    task = _task()
    api_rounds = []
    history = [{'role': 'user', 'content': 'unchanged research history'}]
    _call(task, messages=history, api_rounds=api_rounds)
    history = history + [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'c1', 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'tool result'},
    ]
    _call(task, model='kimi-k3', round_num=7, messages=history,
          api_rounds=api_rounds)
    # The appended tool rows did not probe opus again.
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3']
    assert [e['fallbackModel'] for e in harness['events']
            if e.get('type') == EventType.MODEL_FALLBACK] == ['kimi-k3']
    # The conversation's next request returns to the primary.
    history = history + [{'role': 'assistant', 'content': 'Still fallback.'},
                         {'role': 'user', 'content': 'the next request'}]
    result = _call(task, model='kimi-k3', round_num=8, messages=history,
                   api_rounds=api_rounds)
    assert result['model'] == 'claude-opus-5.5'
    assert harness['streams'][-1][0] == 'claude-opus-5.5'


def test_switch_back_and_fallback_rounds_reach_the_lead_audit(harness, monkeypatch, tmp_path):
    """A project Worker records each switch in the durable lead-audit stream."""
    from lib.logging_pkg import lead_audit
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
        _result('stop', content='Primary answer.'),
    ]
    task = _task()
    task['convId'] = 'coordinator-abc'
    task['config']['_turnId'] = 'turn-1'
    api_rounds = []
    _call(task, api_rounds=api_rounds)
    # Changed history on the next round triggers the return-to-primary switch.
    _call(task, model='kimi-k3', round_num=7,
          messages=[{'role': 'user', 'content': 'a changed history'}],
          api_rounds=api_rounds)
    import json
    rows = [json.loads(l) for l in next(tmp_path.glob('lead-audit/lead-audit-*.jsonl')).read_text().splitlines()]
    kinds = [(r['kind'], r.get('to'), r.get('direction')) for r in rows]
    assert ('model_switch', 'kimi-k3', 'fallback') in kinds
    assert ('model_switch', 'claude-opus-5.5', 'primary') in kinds
    assert all(r['conv'] == 'coordinator-abc' for r in rows)


def test_fallback_writeback_keeps_the_rounds_reasoning_choice(harness):
    """Later rounds latched on the fallback rebuild intent from this writeback.

    A Default depth (thinking_enabled=None) must stay Default there, not turn
    into a 'medium' preset that the fallback's ladder then clamps.
    """
    harness['script'] = [
        _result('content_filter', content='Partial refused text', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
    ]
    result = _call(_task(), preset='max', thinking_enabled=None)

    assert result['model'] == 'kimi-k3'
    assert result['preset'] == 'max'
    assert result['thinking_enabled'] is None


def test_next_turn_tries_the_primary_after_a_refused_previous_turn(harness):
    """Stickiness is turn-scoped state on the task, not a model demotion."""
    task = _task()
    task['_fallback_kind'] = 'content_filter'
    task['_fallback_from'] = 'claude-opus-5.5'
    harness['script'] = [_result('stop', content='Primary answer.')]

    result = _call(task, model='claude-opus-5.5', round_num=0, api_rounds=[])

    assert [m for m, _ in harness['streams']] == ['claude-opus-5.5']
    assert result['model'] == 'claude-opus-5.5'
    assert result['preset'] == 'medium'


def test_refusal_settles_truncated_with_its_own_cause_and_typed_error(harness):
    harness['fallback'] = ''
    harness['script'] = [_result('content_filter', refusal=_REFUSAL)]
    live = _task()
    _call(live)
    task = _terminal_task(**{CONTENT_FILTER_SETTLEMENT_KEY: live[CONTENT_FILTER_SETTLEMENT_KEY]})

    verdict = derive_task_verdict(task)
    status, settlement = _settlement(
        task, {'type': 'done', 'finishReason': 'content_filter'},
        {'content': '', 'toolRounds': []})

    assert verdict.status is TurnStatus.TRUNCATED and verdict.cause == 'model_refusal'
    assert status == 'truncated'
    assert settlement['cause'] == 'model_refusal'
    assert settlement['evidence'] == 'provider_limit'
    assert settlement['error']['context'] == 'model-refusal'
    assert settlement['error']['titleKey'] == 'err.k.content_filter.titleRefusal'
    # A refused turn must stay loadable: snapshots validate every settlement.
    decode('TurnSettlement', settlement)
    # Adapters keep their 200 finish_reason=content_filter projection.
    assert require_deliverable_task(task).status is TurnStatus.TRUNCATED


def test_unstamped_content_filter_keeps_the_historic_settlement():
    status, settlement = _settlement(
        _terminal_task(), {'type': 'done', 'finishReason': 'content_filter'},
        {'content': 'partial', 'toolRounds': []})
    assert status == 'truncated'
    assert settlement['cause'] == 'content_filter'
    assert settlement['error'] is None
    decode('TurnSettlement', settlement)


def test_tool_loop_appending_fallback_rounds_does_not_retry_primary(harness):
    """A real tool loop: fallback rounds grow the history, not the refused prefix.

    Refuse at round k, then append three rounds (assistant + tool result
    each). Only the round whose appended rows are the refused round's OWN
    fallback answer skips the probe; the next round returns to the primary.
    Rewriting an old tool result (clearing) also rewrites the refused prefix
    → its own probe.
    """
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='fallback r6'),
        _result('stop', content='fallback r7'),
        _result('stop', content='primary r8'),
        _result('stop', content='primary r9'),
        _result('stop', content='primary r10'),
        _result('stop', content='primary after clearing'),
    ]
    task = _task()
    api_rounds = []
    history = [
        {'role': 'system', 'content': 'research system prompt'},
        {'role': 'user', 'content': 'research objective'},
        {'role': 'assistant', 'content': 'earlier answer', 'tool_calls': [
            {'id': 'c0', 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c0', 'content': 'research result'},
    ]
    refused_len = len(history)

    # Round k: opus refuses, kimi serves.
    _call(task, messages=history, api_rounds=api_rounds)
    # Three more rounds of the SAME attempt, each appending only the tool
    # rows the tool loop adds between two provider requests (no new request).
    for step in range(3):
        history = history + [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'c{step + 1}', 'type': 'function',
                 'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'c{step + 1}',
             'content': f'tool result {step}'},
        ]
        _call(task, model='kimi-k3', round_num=7 + step,
              messages=history, api_rounds=api_rounds)

    # One skipped round (r7), then the primary returns (r8); the stamps clear,
    # so r9 dispatches on the loop's model (kimi) with no switch event.
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'claude-opus-5.5',
        'kimi-k3']
    assert [e['fallbackModel'] for e in harness['events']
            if e.get('type') == EventType.MODEL_FALLBACK] == [
        'kimi-k3', 'claude-opus-5.5']

    # Clearing rewrites an OLD tool result inside the refused prefix.
    task['_fallback_model'] = 'kimi-k3'
    task['_fallback_from'] = 'claude-opus-5.5'
    task['_fallback_kind'] = 'content_filter'
    history = [dict(row) for row in history]
    history[refused_len - 1] = {
        'role': 'tool', 'tool_call_id': 'c0',
        'content': '[old tool result cleared]'}
    _call(task, model='kimi-k3', round_num=10,
          messages=history, api_rounds=api_rounds)
    assert harness['streams'][-1][0] == 'claude-opus-5.5'
    # A compaction that shortens the history below the refused length also retries.
    harness['script'].append(_result('stop', content='primary after compaction'))
    task['_fallback_model'] = 'kimi-k3'
    task['_fallback_from'] = 'claude-opus-5.5'
    task['_fallback_kind'] = 'content_filter'
    _call(task, model='kimi-k3', round_num=11,
          messages=history[:1] + [{'role': 'user', 'content': 'compacted summary'}],
          api_rounds=api_rounds)
    assert harness['streams'][-1][0] == 'claude-opus-5.5'


def _append_tool_round(history, step):
    return history + [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': f'c{step}', 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': f'c{step}',
         'content': f'tool result {step}'},
    ]


def test_only_the_refused_rounds_own_tool_round_skips_the_return(harness):
    """The exemption covers ONE round, not the whole attempt.

    Round k refuses on opus and kimi serves it. The request that only appends
    that served round's own tool rows answers the same refused payload, so it
    stays on kimi. The round after it returns to the primary, and once the
    primary answers there is no fallback left to return from.
    """
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='fallback r6'),
        _result('stop', content='still fallback r7'),
        _result('stop', content='primary r8'),
    ]
    task = _task()
    api_rounds = []
    history = [{'role': 'user', 'content': 'research objective'}]
    _call(task, messages=history, api_rounds=api_rounds)
    for step in range(1, 4):
        history = _append_tool_round(history, step)
        _call(task, model='kimi-k3', round_num=6 + step, messages=history,
              api_rounds=api_rounds)
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'claude-opus-5.5',
        'kimi-k3']


def test_a_repeated_refusal_falls_back_again_and_returns_once_more(harness):
    """A probe refused again re-arms exactly one skip, never a permanent latch."""
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='fallback r6'),
        _result('content_filter', content='Partial again', refusal=_REFUSAL),
        _result('stop', content='fallback r7'),
        _result('stop', content='fallback r8'),
        _result('stop', content='fallback r9'),
    ]
    task = _task()
    api_rounds = []
    history = [{'role': 'user', 'content': 'research objective'}]
    _call(task, messages=history, api_rounds=api_rounds)
    for step in range(1, 5):
        history = _append_tool_round(history, step)
        _call(task, model='kimi-k3', round_num=6 + step, messages=history,
              api_rounds=api_rounds)
    # r6 refused → fallback; r7 skip → kimi; r8 probe refused → fallback;
    # r9 skip → kimi. The fallback never latches: probes keep happening.
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'claude-opus-5.5',
        'kimi-k3', 'kimi-k3']


def test_worker_tool_loop_returns_to_the_primary_after_a_refusal(harness):
    """After R2 refuses, R3 skips once and R4 onward probe the primary."""
    task = _task()
    task['convId'] = 'worker-25bb6da1fd00dd68d2e61cb6'
    api_rounds = []
    history = [{'role': 'user', 'content': 'Update the global hope-910c skill'}]
    harness['script'] = [_result('tool_calls', content='R1 on the primary')]
    _call(task, round_num=0, messages=history, api_rounds=api_rounds)
    # R2: the primary refuses, the configured fallback serves the round.
    harness['script'].extend([
        _result('content_filter', content='', refusal=_REFUSAL),
        _result('tool_calls', content='R2 fallback'),
    ])
    history = _append_tool_round(history, 1)
    _call(task, round_num=1, messages=history, api_rounds=api_rounds)
    # R3 skips once; every later request probes even when the probe refuses.
    model = 'kimi-k3'
    for step in range(2, 8):
        history = _append_tool_round(history, step)
        if step > 2:
            harness['script'].append(_result('content_filter', refusal=_REFUSAL))
        harness['script'].append(_result('tool_calls', content=f'R{step + 1} fallback'))
        model = _call(task, model=model, round_num=step, messages=history,
                      api_rounds=api_rounds)['model']

    streams = [m for m, _ in harness['streams']]
    assert streams[0] == 'claude-opus-5.5'          # R1
    assert streams[1:3] == ['claude-opus-5.5', 'kimi-k3']   # R2 refused + served
    assert streams[3] == 'kimi-k3'                 # R3 consumes the one skip
    assert streams[4:] == ['claude-opus-5.5', 'kimi-k3'] * 5
    assert not harness['script']
    # The paid ledger proves the primary was attempted after the refusal.
    assert [r['model'] for r in api_rounds[:3]] == [
        'claude-opus-5.5', 'claude-opus-5.5', 'kimi-k3']


def test_model_fallback_notice_reports_whether_the_primary_returned():
    """The review notice must describe the ledger, not claim a retry always ran.

    worker-25bb6da1's receipt said "later rounds retried the primary" while
    byModel held exactly one primary round (the one that refused).
    """
    from lib.tasks_pkg.orchestrator._model_continuation import (
        model_fallback_receipt)

    never_retried = model_fallback_receipt([
        {'round': 1, 'model': 'claude-opus-5.5', 'tag': 'R1'},
        {'round': 2, 'model': 'claude-opus-5.5', 'tag': 'R2-REFUSED'},
        {'round': 2, 'model': 'kimi-k3.1-pro', 'tag': 'R2-FALLBACK'},
        {'round': 3, 'model': 'kimi-k3.1-pro', 'tag': 'R3'},
    ], 'claude-opus-5.5')
    assert 'never' in never_retried['notice']

    returned = model_fallback_receipt([
        {'round': 1, 'model': 'claude-opus-5.5', 'tag': 'R1'},
        {'round': 2, 'model': 'claude-opus-5.5', 'tag': 'R2-REFUSED'},
        {'round': 2, 'model': 'kimi-k3.1-pro', 'tag': 'R2-FALLBACK'},
        {'round': 3, 'model': 'claude-opus-5.5', 'tag': 'R3-REFUSED'},
        {'round': 3, 'model': 'kimi-k3.1-pro', 'tag': 'R3-FALLBACK'},
    ], 'claude-opus-5.5')
    assert 'retried the primary' in returned['notice']


@pytest.mark.parametrize('predispatch_failure', [None, 'checkpoint', 'abort'])
@pytest.mark.parametrize('conv_id', ['worker-refusal', 'coordinator-refusal'])
@pytest.mark.parametrize(('refuse_probe', 'history_change'), [
    (False, None), (True, None), (False, 'injection'), (False, 'rewrite'),
    (False, 'admission-rewrite'),
])
def test_root_dispatch_consumes_one_skip_for_one_request(
        harness, monkeypatch, tmp_path, conv_id, refuse_probe, history_change,
        predispatch_failure):
    from types import SimpleNamespace
    from lib.logging_pkg import lead_audit
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root
    from lib.tasks_pkg.orchestrator import _llm_round_call as round_call

    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: str(tmp_path))
    task = _task()
    task['convId'] = conv_id
    history = [{'role': 'user', 'content': 'research objective'}]
    state = SimpleNamespace(
        model='claude-opus-5.5', preset='medium', thinking_enabled=True,
        tool_call_happened=True, accumulated_usage={}, api_rounds=[], last_usage=None)
    request = SimpleNamespace(
        task=task, state=state, messages=history, tid=task['id'], cfg={},
        project_path='', project_enabled=False, thinking_depth=None,
        temperature=None, max_tokens=4096, response_format=None)
    hooks = root._RootLoopHooks(request)
    built_models = []

    def build(task, state, messages, tools, **kwargs):
        built_models.append(state.model)
        return tools, {'model': state.model}

    class Dispatched(Exception):
        pass

    def dispatch(*args, **kwargs):
        assert round_call.run_llm_call_with_fallback(*args, **kwargs) == 'proceed'
        raise Dispatched

    preparations = []

    def prepare(*args, **kwargs):
        preparations.append(None)
        if len(preparations) == 2:
            if history_change == 'injection':
                history.append({'role': 'user', 'content': 'new steering input'})
            elif history_change == 'rewrite':
                history[0] = {'role': 'user', 'content': 'compacted history'}

    admissions = []

    def admit(*args, **kwargs):
        admissions.append(kwargs['model'])
        if history_change == 'admission-rewrite' and len(admissions) == 2:
            history[0] = {'role': 'user', 'content': 'compacted during admission'}
        return {}

    monkeypatch.setattr(root, 'prepare_round_context', prepare)
    monkeypatch.setattr(root, 'admit_round_context', admit)
    monkeypatch.setattr(root, 'build_round_request', build)
    monkeypatch.setattr(root, 'build_stream_accumulator',
                        lambda *a, **k: SimpleNamespace(on_tool_call_ready=None, close=lambda **kw: None))
    monkeypatch.setattr(root, 'run_llm_call_with_fallback', dispatch)
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait.checkpoint_model_dispatch',
                        lambda *a, **k: None)
    monkeypatch.setattr(round_call, 'confirm_consumed_project_context', lambda *a, **k: None)
    monkeypatch.setattr(round_call, 'flush_deferred_peer_and_steer', lambda *a, **k: None)
    harness['script'] = [
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('tool_calls'),
    ]
    for _ in range(2):
        if refuse_probe:
            harness['script'].append(_result('content_filter', refusal=_REFUSAL))
        harness['script'].append(_result('tool_calls'))
    for step in range(4):
        if step:
            history[:] = _append_tool_round(history, step)
        if step == 1 and predispatch_failure:
            from lib.llm import AbortedError
            from lib.tasks_pkg import persistence_wait
            failure = (AbortedError('stopped before dispatch') if predispatch_failure == 'abort'
                       else RuntimeError('checkpoint failed'))
            with monkeypatch.context() as patch:
                def reject(*args, **kwargs):
                    raise failure
                patch.setattr(persistence_wait, 'checkpoint_model_dispatch', reject)
                before = list(harness['streams'])
                with pytest.raises(type(failure)):
                    hooks._dispatch_open_round(6, [])
                assert harness['streams'] == before
                assert task['_refusal_skip_left'] == 1
            built_models.pop()
        # Deliberately reuse a round number: request identity is not its ordinal.
        with pytest.raises(Dispatched):
            hooks._dispatch_open_round(6, [])

    exempt_model = 'claude-opus-5.5' if history_change else 'kimi-k3'
    assert built_models == ['claude-opus-5.5', exempt_model,
                            'claude-opus-5.5', 'claude-opus-5.5']
    expected = ['claude-opus-5.5', 'kimi-k3', exempt_model]
    expected += (['claude-opus-5.5', 'kimi-k3'] * 2 if refuse_probe
                 else ['claude-opus-5.5'] * 2)
    assert [m for m, _ in harness['streams']] == expected
    switches = [e['fallbackKind'] for e in harness['events']
                if e.get('type') == EventType.MODEL_FALLBACK]
    assert switches == (['content_filter', 'primary_retry', 'content_filter',
                         'primary_retry', 'content_filter'] if refuse_probe
                        else ['content_filter', 'primary_retry'])
    assert [r['model'] for r in state.api_rounds] == expected
    if conv_id.startswith('coordinator-'):
        import json
        audit_rows = [json.loads(line) for line in
                      next(tmp_path.glob('lead-audit/lead-audit-*.jsonl')).read_text().splitlines()]
        assert [r['direction'] for r in audit_rows] == [
            'fallback' if kind == 'content_filter' else 'primary' for kind in switches]
    if history_change == 'admission-rewrite':
        assert admissions == (['claude-opus-5.5', 'kimi-k3', 'claude-opus-5.5']
                              + ['claude-opus-5.5'] * (3 if predispatch_failure else 2))
    assert not harness['script']


def test_direct_repeated_refusal_does_not_rearm_the_skip(harness):
    task = _task()
    history = [{'role': 'user', 'content': 'research objective'}]
    model = 'claude-opus-5.5'
    harness['script'] = [
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('tool_calls'),
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
    ]
    for step in range(4):
        if step:
            history = _append_tool_round(history, step)
        model = _call(task, model=model, round_num=6, messages=history)['model']
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3',
        'claude-opus-5.5', 'kimi-k3', 'claude-opus-5.5', 'kimi-k3']
    assert not harness['script']


def test_changed_request_retires_unused_skip_before_a_repeated_refusal(harness):
    task = _task()
    history = [{'role': 'user', 'content': 'research objective'}]
    harness['script'] = [
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('tool_calls'),
    ]
    first = _call(task, messages=history)
    history += [{'role': 'user', 'content': 'a different request'}]
    second = _call(task, model=first['model'], messages=history)
    history = _append_tool_round(history, 1)
    third = _call(task, model=second['model'], messages=history)
    assert third['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'claude-opus-5.5', 'kimi-k3',
        'claude-opus-5.5']


def test_fresh_resume_uses_the_accepted_model_without_refusal_state(harness):
    harness['script'] = [_result('tool_calls')]
    result = _call(_task(), model='accepted-resume-model', round_num=0)
    assert result['model'] == 'accepted-resume-model'
    assert harness['streams'] == [('accepted-resume-model', 'R1')]
    assert not [e for e in harness['events']
                if e.get('type') == EventType.MODEL_FALLBACK]


def test_successful_primary_retires_the_old_refusal_budget(harness):
    task = _task()
    history = [{'role': 'user', 'content': 'research objective'}]
    model = 'claude-opus-5.5'
    harness['script'] = [
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('tool_calls'), _result('tool_calls'),
        _result('content_filter', refusal=_REFUSAL), _result('tool_calls'),
        _result('tool_calls'), _result('tool_calls'),
    ]
    for step in range(6):
        if step:
            history = _append_tool_round(history, step)
        model = _call(task, model=model, messages=history)['model']
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'claude-opus-5.5',
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'claude-opus-5.5']
    assert '_fallback_kind' not in task
    assert '_refusal_skip_left' not in task
    assert not harness['script']


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
@pytest.mark.parametrize('gate', ['prepare', 'abort', 'input', 'lease'])
def test_direct_unsent_request_keeps_skip_until_physical_dispatch(
        harness, monkeypatch, protocol, gate):
    from lib.llm import AbortedError
    from lib.llm._sse_core import prepare_request
    from lib.llm.request import dispatch_model_request
    from lib.tasks_pkg.llm_fallback import _switch

    task = _task()
    refused = [{'role': 'user', 'content': 'research objective'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    history = _append_tool_round(refused, 1)
    scripted_stream = _fallback_transport.stream_llm_response
    rejecting = True
    plans = []

    class InputGate:
        def prepare_input(self, body):
            if rejecting and gate == 'input':
                raise AbortedError('input admission rejected before send')

        def mark_dispatched(self):
            if rejecting and gate == 'lease':
                raise AbortedError('budget dispatch fence rejected before send')

    def stream(task, body, **kwargs):
        if rejecting and gate == 'prepare':
            raise AbortedError('preparation rejected before send')
        plan = prepare_request(dict(body, messages=[{'role': 'user', 'content': 'test'}],
                                    max_tokens=4096),
                               base_url='https://example.invalid/v1', api_protocol=protocol)
        plans.append(plan)
        assert '_on_model_request_dispatching' not in plan.body
        assert '_on_model_request_dispatched' not in plan.body
        assert task['_refusal_skip_left'] == (1 if rejecting else expected_skip)
        if rejecting and gate == 'abort':
            raise AbortedError('aborted after preparation before send')
        plan.model_budget_lease = InputGate()
        return dispatch_model_request(plan, scripted_stream, task, body, **kwargs)

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    harness['script'] = [_result('tool_calls'), _result('tool_calls')]
    before = dict(task)
    with pytest.raises(AbortedError):
        _call(task, model='kimi-k3', messages=history)
    assert harness['streams'] == []
    assert task['_refusal_skip_left'] == 1
    assert task['_fallback_kind'] == before['_fallback_kind']
    assert not harness['events']

    rejecting = False
    expected_skip = 1
    first = _call(task, model='kimi-k3', messages=history)
    assert first['model'] == 'kimi-k3'
    assert task['_refusal_skip_left'] == 0
    # Repeated transport entry on the same prepared request cannot consume a
    # second decision or stamp a spurious return to primary.
    from lib.llm.request import mark_model_request_dispatched
    mark_model_request_dispatched(plans[-1])
    assert not harness['events']
    expected_skip = 0
    second = _call(task, model=first['model'], messages=history)
    assert second['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == ['kimi-k3', 'claude-opus-5.5']
    assert '_refusal_skip_left' not in task
    assert not harness['script']


def test_selection_is_pure_and_rewrite_invalidates_uncommitted_exemption(harness):
    from lib.tasks_pkg.llm_fallback import _switch

    task = _task()
    refused = [{'role': 'user', 'content': 'research objective'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    before = dict(task)
    history = _append_tool_round(refused, 1)
    decision = _switch.decide_request_model(task, 'kimi-k3', 6, history)
    assert decision.model == 'kimi-k3'
    assert _switch.decide_request_model(task, decision.model, 6, history, decision) is decision
    assert task == before
    history[0] = {'role': 'user', 'content': 'rewritten context'}
    revised = _switch.decide_request_model(task, decision.model, 6, history, decision)
    assert revised.model == 'claude-opus-5.5'
    assert task == before
    assert not harness['events']
    harness['script'] = [_result('tool_calls')]
    result = call_mod._llm_call_with_fallback(
        task, {'model': decision.model}, decision.model, 6, 4096, True, None,
        history, 'medium', True, {}, [], request_model_decision=decision)
    assert result['model'] == 'claude-opus-5.5'
    assert harness['streams'] == [('claude-opus-5.5', 'R7')]


def test_actual_failed_dispatch_spends_skip_and_reentry_probes_primary(harness):
    from lib.llm import AbortedError
    from lib.tasks_pkg.llm_fallback import _switch

    task = _task()
    refused = [{'role': 'user', 'content': 'research objective'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    history = _append_tool_round(refused, 1)
    harness['script'] = [AbortedError('stopped after physical dispatch'), _result('tool_calls')]
    with pytest.raises(AbortedError):
        _call(task, model='kimi-k3', messages=history)
    assert task['_refusal_skip_left'] == 0
    assert _call(task, model='kimi-k3', messages=history)['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == ['kimi-k3', 'claude-opus-5.5']


@pytest.mark.parametrize('return_probe', [False, True])
def test_physical_retry_keeps_one_decision_then_next_request_probes(
        harness, monkeypatch, return_probe):
    from lib.tasks_pkg.llm_fallback import _switch

    task = _task()
    refused = [{'role': 'user', 'content': 'research objective'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    history = ([{'role': 'user', 'content': 'a changed request'}] if return_probe
               else _append_tool_round(refused, 1))
    scripted_stream = _fallback_transport.stream_llm_response

    def retry_stream(task, body, **kwargs):
        try:
            return scripted_stream(task, body, **kwargs)
        except ConnectionError:
            assert task['_refusal_skip_left'] == 0
            switches = [e for e in harness['events']
                        if e.get('type') == EventType.MODEL_FALLBACK]
            assert not switches
            return scripted_stream(task, body, **kwargs)

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', retry_stream)
    harness['script'] = [ConnectionError('connection failed after send'),
                         _result('tool_calls'), _result('tool_calls')]
    first = _call(task, model='kimi-k3', messages=history)
    selected = 'claude-opus-5.5' if return_probe else 'kimi-k3'
    assert first['model'] == selected
    second = _call(task, model=first['model'], messages=history)
    assert second['model'] == 'claude-opus-5.5'
    assert [m for m, _ in harness['streams']] == [selected, selected, 'claude-opus-5.5']
    assert [e['fallbackKind'] for e in harness['events']
            if e.get('type') == EventType.MODEL_FALLBACK] == ['primary_retry']


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
@pytest.mark.parametrize('fail_event', [1, 2])
@pytest.mark.parametrize('failure_kind', ['delivery', 'revoked', 'abort'])
def test_return_switch_publication_failure_propagates_after_real_attempt(
        harness, monkeypatch, protocol, fail_event, failure_kind):
    from types import SimpleNamespace
    from lib.agent_core.execution_budget import ModelBudgetLease
    from lib.llm._sse_core import prepare_request
    from lib.llm.request import dispatch_model_request
    from lib.llm import AbortedError
    from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
    from lib.tasks_pkg.llm_fallback import _switch

    task = _task()
    refused = [{'role': 'user', 'content': 'research objective'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    history = [{'role': 'user', 'content': 'a changed request'}]
    leases = []
    order = []
    failure_type = {'delivery': EventDeliveryError, 'revoked': EventDeliveryRevoked,
                    'abort': AbortedError}[failure_kind]
    failure = failure_type('switch publication failed after actual attempt')
    scripted_stream = _fallback_transport.stream_llm_response

    def mark(ordinal):
        assert task['_fallback_kind'] == 'content_filter'
        assert task['_refusal_skip_left'] == 1
        order.append('mark')

    def send(task, body, **kwargs):
        assert leases[-1].dispatched is True
        assert task['_fallback_kind'] == 'primary_retry'
        assert task['_refusal_skip_left'] == 0
        order.append('send')
        return scripted_stream(task, body, **kwargs)

    def stream(task, body, **kwargs):
        plan = prepare_request(dict(body, messages=history, max_tokens=4096),
                               base_url='https://example.invalid/v1', api_protocol=protocol)
        lease = ModelBudgetLease(SimpleNamespace(mark_model_dispatched=mark),
                                 len(leases) + 1, 4096)
        leases.append(lease)
        plan.model_budget_lease = lease
        return dispatch_model_request(plan, send, task, body, **kwargs)

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    publish = _fallback_events.append_event

    def checked_publish(task, event):
        assert leases[-1].dispatched is True
        assert task['_fallback_kind'] == 'primary_retry'
        assert task['_refusal_skip_left'] == 0
        order.append('publish')
        return publish(task, event)

    monkeypatch.setattr(_fallback_events, 'append_event', checked_publish)
    harness['script'] = [_result('tool_calls')]
    with monkeypatch.context() as patch:
        def reject(task, event):
            checked_publish(task, event)
            if order.count('publish') == fail_event:
                raise failure
        patch.setattr(_fallback_events, 'append_event', reject)
        with pytest.raises(failure_type) as raised:
            _call(task, model='kimi-k3', messages=history)
        assert raised.value is failure
    assert order == ['mark', 'send'] + ['publish'] * fail_event
    assert task['_refusal_skip_left'] == 0
    assert task['_fallback_kind'] == 'primary_retry'
    assert harness['streams'] == [('claude-opus-5.5', 'R7')]
    assert len(leases) == 1
    assert leases[0].dispatched is True
    assert not harness['script']


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
def test_denied_return_probe_has_no_completed_switch_timeline_or_audit(
        harness, monkeypatch, protocol):
    from types import SimpleNamespace
    from lib.agent_core.execution_budget import ModelBudgetLease
    from lib.llm import AbortedError
    from lib.llm._sse_core import prepare_request
    from lib.llm.request import dispatch_model_request
    from lib.logging_pkg import lead_audit
    from lib.tasks_pkg.llm_fallback import _switch
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    task['convId'] = 'coordinator-denied-return'
    refused = [{'role': 'user', 'content': 'original request'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    history = [{'role': 'user', 'content': 'changed request'}]
    audit = []
    monkeypatch.setattr(lead_audit, 'record', lambda *args, **kwargs: audit.append((args, kwargs)))
    leases = []
    rejected = AbortedError('execution_owner_cancelled')

    def reject_dispatch(_ordinal):
        raise rejected

    def stream(task, body, **kwargs):
        plan = prepare_request(dict(body, messages=history, max_tokens=4096),
                               base_url='https://example.invalid/v1', api_protocol=protocol)
        lease = ModelBudgetLease(SimpleNamespace(mark_model_dispatched=reject_dispatch), 1, 4096)
        leases.append(lease)
        plan.model_budget_lease = lease
        return dispatch_model_request(plan, lambda: pytest.fail('denied request must not send'))

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    with pytest.raises(AbortedError) as raised:
        _call(task, model='kimi-k3', messages=history)
    assert raised.value is rejected
    assert leases[0].dispatched is False
    assert task['_refusal_skip_left'] == 1
    assert task['_fallback_kind'] == 'content_filter'
    assert not harness['streams']
    timeline = None
    for event in harness['events']:
        timeline = fold_activity_timeline(timeline, event, task, now_ms=1000)
    assert not [row for row in (timeline or {}).get('entries', [])
                if row.get('status') == 'switched']
    assert not [args for args, _ in audit if args and args[0] == 'model_switch']
    assert not [event for event in harness['events'] if event.get('type') == EventType.MODEL_FALLBACK]


@pytest.mark.parametrize('primary_stop', [False, True])
def test_transport_and_publication_failures_keep_cause_usage_and_stop_priority(
        harness, monkeypatch, primary_stop):
    from lib.error_envelope import from_exception
    from lib.llm import AbortedError
    from lib.tasks_pkg.event_delivery import EventDeliveryError
    from lib.tasks_pkg.llm_fallback import _switch
    from lib.tasks_pkg.turn_retry import should_auto_retry_turn

    task = _task()
    refused = [{'role': 'user', 'content': 'original'}]
    _switch.record_refused_history(task, refused)
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    history = [{'role': 'user', 'content': 'changed'}]
    primary = AbortedError('owner stopped after send') if primary_stop else ConnectionError('provider lost')
    primary.usage = {'prompt_tokens': 12, 'completion_tokens': 3}
    publication = EventDeliveryError('local durable evidence unavailable')
    harness['script'] = [primary]

    def fail_publication(task, event):
        raise publication

    monkeypatch.setattr(_fallback_events, 'append_event', fail_publication)
    with pytest.raises((AbortedError, EventDeliveryError)) as raised:
        _call(task, model='kimi-k3', messages=history)
    if primary_stop:
        assert raised.value is primary
        assert any('EventDeliveryError' in note for note in primary.__notes__)
    else:
        assert raised.value is publication
        assert raised.value.__cause__ is primary
        assert raised.value.usage == primary.usage
        envelope = from_exception(raised.value)
        assert envelope['kind'] == 'event_delivery_failed'
        assert should_auto_retry_turn(envelope, 0) == (False, 0.0)
    assert harness['streams'] == [('claude-opus-5.5', 'R7')]
    assert harness['rescues'] == 0
    assert not harness['script']


@pytest.mark.parametrize('publication_stage', [
    'phase_build', 'event_build', 'phase', 'switch', 'audit', 'log'])
@pytest.mark.parametrize('provider_outcome', ['success', 'failure', 'stop'])
def test_all_model_switch_publication_failures_are_local_and_never_resend(
        harness, monkeypatch, publication_stage, provider_outcome):
    from lib.error_envelope import from_exception
    from lib.llm import AbortedError
    from lib.logging_pkg import lead_audit
    from lib.tasks_pkg.event_delivery import EventDeliveryError
    from lib.tasks_pkg.llm_fallback import _switch
    from lib.tasks_pkg.turn_retry import should_auto_retry_turn

    task = _task()
    task['convId'] = 'coordinator-publication-boundary'
    _switch.record_refused_history(task, [{'role': 'user', 'content': 'original'}])
    task.update(_fallback_model='kimi-k3', _fallback_from='claude-opus-5.5',
                _fallback_kind='content_filter')
    primary = (AbortedError('owner stopped') if provider_outcome == 'stop'
               else ConnectionError('original provider failure'))
    primary.usage = {'prompt_tokens': 12, 'completion_tokens': 3}
    harness['script'] = [_result('stop', content='paid primary answer')
                         if provider_outcome == 'success' else primary]
    publication = OSError('local switch evidence unavailable')
    publish = _fallback_events.append_event
    phase_builder = _switch.build_phase
    event_builder = _switch.build_event

    def build_phase(*args, **kwargs):
        if publication_stage == 'phase_build':
            raise publication
        return phase_builder(*args, **kwargs)

    def build_event(*args, **kwargs):
        if publication_stage == 'event_build':
            raise publication
        return event_builder(*args, **kwargs)

    def append(task, event):
        publish(task, event)
        if ((publication_stage == 'phase' and event['type'] == 'phase')
                or (publication_stage == 'switch' and event['type'] == EventType.MODEL_FALLBACK)):
            raise publication

    def audit(*args, **kwargs):
        if publication_stage == 'audit':
            raise publication

    def log(*args, **kwargs):
        if publication_stage == 'log':
            raise publication

    monkeypatch.setattr(_switch, 'build_phase', build_phase)
    monkeypatch.setattr(_switch, 'build_event', build_event)
    monkeypatch.setattr(_fallback_events, 'append_event', append)
    monkeypatch.setattr(lead_audit, 'record', audit)
    monkeypatch.setattr(_switch.logger, 'info', log)
    rounds = []
    with pytest.raises((EventDeliveryError, AbortedError)) as raised:
        _call(task, model='kimi-k3', messages=[{'role': 'user', 'content': 'changed'}],
              api_rounds=rounds)
    if provider_outcome == 'stop':
        assert raised.value is primary
    else:
        assert isinstance(raised.value, EventDeliveryError)
        envelope = from_exception(raised.value)
        assert envelope['kind'] == 'event_delivery_failed'
        assert should_auto_retry_turn(envelope, 0) == (False, 0.0)
        if provider_outcome == 'success':
            assert len(rounds) == 1 and rounds[0]['model'] == 'claude-opus-5.5'
        else:
            assert raised.value.__cause__ is primary
            assert raised.value.usage == primary.usage
    assert harness['streams'] == [('claude-opus-5.5', 'R7')]
    assert not harness['script']
    assert harness['rescues'] == 0
    assert task['_fallback_kind'] == 'primary_retry'
    switches = [event for event in harness['events'] if event['type'] == EventType.MODEL_FALLBACK]
    assert len(switches) <= 1
    assert all(event['fallbackKind'] == 'primary_retry' for event in switches)


@pytest.mark.api
@pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)
def test_paid_switch_delivery_failure_settles_the_attempt_and_recovery_never_redispatches(
        claimed, harness, monkeypatch):
    from types import SimpleNamespace
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.tasks_pkg.event_delivery import EventDeliveryError
    from lib.tasks_pkg.llm_fallback import _switch
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root
    from lib.tasks_pkg.orchestrator import _llm_round_call as round_call
    from lib.tasks_pkg.orchestrator import _post_loop as post

    context, pair, _factory, owner = claimed
    entered, ledgers = [], []
    harness['script'] = [_result('stop', content='paid primary answer')]
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])
    monkeypatch.setattr(root, 'prepare_round_context', lambda *a, **kw: None)
    monkeypatch.setattr(root, 'admit_round_context', lambda *a, **kw: {})
    monkeypatch.setattr(root, 'build_round_request',
                        lambda task, state, messages, tools, **kw: (tools, {'model': state.model}))
    monkeypatch.setattr(root, 'build_stream_accumulator',
                        lambda *a, **kw: SimpleNamespace(on_tool_call_ready=None, close=lambda **kw: None))
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait.checkpoint_model_dispatch', lambda *a, **kw: None)
    monkeypatch.setattr(round_call, 'confirm_consumed_project_context', lambda *a, **kw: None)
    monkeypatch.setattr(round_call, 'flush_deferred_peer_and_steer', lambda *a, **kw: None)

    def forbid_retry(*args):
        pytest.fail('delivery failure must not regenerate or redispatch the paid request')

    def audit(*args, **kwargs):
        raise OSError('local audit unavailable after paid request')

    monkeypatch.setattr(post, '_maybe_auto_retry_turn', forbid_retry)
    monkeypatch.setattr(_switch, '_lead_audit_switch', audit)

    def run(task):
        entered.append(task['id'])
        history = [{'role': 'user', 'content': 'changed accepted input'}]
        _switch.record_refused_history(task, [{'role': 'user', 'content': 'original'}])
        task.update(_fallback_model='kimi-k3', _fallback_from='controlled',
                    _fallback_kind='content_filter')
        state = SimpleNamespace(model='kimi-k3', preset='medium', thinking_enabled=True,
                                tool_call_happened=True, accumulated_usage={}, api_rounds=[], last_usage=None)
        request = SimpleNamespace(task=task, state=state, messages=history, tid=task['id'], cfg={},
                                  project_path='', project_enabled=False, thinking_depth=None,
                                  temperature=None, max_tokens=4096, response_format=None)
        with pytest.raises(EventDeliveryError) as raised:
            root._RootLoopHooks(request)._dispatch_open_round(6, [])
        ledgers.append(list(state.api_rounds))
        assert post.handle_task_fatal(task, raised.value) is False
        assert task['error']['kind'] == 'event_delivery_failed'
        assert task['error']['source'] == 'task-event-delivery'

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)
    outcome = execute_claimed_attempt(context, context.claim.payload)
    assert outcome.status == 'failed'
    assert entered == [context.claim.task_id]
    assert harness['streams'] == [('controlled', 'R7')]
    assert not harness['script'] and harness['rescues'] == 0
    assert len(ledgers[0]) == 1 and ledgers[0][0]['model'] == 'controlled'
    assert ledgers[0][0]['usage']['prompt_tokens'] == 250_000
    assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] == 'failed'
    chat_task_runtime.discard(context.claim.task_id)
    assert execute_claimed_attempt(context, context.claim.payload) == outcome
    assert entered == [context.claim.task_id]
    assert harness['streams'] == [('controlled', 'R7')]
