"""Prospective cold request projection shares the live text and tool rebuild."""
import hashlib
import json

import pytest

from lib.conversation_sync.resume_request_plan import ColdResumeCandidate, read_cold_resume_candidate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('variant', [
    'complete', 'paged', 'stale', 'media', 'nested_media', 'gap',
    'earlier_gap', 'earlier_interrupted', 'stale_history', 'missing_eligibility', 'ineligible',
])
def test_cold_plan(monkeypatch, variant):
    history = [
        dict(turnId='input', actor='human', laneId='main', status='completed',
             projection={'content': 'original question'}),
        dict(turnId='reply', currentAttemptId='source', actor='assistant', laneId='main',
             status='failed', projectionRevision=7, projection={
                 'content': 'unfinished text',
                 'toolRounds': [{'toolCallId': 'call-1', 'toolName': 'read_files',
                                 'toolArgs': {'path': 'fixture.txt'},
                                 'toolContent': 'retained result', 'status': 'done'}],
             }),
    ]
    context = dict(supported=True, covered=0, continuation=None, turns=history,
                   nextTurnId=None, historyProof={'version': 2, 'digest': 'a' * 64})
    if variant == 'paged':
        context['nextTurnId'] = 'input'
    elif variant == 'stale':
        history[-1]['projectionRevision'] = 8
    elif variant == 'media':
        history[0]['projection']['attachments'] = [{'id': 'unknown'}]
    elif variant == 'nested_media':
        history[-1]['projection']['toolRounds'][0]['toolModelContent'] = [
            {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image'}},
        ]
    elif variant.startswith('earlier_'):
        history.insert(1, dict(turnId='earlier', actor='assistant', laneId='main',
            status='completed' if variant == 'earlier_gap' else 'interrupted',
            projection={'content': 'earlier prose', 'toolRounds': [
                {'toolCallId': 'call-earlier', 'toolName': 'read_files', 'toolArgs': {},
                 **({'toolContent': 'result'} if variant == 'earlier_interrupted' else {})},
            ]}))
    elif variant == 'gap':
        rounds = history[-1]['projection']['toolRounds']
        rounds.extend([
            {**rounds[0], 'toolCallId': 'missing', 'toolContent': None},
            {**rounds[0], 'toolCallId': 'after-gap'},
        ])

    context['continuePlan'] = {'historyDigest': 'a' * 64, 'anchor': {},
                               'projection': history[-1]['projection']}
    if variant == 'missing_eligibility':
        del context['continuePlan']
    if variant == 'stale_history':
        context['historyProof']['digest'] = 'b' * 64

    def digest():
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).digest()

    initial = digest()
    reads = []

    class ReadAuthority:
        def query(self, operation, payload, **kwargs):
            assert payload['user_id'] == 5
            reads.append(operation)
            if operation == 'turn.resume.plan.read':
                return dict(status='source_pinned', sourceStatus='failed', conversationId='conv',
                            turnId='reply', attemptId='source', projectionRevision=7)
            assert operation == 'turn.context.read' and payload['replay'] is False
            assert payload['continue_plan'] == dict(attempt_id='source', projection_revision=7,
                                                    history_digest='a' * 64, resume_anchor=None)
            if variant == 'ineligible':
                from lib.agent_core.storage_errors import StorageError
                raise StorageError('database_conflict', 'live descendant')
            return context

    def client(*, write=False):
        assert not write
        return ReadAuthority()

    def forbidden(*args, **kwargs):
        pytest.fail('planning entered an effectful owner')

    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor', forbidden)
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission.force_compact_if_needed', forbidden)
    monkeypatch.setattr('lib.tasks_pkg.compaction._resume_projection.restore_compacted_resume', forbidden)
    result = read_cold_resume_candidate(user_id=5, conversation_id='conv', turn_id='reply',
                                        attempt_id='source', projection_revision=7,
                                        history_digest='a' * 64)
    assert digest() == initial
    assert reads == ['turn.resume.plan.read', 'turn.context.read']
    if variant == 'complete':
        assert isinstance(result, ColdResumeCandidate)
        assert result.readiness == 'unknown'
        assert result.history_digest == 'a' * 64
        assert result.archive_presence == result.continuation_selection == 'unknown'
        messages = json.loads(result.messages_json)
        assert [m['role'] for m in messages] == ['user', 'assistant', 'tool']
        assert messages[1]['tool_calls'][0]['id'] == messages[2]['tool_call_id'] == 'call-1'
        assert 'retained result' in messages[2]['content']
        assert b'unfinished text' not in result.messages_json
    elif variant == 'gap':
        assert isinstance(result, ColdResumeCandidate)
        assert result.archive_presence == result.continuation_selection == 'unknown'
        assert result.readiness == 'unknown'
        messages = json.loads(result.messages_json)
        assert [m['role'] for m in messages] == ['user', 'assistant', 'tool']
        assert messages[-1]['tool_call_id'] == 'call-1'
        assert b'after-gap' not in result.messages_json
    else:
        assert result == {'paged': 'history_unavailable', 'stale': 'stale_source',
                          'stale_history': 'stale_context', 'ineligible': 'continue_ineligible',
                          'missing_eligibility': 'continue_eligibility_unavailable',
                          **dict.fromkeys(('media', 'nested_media', 'earlier_gap',
                                           'earlier_interrupted'), 'unsupported_history')}[variant]
