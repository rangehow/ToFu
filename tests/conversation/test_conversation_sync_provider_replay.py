"""providerReplayPayload=none: the generated browser's evidence-free representation.

Snapshots, history pages and both event streams omit provider replay evidence
by path, for live Turns too; the default representation is unchanged, and a
whole-projection edit built from the view keeps every stored signature.
"""
import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.conversation_sync.cursor import ReplayPosition
from lib.conversation_sync.service import ConversationSyncService
from lib.conversation_sync.validation import decode
from lib.turn_projection_patch import build_projection_patch
from lib.turn_projection_segments import snapshot_with_reference_tool_segments
from tests.support.conversation_replay import SCRIPTED_EPOCH, scripted_digest, with_lineage

pytest_plugins = ("tests._chat_sidecar",)

SIGNATURE = 'sig-' + 's' * 4_000
EVIDENCE = ('thinkingSignature', '_anthropicContentBlocks', '_responsesItems')


def _round(call_id='call', thinking='plan', signature=SIGNATURE):
    return {'toolCallId': call_id, 'toolName': 'search_files', 'status': 'done',
            'toolArgs': {'q': 'x'}, 'toolContent': 'hit', 'llmRound': 0,
            'thinking': thinking, 'thinkingSignature': signature,
            '_anthropicContentBlocks': [{'type': 'thinking', 'thinking': thinking,
                                         'signature': signature}]}


def _projection(rounds):
    return {
        'content': 'answer', 'toolRounds': rounds,
        'segments': [
            {'type': 'thinking', 'blockId': 'thinking:llm-0', 'text': 'plan',
             'signature': SIGNATURE, 'llmRound': 0},
            {'type': 'tool_use', 'blockId': 'tool:call', 'id': rounds[0]['toolCallId'],
             'name': 'search_files', 'input': {'q': 'x'},
             'result': {'content': 'hit', 'status': 'done'}, 'llmRound': 0},
            {'type': 'text', 'blockId': 'text:terminal', 'text': 'answer', 'terminal': True},
        ],
    }


def _has_evidence(value) -> bool:
    text = json.dumps(value)
    return any(f'"{field}"' in text for field in (*EVIDENCE, 'signature'))


@pytest.mark.unit
def test_snapshot_view_omits_evidence_from_live_turns_only_when_requested():
    live = {'conversationId': 'c', 'turnId': 'live', 'presentationId': 'live', 'status': 'running',
            'projectionRevision': 4, 'projection': _projection([_round()])}
    snapshot = {'conversationId': 'c', 'turns': [live]}
    before = deepcopy(snapshot)
    exact = snapshot_with_reference_tool_segments(snapshot, owner_cache_scope='a' * 24)
    assert exact['turns'][0]['projection'] is live['projection']
    view = snapshot_with_reference_tool_segments(snapshot, owner_cache_scope='a' * 24,
                                                provider_replay='none')
    assert not _has_evidence(view['turns'][0]['projection'])
    assert view['turns'][0]['projection']['toolRounds'][0]['thinking'] == 'plan'
    assert snapshot == before


@pytest.mark.unit
def test_stream_view_drops_evidence_operations_and_keeps_order_and_revisions():
    before = _projection([_round()])
    after = deepcopy(before)
    after['toolRounds'].append(_round('call-2', 'next plan', 'sig-next'))
    after['segments'][0]['signature'] = 'sig-rotated'
    after['content'] = 'answer and more'
    patch = build_projection_patch(before, after, base_revision=4, target_revision=5)
    attempt = {'attemptId': 'attempt', 'turnId': 'turn', 'conversationId': 'conv', 'seq': 3,
               'projectionRevision': 5, 'type': 'projection_updated',
               'payload': {'status': 'running', 'phase': None, 'projectionPatch': patch}}
    change = {'contract': 'tofu.conversation-sync.event/v1', 'type': 'attempt.event',
              'conversationId': 'conv', 'syncSeq': 9, 'occurredAt': 1, 'payload': {'event': attempt}}
    stored = with_lineage({'head': 9, 'events': [change], 'hasMore': False}, 8)
    original = deepcopy(stored)
    service = ConversationSyncService(SimpleNamespace(changes=lambda *_a, **_k: stored))
    position = ReplayPosition(8, SCRIPTED_EPOCH, scripted_digest(8))

    projected = service.changes('conv', 7, after=position, diagnostic_payload='none',
                                provider_replay_payload='none')
    event = projected['events'][0]
    view_patch = event['payload']['event']['payload']['projectionPatch']
    assert (view_patch['baseRevision'], view_patch['targetRevision']) == (4, 5)
    assert not _has_evidence(view_patch)
    assert any(operation['path'] == ['content'] for operation in view_patch['operations'])
    assert event['syncSeq'] == 9 and stored == original
    decode('ConversationChange', event)
    decode('AttemptEvent', event['payload']['event'])
    assert service.changes('conv', 7, after=position)['events'] == original['events']
    with pytest.raises(ValueError, match='provider replay'):
        service.changes('conv', 7, after=position, provider_replay_payload='partial')


