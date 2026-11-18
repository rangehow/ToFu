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
from lib.llm_dispatch.credential_reservation import active_reservations, reservations
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
             'script': [], 'fallback': 'kimi-k3', 'reservations': []}

    def fake_stream(task, body, tag='', on_tool_call_ready=None, **_kwargs):
        state['streams'].append((body.get('model'), tag))
        state['reservations'].append(active_reservations())
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


def test_refusal_fallback_and_its_tool_rounds_avoid_reserved_credentials(harness):
    harness['script'] = [
        _result('content_filter', content='Partial refused text', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
    ]
    result = _call(_task())

    # The primary attempt is unrestricted; the fallback attempt is scoped.
    assert harness['reservations'] == [None, reservations()]
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
    # The refused attempt's own tool round stays on the fallback, still scoped.
    assert harness['reservations'][-1] == reservations()

    task = _task()
    task['_fallback_model'] = 'kimi-k3'
    task['_fallback_from'] = 'claude-opus-5.5'
    task['_fallback_kind'] = 'user_switch'
    harness['script'] = [_result('stop', content='Deliberate switch.')]
    _call(task, model='kimi-k3')
    # The owner's own model choice is not fallback traffic.
    assert harness['reservations'][-1] is None


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
    """Only the refused attempt's own tool rounds skip the return probe.

    Between two provider requests of one attempt the tool loop appends the
    served assistant tool-call row and its tool result — no new request. A
    repeated refusal on that same history would otherwise bill a primary
    probe on every round. The NEXT user message ends that exemption.
    """
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='Fallback answer.'),
        _result('stop', content='Still fallback.'),   # appended tool rows → kimi
        _result('stop', content='Primary again.'),    # next request → opus
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

    Refuse at round k, then append three fallback rounds (assistant + tool
    result each). The primary must NOT be retried on any of them. Rewriting an
    old tool result (clearing) DOES rewrite the refused prefix → one retry.
    """
    harness['script'] = [
        _result('content_filter', content='Partial', refusal=_REFUSAL),
        _result('stop', content='fallback r6'),
        _result('stop', content='fallback r7'),
        _result('stop', content='fallback r8'),
        _result('stop', content='fallback r9'),
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

    # Appending must not trigger a primary retry: exactly one opus call so far.
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'kimi-k3', 'kimi-k3']
    assert [e['fallbackModel'] for e in harness['events']
            if e.get('type') == EventType.MODEL_FALLBACK] == ['kimi-k3']

    # Clearing rewrites an OLD tool result inside the refused prefix.
    history = [dict(row) for row in history]
    history[refused_len - 1] = {
        'role': 'tool', 'tool_call_id': 'c0',
        'content': '[old tool result cleared]'}
    _call(task, model='kimi-k3', round_num=10,
          messages=history, api_rounds=api_rounds)
    assert [m for m, _ in harness['streams']] == [
        'claude-opus-5.5', 'kimi-k3', 'kimi-k3', 'kimi-k3', 'kimi-k3',
        'claude-opus-5.5']
    # A compaction that shortens the history below the refused length also retries.
    harness['script'].append(_result('stop', content='primary after compaction'))
    task['_fallback_model'] = 'kimi-k3'
    task['_fallback_from'] = 'claude-opus-5.5'
    task['_fallback_kind'] = 'content_filter'
    _call(task, model='kimi-k3', round_num=11,
          messages=history[:1] + [{'role': 'user', 'content': 'compacted summary'}],
          api_rounds=api_rounds)
    assert harness['streams'][-1][0] == 'claude-opus-5.5'
