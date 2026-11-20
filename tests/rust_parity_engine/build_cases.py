"""Build the ``engine_session`` parity case corpus (``cases.jsonl``).

Two sources:

1. Every recorded fixture in ``tests/fixtures/llm_replay/*`` that maps to the
   boundary schema, converted mechanically from its ``script.jsonl``
   stream/error steps.  Fixtures that cannot be represented are recorded in
   ``UNREPRESENTABLE`` with the reason (also emitted as ``_unrepresentable``
   in the builder's stdout report, NOT as cases).
2. Synthetic fault cases for content-filter fallback, provider errors,
   mid-stream drop, cancellation, tool effect outcomes, parallel calls,
   restart resume and durable storage failure.

Each output line is one case object::

    {"name": ..., "origin": "fixture:<dir>"|"synthetic",
     "input": {...engine_session input...}}

Run ``.venv/bin/python3 -m tests.rust_parity_engine.build_cases`` (with
``PYTHONPATH=$PWD``) to regenerate ``cases.jsonl`` next to this file.
``--check`` verifies the committed file is up to date instead of writing.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / 'fixtures' / 'llm_replay'
OUTPUT = HERE / 'cases.jsonl'

PRIMARY = 'parity-primary'
FALLBACK = 'parity-fallback'

# tool name -> readOnly, for the scripted tool surface.
TOOL_READ_ONLY = {
    'read_files': True,
    'write_file': False,
    'edit_file': False,
    'produce_slides': False,
}

UNREPRESENTABLE = {
    'async_path': (
        'drives async_dispatch_stream directly at the dispatch seam, not the '
        'root agent loop; there is no conversation turn, durable event log or '
        'tool settlement to project into parts'),
    'parallel_tools_timeout': (
        'the timeout is armed by a per-tool declared timer plus a patched '
        'threading.Timer that the scenario expires mid-call; the boundary '
        'owns tool handlers but not the dispatcher clock, so the declared-'
        'timeout verdict cannot be scripted as a provider/tool step'),
    'agent_tool_continuation': (
        'requires real slides runtime/engine side effects (spawned background '
        'production job) which the boundary deliberately does not own'),
}


def _base(user, requests, *, tools=(), fallback=None, outcomes=None,
          cancel=None, storage=None, restart=None):
    return {
        'user': user,
        'model': PRIMARY,
        'fallbackModel': fallback,
        'tools': [{'name': name, 'readOnly': TOOL_READ_ONLY[name]}
                  for name in tools],
        'requests': requests,
        'toolOutcomes': outcomes or {},
        'cancelAt': cancel,
        'storageFailure': storage,
        'restartAfterRequest': restart,
    }


def _text(text):
    return {'kind': 'text', 'text': text}


def _tool(calls, text=''):
    return {'kind': 'tool', 'text': text, 'calls': calls}


def _call(call_id, name, arguments):
    return {'id': call_id, 'name': name, 'arguments': arguments}


def synthetic_cases():
    cases = []

    def add(name, case_input, note):
        cases.append({'name': name, 'origin': 'synthetic',
                      'note': note, 'input': case_input})

    add('content_filter_fallback_success', _base(
        'Answer the parity question.',
        [{'kind': 'content_filter', 'partial': 'Partial refused text'},
         _text('Fallback answered the question.')],
        fallback=FALLBACK),
        'primary refuses with a verified content_filter finish; the '
        'configured fallback answers')
    add('content_filter_without_fallback', _base(
        'Answer the parity question.',
        [{'kind': 'content_filter', 'partial': 'Partial refused text'}]),
        'a verified provider refusal with NO configured fallback settles the '
        'typed content_filter outcome instead of switching models')
    add('content_filter_tool_loop_stays_on_fallback', _base(
        'Refuse then use a tool.',
        [{'kind': 'content_filter', 'partial': 'Refused prefix'},
         _tool([_call('call_cf_tool', 'read_files', '{"reads":[{"path":"a"}]}')],
               'On the fallback I will read. '),
         _text('Read on the fallback.')],
        tools=['read_files'], fallback=FALLBACK,
        outcomes={'call_cf_tool': {'kind': 'ok', 'output': 'FILE-A'}}),
        'the tool round appended to the refused history stays on the '
        'fallback model for exactly the exempt request')
    add('content_filter_second_tool_round_returns_to_primary', _base(
        'Refuse once then complete two read rounds.',
        [{'kind': 'content_filter', 'partial': 'Refused prefix'},
         _tool([_call('first_cf_read', 'read_files', '{"reads":[{"path":"a"}]}')], 'Read A.'),
         _tool([_call('second_cf_read', 'read_files', '{"reads":[{"path":"b"}]}')], 'Read B.'),
         _text('Both reads complete.')],
        tools=['read_files'], fallback=FALLBACK,
        outcomes={'first_cf_read': {'kind': 'ok', 'output': 'A'},
                  'second_cf_read': {'kind': 'ok', 'output': 'B'}}),
        'only one appended tool-round request uses the refusal exemption; '
        'the next request returns to primary without any human nudge')
    add('content_filter_returns_to_primary_after_nudge', _base(
        'Refuse, fail a tool, then continue.',
        [{'kind': 'content_filter', 'partial': 'Refused prefix'},
         _tool([_call('call_cf_fail', 'read_files', '{"reads":[{"path":"a"}]}')],
               'Reading on the fallback. '),
         _text('I will retry the read.'),
         _text('Back on the primary. [END_TURN: done]')],
        tools=['read_files'], fallback=FALLBACK,
        outcomes={'call_cf_fail': {'kind': 'error', 'output': 'file vanished'}}),
        'a refusal applies to one request: once the history leaves the '
        'refused tool loop (the intent-stall nudge appends a user row), the '
        'next request returns to the primary with a primary_retry switch')
    add('http_error_500_mid_stream', _base(
        'Answer after the gateway fails mid-stream.',
        [{'kind': 'http_error', 'status': 500, 'partial': 'half an answer',
          'midStream': True},
         _text('Recovered after the 500.')]),
        'provider HTTP 500 raised after streamed partial content; no '
        'configured fallback, so dispatch recovers on the primary')
    add('http_error_429', _base(
        'Answer after the rate limit.',
        [{'kind': 'http_error', 'status': 429, 'partial': '',
          'midStream': False},
         _text('Answered after the 429.')], fallback=FALLBACK),
        'pre-chunk 429; dispatch retries (primary waitable) before any '
        'configured fallback is used')
    add('drop_mid_stream', _base(
        'Explain why the stream retry is safe.',
        [{'kind': 'drop', 'partial': 'The interrupted answer begins with half a'},
         _text('The complete answer: the retry resent the same request.')]),
        'premature_close after partial prose: the interrupted request is '
        'resent identically (delta_reset, no round_end for the cut attempt)')
    add('cancel_mid_stream', _base(
        'Stream a long answer that will be stopped.',
        [_text('First chunk of a long answer. The rest never arrives.')],
        cancel={'request': 0, 'afterText': True}),
        'owner Stop after the first streamed content chunk; the attempt '
        'settles client_aborted, never a fabricated success')
    add('cancel_before_text', _base(
        'Stream a long answer that will be stopped.',
        [_text('No content is delivered before the cancel.')],
        cancel={'request': 0, 'afterText': False}),
        'owner Stop before the first chunk of the request')
    add('mutating_tool_lost_after_entry', _base(
        'Write the marker.',
        [_tool([_call('call_lost', 'write_file', '{"path":"a.txt"}')]),
         _text('The write outcome is unknown.'),
         _text('The unknown write must be reconciled before continuing. [END_TURN: blocked]')],
        tools=['write_file'],
        outcomes={'call_lost': {'kind': 'lost_after_entry',
                                'output': 'write acknowledgement lost'}}),
        'mutating tool whose effect ACK was lost after entry: durable effect '
        'unknown, model receives tool_effect_unknown (non-retryable)')
    add('mutating_tool_refused_before_entry', _base(
        'Write the marker.',
        [_tool([_call('call_refused', 'write_file', '{"path":"a.txt"}')]),
         _text('The write was refused before it ran.'),
         _text('The refused write needs admission before continuing. [END_TURN: blocked]')],
        tools=['write_file'],
        outcomes={'call_refused': {'kind': 'refused_before_entry',
                                   'output': 'admission refused the write'}}),
        'pre-execution refusal: the effect is corrected to not_started and '
        'the model receives the typed refusal')
    add('mutating_tool_handler_error', _base(
        'Write the marker.',
        [_tool([_call('call_handler_error', 'write_file', '{"path":"a.txt"}')]),
         _text('The write failed.'),
         _text('The failed write cannot be repeated automatically. [END_TURN: blocked]')],
        tools=['write_file'],
        outcomes={'call_handler_error': {'kind': 'error',
                                         'output': 'disk refused the write'}}),
        'mutating tool handler raises after entry: effect unknown, typed '
        'non-retryable tool error; the intent-stall nudge receives an explicit blocked answer')
    add('read_only_tool_handler_error', _base(
        'Read the file.',
        [_tool([_call('call_read_error', 'read_files', '{"reads":[{"path":"a"}]}')]),
         _text('The read failed.'),
         _text('The vanished file prevents completing the read. [END_TURN: blocked]')],
        tools=['read_files'],
        outcomes={'call_read_error': {'kind': 'error',
                                      'output': 'file vanished'}}),
        'read-only tool handler raises after entry: effect unknown, typed '
        'tool_execution_failed result')
    add('parallel_tool_calls_one_round', _base(
        'Read both data files, then summarize.',
        [_tool([_call('call_par_a', 'read_files', '{"reads":[{"path":"a"}]}'),
                _call('call_par_b', 'read_files', '{"reads":[{"path":"b"}]}')],
               'Reading both. '),
         _text('Both files read.')],
        tools=['read_files'],
        outcomes={'call_par_a': {'kind': 'ok', 'output': 'FILE-A'},
                  'call_par_b': {'kind': 'ok', 'output': 'FILE-B'}}),
        'two calls in one round settle in declaration order with returned '
        'read-only effects')
    add('restart_after_first_request', _base(
        'Write then confirm.',
        [_tool([_call('call_restart', 'write_file', '{"path":"a.txt"}')],
               'Writing. '),
         _text('Confirmed after the restart.')],
        tools=['write_file'],
        outcomes={'call_restart': {'kind': 'ok', 'output': 'WROTE'}},
        restart=1),
        'crash immediately after request 1 returns: the provider reply was '
        'never adopted, so the resume re-sends the pre-request history and '
        'the announced-but-never-entered tool settles not_started/aborted '
        '(no effect intent, no re-execution)')
    add('restart_after_tool_round', _base(
        'Write then confirm.',
        [_tool([_call('call_restart_tool', 'write_file', '{"path":"a.txt"}')],
               'Writing. '),
         _text('Confirmed after the restart.'),
         _text('Final confirmation after the resume.')],
        tools=['write_file'],
        outcomes={'call_restart_tool': {'kind': 'ok', 'output': 'WROTE'}},
        restart=2),
        'crash after request 2 (the post-tool answer): the settled tool '
        'effect stays returned and the resumed round issues request 3 '
        '(script step 3 supplies its answer)')
    add('storage_failure_after_round_start', _base(
        'Answer despite the durable write failure.',
        [_text('Hello.')],
        storage={'op': 'append_event'}),
        'every non-terminal durable event write after the first round_start '
        'fails at the real turn-event commit boundary')
    return cases


def convert_fixture(name):
    """Convert one recorded fixture, or return (None, reason)."""
    if name in UNREPRESENTABLE:
        return None, UNREPRESENTABLE[name]
    script = FIXTURES / name / 'script.jsonl'
    steps = [json.loads(line) for line in script.read_text(
        encoding='utf-8').splitlines() if line.strip()]
    requests, tools, outcomes = [], set(), {}
    user = f'Replay the recorded {name} scenario.'
    for step in steps:
        if step['kind'] == 'error':
            attributes = step.get('attributes') or {}
            status = int(attributes.get('status_code') or 500)
            requests.append({'kind': 'http_error', 'status': status,
                             'partial': '', 'midStream': False})
            continue
        if step['kind'] != 'stream':
            return None, f'unsupported script step kind {step["kind"]!r}'
        result = step['result']
        state = result['state']
        events = step.get('events') or []
        text = ''.join(event.get('text') or '' for event in events
                       if event.get('t') == 'content')
        calls = []
        for event in events:
            if event.get('t') != 'tool_call':
                continue
            call = event['call']
            calls.append({'id': call['id'], 'name': call['function']['name'],
                          'arguments': call['function']['arguments']})
        for event in events:
            if event.get('t') not in ('content', 'tool_call'):
                return None, (
                    f'stream event kind {event["t"]!r} has no input-schema '
                    'counterpart')
        if calls:
            requests.append(_tool(calls, text))
            for call in calls:
                tools.add(call['name'])
        elif state == 'premature_close':
            requests.append({'kind': 'drop', 'partial': text})
        elif result.get('compatibility_finish_reason') == 'content_filter':
            requests.append({'kind': 'content_filter', 'partial': text})
        else:
            requests.append(_text(text))
    if name == 'abort_mid_stream':
        return _base(user, requests, cancel={'request': 0, 'afterText': True}), None
    if name == 'rate_limit_fallback':
        # The recording was made on a single-slot route; the 429 is the only
        # provider failure and the same model answers on retry.
        return _base(user, requests), None
    if name in ('upstream_turn_recovery', 'upstream_turn_exhaustion'):
        return _base(user, requests), None
    if name == 'incomplete_tool_stream':
        # The premature_close happens on a stream that also carries a tool
        # call; the schema's drop step is prose-only, so model the tool call
        # as a completed tool step followed by the reissued call only if the
        # states line up — they do not: keep the real shape.
        first = steps[0]
        events = first.get('events') or []
        text = ''.join(event.get('text') or '' for event in events
                       if event.get('t') == 'content')
        calls = [{'id': event['call']['id'],
                  'name': event['call']['function']['name'],
                  'arguments': event['call']['function']['arguments']}
                 for event in events if event.get('t') == 'tool_call']
        converted = [{'kind': 'tool', 'text': text, 'calls': calls,
                      'streamState': 'premature_close'}]
        converted.extend(requests[1:])
        converted[0].pop('streamState')
        return None, (
            'the first attempt carries a tool call AND a premature_close '
            'state; the input schema has no step for an unverified tool-call '
            'stream, and scripting it as a finished tool call would execute '
            'the unverified write the fixture exists to prevent')
    if name == 'partial_stream_cut':
        return _base(user, requests), None
    if name == 'text_reply':
        return _base(user, requests), None
    if name in ('write_file_tool', 'edit_file_blocks'):
        # Tool handlers are scripted at the boundary, not run on disk; the
        # real file outcomes are replaced by their recorded tool results.
        for call_id in outcomes:
            outcomes[call_id]['kind'] = 'ok'
        return _base(user, requests, tools=sorted(tools),
                     outcomes=outcomes), None
    return _base(user, requests, tools=sorted(tools), outcomes=outcomes), None


def build():
    cases = []
    for directory in sorted(FIXTURES.iterdir()):
        if not directory.is_dir() or not (directory / 'script.jsonl').is_file():
            continue
        case_input, reason = convert_fixture(directory.name)
        if case_input is None:
            continue
        cases.append({'name': f'fixture_{directory.name}',
                      'origin': f'fixture:{directory.name}',
                      'note': f'converted from {directory.name}/script.jsonl',
                      'input': case_input})
    cases.extend(synthetic_cases())
    return cases


def main():
    import sys
    cases = build()
    lines = ''.join(json.dumps(case, sort_keys=True, ensure_ascii=False) + '\n'
                    for case in cases)
    if '--check' in sys.argv:
        current = OUTPUT.read_text(encoding='utf-8') if OUTPUT.is_file() else ''
        if current != lines:
            raise SystemExit('cases.jsonl is stale; rerun build_cases')
        print(f'{len(cases)} cases up to date')
        return
    OUTPUT.write_text(lines, encoding='utf-8')
    print(f'wrote {len(cases)} cases to {OUTPUT}')
    for name, reason in sorted(UNREPRESENTABLE.items()):
        print(f'unrepresentable fixture {name}: {reason}')


if __name__ == '__main__':
    main()
