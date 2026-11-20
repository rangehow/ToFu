"""Read-only Continue projection compared with real isolated storage acceptance."""
import json

import pytest

from lib.conversation_sync.command_service import restore_accepted_attempt_config
from lib.conversation_sync.resume_request_plan import ColdResumeCandidate, read_cold_resume_candidate
from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
from lib.turn_lifecycle import build_api_messages, create_attempt

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit


def test_continue_plan_matches_accepted_builder(semantic_authority, monkeypatch):
    factory, owner, _ = semantic_authority
    writer = factory(write=True)
    reader = factory(write=False)
    conv = 'continue-plan-parity'
    scope = dict(conversation_id=conv, user_id=owner)
    writer.command('conversation.create', dict(conv_id=conv, user_id=owner, title='Fixture',
                                               settings={}), 'create')
    writer.command('turn.append_settled', dict(**scope, turn_id='input', command_id='input',
        actor='human', projection={'content': 'question'}), 'input')
    occurrence = {'toolCallId': 'first', 'toolName': 'read_files', 'toolArgs': {},
                  'toolContent': 'read result', 'status': 'done', 'llmRound': 0}
    occurrence['_anthropicContentBlocks'] = [
        {'type': 'tool_use', 'id': 'first', 'name': 'read_files', 'input': {}},
        {'type': 'tool_use', 'id': 'missing', 'name': 'read_files', 'input': {}},
    ]
    source = writer.command('turn.append_settled', dict(**scope, turn_id='source', command_id='source',
        actor='assistant', status='interrupted', settlement={'outcome': 'interrupted',
            'cause': 'fixture_interruption', 'resumeOptions': ['continue']},
        projection={'toolRounds': [occurrence, {**occurrence, 'toolCallId': 'missing',
            'toolContent': None, 'llmRound': 0}, {**occurrence, 'toolCallId': 'after-gap', 'llmRound': 2}]}),
        'source')
    rev = source['turn']['projectionRevision']
    pin = dict(**scope, turn_id='source', projection_revision=rev,
               attempt_id=source['attempt']['attemptId'])
    digest = reader.query('turn.context.read', dict(**scope, turn_id='source', replay=False))[
        'historyProof']['digest']
    before = reader.query('turn.sync.snapshot', scope)

    def no_write_client(*, write=False):
        assert not write
        return reader

    def forbidden(*args, **kwargs):
        pytest.fail('planning entered an execution or compaction effect owner')

    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor', forbidden)
    monkeypatch.setattr('lib.tasks_pkg.compaction._resume_projection.restore_compacted_resume', forbidden)
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission.force_compact_if_needed', forbidden)
    monkeypatch.setattr('lib.storage.get_storage_client', no_write_client)
    candidate = read_cold_resume_candidate(**pin, history_digest=digest)
    assert isinstance(candidate, ColdResumeCandidate)
    assert candidate.readiness == 'unknown'
    assert set(candidate.missing_witnesses) == {
        'continuation', 'archive', 'policy', 'tools', 'route_subscription', 'window',
    }
    assert read_cold_resume_candidate(**pin, history_digest='a' * 64) == 'stale_context'
    assert read_cold_resume_candidate(**pin, history_digest=digest,
                                      resume_anchor={'type': 'wrong'}) == 'continue_ineligible'
    assert reader.query('turn.sync.snapshot', scope) == before

    monkeypatch.setattr('lib.storage.get_storage_client', factory)
    accepted = create_attempt(conv, 'source', command_id='resume', operation='continue',
        user_id=owner, expected_projection_revision=rev, config={})
    cfg = restore_accepted_attempt_config(accepted['turn'], accepted['attempt'], user_id=owner)
    wire = build_api_messages(conv, 'source', cfg, user_id=owner, allow_model_replay=False)
    wire.extend(prepare_resume_state(cfg).checkpoint_messages)
    assert json.loads(candidate.messages_json) == wire
    from lib.tool_results.tool_round_replay import UNRECORDED_TOOL_RESULT_TEXT
    assert [(row['tool_call_id'], row['content']) for row in wire if row['role'] == 'tool'] == [
        ('first', 'read result'), ('missing', UNRECORDED_TOOL_RESULT_TEXT),
    ]
    assert 'after-gap' not in candidate.messages_json.decode()
    assert read_cold_resume_candidate(**pin, history_digest=digest) == 'stale_source'