@pytest.mark.unit
def test_snapshot_and_page_reads_require_refs_for_the_view():
    def forbidden(*_args, **_kwargs):
        pytest.fail('invalid representation reached storage')
    service = ConversationSyncService(SimpleNamespace(snapshot=forbidden, turn_page=forbidden))
    with pytest.raises(ValueError, match='reference segments'):
        service.snapshot('c', 1, provider_replay_payload='none')
    with pytest.raises(ValueError, match='reference segments'):
        service.turn_page('c', 1, lane_id='main', expected_sync_sequence=0,
                          provider_replay_payload='none')


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_view_round_trip_restores_evidence_of_ambiguous_rounds(flask_client, chat_sidecar, monkeypatch):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.storage import get_storage_client
    from routes import conversation_sync_v3 as routes
    client = get_storage_client(write=True)
    cid = 'replay-view-' + uuid.uuid4().hex
    service = ConversationSyncService(SidecarConversationSyncRepository(
        client_factory=lambda write=False: client))
    monkeypatch.setattr(routes, '_service', service)
    client.command('conversation.create', {'conv_id': cid, 'user_id': 1, 'title': 'Replay view',
                   'created_at': 1, 'updated_at': 1, 'settings': {}}, cid + '-create')
    rounds = [_round('reused', 'plan'), _round('reused', 'second plan', 'sig-second')]
    try:
        client.command('turn.append_settled', {'conversation_id': cid, 'user_id': 1,
                       'command_id': cid + '-append', 'actor': 'assistant', 'kind': 'fixture',
                       'projection': _projection(rounds), 'status': 'interrupted',
                       'created_at': 2}, cid + '-append')
        root = f'/api/v4/conversations/{cid}'
        refs = {'segmentPayload': 'refs', 'toolPayload': 'lazy'}
        inline = flask_client.get(root + '/sync', query_string=refs).get_json()
        # The default refs view keeps ambiguous replay evidence inline.
        assert inline['turns'][0]['projection']['toolRounds'][1]['thinkingSignature'] == 'sig-second'
        view = flask_client.get(root + '/sync', query_string={
            **refs, 'providerReplayPayload': 'none'}).get_json()
        turn = view['turns'][0]
        assert not _has_evidence(turn['projection'])
        for query in ({'providerReplayPayload': 'none'}, {**refs, 'providerReplayPayload': 'x'}):
            assert flask_client.get(root + '/sync', query_string=query).status_code == 400
        browser = turn['projection']
        materialized = [
            {**segment, 'input': {'q': 'x'}, 'result': {'content': 'hit', 'status': 'done'}}
            if segment.get('roundRef') else segment for segment in browser['segments']]
        for segment in materialized:
            segment.pop('roundRef', None)
        response = flask_client.patch(f"{root}/turns/{turn['turnId']}", json={
            'expectedProjectionRevision': turn['projectionRevision'],
            'projection': {**browser, 'segments': materialized, 'content': 'edited'}})
        assert response.status_code == 200, response.get_json()
        stored = flask_client.get(f"{root}/turns/{turn['turnId']}").get_json()['turn']['projection']
        assert stored['content'] == 'edited'
        for source, result in zip(rounds, stored['toolRounds'], strict=True):
            assert {field: result.get(field) for field in EVIDENCE[:2]} == {
                field: source[field] for field in EVIDENCE[:2]}
        assert next(s for s in stored['segments'] if s['type'] == 'thinking')['signature'] == SIGNATURE
    finally:
        client.command('conversation.purge', {'conv_id': cid, 'user_id': 1}, cid + '-purge')


@pytest.mark.unit
@pytest.mark.parametrize('query,expected', [
    ('', {}), ('?providerReplayPayload=none', {'provider_replay_payload': 'none'}),
    ('?providerReplayPayload=full', {}),
    ('?diagnosticPayload=none&providerReplayPayload=none',
     {'diagnostic_payload': 'none', 'provider_replay_payload': 'none'}),
])
def test_stream_options_decode_the_view_selector(query, expected):
    from quart import Quart
    from routes import conversation_sync_v3 as routes

    async def decode_options():
        async with Quart(__name__).test_request_context('/events' + query):
            return routes._diagnostic_options()

    import asyncio
    assert asyncio.run(decode_options()) == expected


@pytest.mark.unit
@pytest.mark.parametrize('query', ['?providerReplayPayload=invalid', '?providerReplayPayload=',
                                   '?providerReplayPayload=none&providerReplayPayload=full'])
def test_stream_options_refuse_an_unknown_view(query):
    from quart import Quart
    from lib.error_envelope import BadRequest
    from routes import conversation_sync_v3 as routes

    async def decode_options():
        async with Quart(__name__).test_request_context('/events' + query):
            return routes._diagnostic_options()

    import asyncio
    with pytest.raises(BadRequest):
        asyncio.run(decode_options())
