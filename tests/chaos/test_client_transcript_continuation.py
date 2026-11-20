"""A client read reconnect consumes retained output, never execution authority."""
from __future__ import annotations

from copy import deepcopy
import threading
import uuid

import pytest

from lib.turn_projection_patch import apply_projection_patch
from tests.chaos import fake_provider as fp
from tests.chaos.client import EventStream
from tests.chaos.test_chaos_faults import _running_chaos

pytestmark = [pytest.mark.api, pytest.mark.timeout(600)]


@pytest.fixture(scope='module', params=['anthropic', 'openai', 'openai_responses'])
def chaos(request, tmp_path_factory):
    with _running_chaos(tmp_path_factory.mktemp('reconnect-' + request.param),
                        protocol=request.param) as harness:
        yield harness


def _apply_attempt_change(projection, revision, data, turn_id):
    attempt_event = data['payload'].get('event')
    if not attempt_event or attempt_event['turnId'] != turn_id:
        return projection, revision
    patch = attempt_event['payload'].get('projectionPatch')
    if patch is None:
        return projection, revision
    assert patch['baseRevision'] == revision
    return apply_projection_patch(projection, patch), patch['targetRevision']


def test_settled_local_tool_result_reconnects_once_in_canonical_order(chaos):
    release = threading.Event()
    marker = uuid.uuid4().hex
    final = 'The local fixture result is retained.'
    conversation, nonce, user_text, _ = chaos.start_turn([
        fp.hold('Preparing the local fixture.', release,
                fp.tool('run_command', {'command': chaos.effect_command(marker)})),
        fp.text(final),
    ])
    stream = None
    try:
        assert chaos.provider.held.wait(30), 'provider never reached the controlled pre-tool boundary'
        held_requests = chaos.provider.requests(nonce)
        assert len(held_requests) == 1 and held_requests[0]['step'] == 'hold'
        assert held_requests[0]['protocol'] == chaos.provider.protocol
        before = chaos.client.snapshot(conversation)
        assistant = next(t for t in before['turns'] if t['actor'] == 'assistant')
        projection = deepcopy(assistant['projection'])
        revision = assistant['projectionRevision']
        stream = EventStream(chaos.instance.base_url, conversation, after=before['cursor'])
        # The first subscription catches up without admitting another execution.
        initial_cursors = []
        for cursor, event, data in stream.events():
            if event == 'sync.heartbeat':
                assert cursor
                break
            assert event != 'sync.reset_required', data
            initial_cursors.append(cursor)
            projection, revision = _apply_attempt_change(
                projection, revision, data, assistant['turnId'])
        else:
            pytest.fail('initial read stream never confirmed its retained cursor')
        stream.close()
        stream = None
        assert chaos.effect_count(marker) == 0
        assert chaos.provider.requests(nonce) == held_requests
        release.set()
        settled = chaos.assert_contract(conversation, user_text, outcomes={'completed'},
                                        final_text=final, effects={marker: 1})
        requests = chaos.provider.requests(nonce)
        assert requests[0] == held_requests[0]
        assert [request['step'] for request in requests] == ['hold', 'text']
        provider_results = fp.result_texts(requests[1]['tool_results'])
        assert len(provider_results) == 1
        assert provider_results[0].splitlines().count('effect-recorded') == 1
        durable_before_read = chaos.client.snapshot(conversation)
        stream = EventStream(chaos.instance.base_url, conversation, after=cursor)
        sequences, cursors, results = [], [], []
        for event_cursor, event, data in stream.events():
            assert event != 'sync.reset_required', data
            if event == 'sync.heartbeat':
                break
            sequences.append(data['syncSeq'])
            cursors.append(event_cursor)
            projection, revision = _apply_attempt_change(
                projection, revision, data, assistant['turnId'])
            rounds = [r for r in projection.get('toolRounds', []) if r.get('status') == 'done']
            if rounds:
                assert len(rounds) == 1
                if 'toolContent' in rounds[0]:
                    results.append((rounds[0]['toolCallId'], rounds[0]['toolContent']))
        else:
            pytest.fail('reconnected stream never caught up')
        assert sequences and sequences == sorted(set(sequences))
        assert len(cursors) == len(set(cursors)) and cursor not in cursors
        assert not set(cursors) & set(initial_cursors)
        assert revision == settled['projectionRevision']
        assert projection == settled['projection']
        assert results and all(result == results[0] for result in results)
        assert results[0] == (projection['toolRounds'][0]['toolCallId'], provider_results[0])
        rounds = projection['toolRounds']
        assert len(rounds) == 1 and rounds[0]['toolName'] == 'run_command'
        assert rounds[0]['toolContent'] == provider_results[0]
        assert rounds[0]['toolContent'].splitlines().count('effect-recorded') == 1
        segments = projection['segments']
        tool_indices = [i for i, segment in enumerate(segments) if segment['type'] == 'tool_use']
        final_indices = [i for i, segment in enumerate(segments)
                         if segment['type'] == 'text' and final in (
                             projection['content'] if segment.get('terminal') else segment.get('text', ''))]
        assert len(tool_indices) == len(final_indices) == 1
        assert tool_indices[0] < final_indices[0]
        assert chaos.client.snapshot(conversation) == durable_before_read
        assert chaos.effect_count(marker) == 1
        assert chaos.provider.requests(nonce) == requests
    finally:
        release.set()
        if stream is not None:
            stream.close()
