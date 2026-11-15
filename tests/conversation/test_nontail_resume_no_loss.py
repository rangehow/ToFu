"""No silent loss of later Turns when an old Turn is resumed or regenerated.

Owner-reported defect (2026-11-10): clicking Continue (继续生成), Regenerate
(重新生成) or a checkpoint resume on an interrupted OLD Turn deleted every
later Turn of the conversation, the owner's own later messages included,
without a warning. A resume now completes the old Turn in place and discards
nothing. Regenerate still supersedes the later lane tail, but the storage
authority refuses that discard unless the command names exactly the current
deletion closure, and only the owner's own request may name it. These specs
pin the HTTP contract and the synthetic-actor boundary; the authority's own
specs live in ``tests/storage_authority_sql/test_turns_delta_sync.py`` and the
native ``turn::tests`` / ``semantic_executor::tests``.
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.support.conversation_workers import worker_attempts as worker_attempts

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture()
def worker_admissions(monkeypatch, worker_attempts):
    """Observe real worker admission without entering a model or tool."""
    from tests.support.conversation_workers import observe_admissions
    return observe_admissions(monkeypatch)


def _old_interrupted_turn(owner_id=1):
    """An interrupted Turn followed by the owner's own later message and reply."""
    from lib.turn_lifecycle import append_settled_turn

    conversation_id = f'non-tail-http-{uuid.uuid4().hex[:10]}'
    common = {'user_id': owner_id, 'conversation_defaults': {
        'allowCreate': True, 'title': 'Non-tail resume', 'settings': {'model': 'gpt-4o-mini'}}}
    append_settled_turn(conversation_id, command_id=f'q-{conversation_id}', actor='human',
                        projection={'content': 'question'}, **common)
    target = append_settled_turn(
        conversation_id, command_id=f't-{conversation_id}', actor='assistant',
        projection={'content': 'partial answer'}, status='interrupted',
        settlement={'outcome': 'interrupted', 'cause': 'server_restart', 'resumeOptions': [
            {'operation': 'continue', 'anchor': {}},
            {'operation': 'checkpoint_resume', 'anchor': {
                'content': '', 'thinking': '', 'segments': [], 'keptToolRounds': 0}},
        ]},
        **common)['turn']
    later = [append_settled_turn(
        conversation_id, command_id=f'{name}-{conversation_id}', actor=actor,
        projection={'content': content}, **common)['turn']
        for name, actor, content in [('owner', 'human', 'please also check the docs'),
                                     ('reply', 'assistant', 'checked the docs')]]
    return conversation_id, target, later


def _remaining(conversation_id, owner_id=1):
    from lib.turn_lifecycle import list_turns
    return {turn['turnId'] for turn in list_turns(conversation_id, user_id=owner_id)['turns']}


@pytest.mark.parametrize('operation', ['continue', 'checkpoint_resume'])
def test_http_resume_of_an_old_turn_keeps_every_later_turn(
        flask_client, worker_admissions, operation):
    conversation_id, target, later = _old_interrupted_turn()
    later_ids = [turn['turnId'] for turn in later]

    response = flask_client.post(
        f'/api/v4/conversations/{conversation_id}/turns/{target["turnId"]}/attempts',
        json={'commandId': f'resume-{operation}', 'operation': operation,
              'expectedProjectionRevision': target['projectionRevision'],
              'config': {'model': 'gpt-4o-mini'}})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()['deletedTurnIds'] == []
    assert set(later_ids) <= _remaining(conversation_id)
    assert len(worker_admissions) == 1


def test_http_regenerate_of_an_old_turn_lists_the_closure_then_needs_its_exact_ids(
        flask_client, worker_admissions):
    from lib.turn_lifecycle import append_settled_turn

    conversation_id, target, later = _old_interrupted_turn()
    later_ids = [turn['turnId'] for turn in later]
    url = f'/api/v4/conversations/{conversation_id}/turns/{target["turnId"]}/attempts'
    body = {'operation': 'regenerate', 'expectedProjectionRevision': target['projectionRevision'],
            'config': {'model': 'gpt-4o-mini'}}

    refused = flask_client.post(url, json={**body, 'commandId': 'regenerate-unacknowledged'})
    assert refused.status_code == 409, refused.get_json()
    payload = refused.get_json()
    assert payload['ok'] is False
    assert payload['error']['kind'] == 'later_turns_unacknowledged'
    assert payload['laterTurns']['count'] == 2
    assert payload['laterTurns']['turnIds'] == later_ids
    assert [row['actor'] for row in payload['laterTurns']['turns']] == ['human', 'assistant']
    assert payload['laterTurns']['turns'][0]['preview'] == 'please also check the docs'
    assert set(later_ids) <= _remaining(conversation_id)

    stale = flask_client.post(url, json={**body, 'commandId': 'regenerate-stale',
                                         'discardLaterTurnIds': later_ids[:1]})
    assert stale.status_code == 409
    assert stale.get_json()['error']['kind'] == 'later_turns_changed'
    assert set(later_ids) <= _remaining(conversation_id)

    # The owner reviewed the dialog; a new message arrived before they confirmed.
    arrived = append_settled_turn(
        conversation_id, command_id=f'arrived-{conversation_id}', actor='human',
        projection={'content': 'sent while the dialog was open'}, user_id=1)['turn']
    moved = flask_client.post(url, json={**body, 'commandId': 'regenerate-moved',
                                         'discardLaterTurnIds': later_ids})
    assert moved.status_code == 409
    assert moved.get_json()['error']['kind'] == 'later_turns_changed'
    current_ids = moved.get_json()['laterTurns']['turnIds']
    assert current_ids == [*later_ids, arrived['turnId']]
    assert {*later_ids, arrived['turnId']} <= _remaining(conversation_id)
    assert worker_admissions == []

    accepted = flask_client.post(url, json={**body, 'commandId': 'regenerate-acknowledged',
                                            'discardLaterTurnIds': current_ids})
    assert accepted.status_code == 200, accepted.get_json()
    assert sorted(accepted.get_json()['deletedTurnIds']) == sorted(current_ids)
    assert not set(current_ids) & _remaining(conversation_id)
    assert len(worker_admissions) == 1


@pytest.mark.parametrize('operation', ['continue', 'regenerate'])
def test_http_attempt_on_the_latest_turn_needs_no_acknowledgement(
        flask_client, worker_admissions, operation):
    from lib.turn_lifecycle import append_settled_turn

    conversation_id = f'tail-http-{uuid.uuid4().hex[:10]}'
    target = append_settled_turn(
        conversation_id, command_id=f't-{conversation_id}', actor='assistant',
        projection={'content': 'partial answer'}, status='interrupted',
        settlement={'outcome': 'interrupted', 'cause': 'server_restart',
                    'resumeOptions': [{'operation': 'continue', 'anchor': {}}]},
        user_id=1, conversation_defaults={'allowCreate': True, 'title': 'Tail resume',
                                          'settings': {'model': 'gpt-4o-mini'}})['turn']
    response = flask_client.post(
        f'/api/v4/conversations/{conversation_id}/turns/{target["turnId"]}/attempts',
        json={'commandId': f'tail-{operation}', 'operation': operation,
              'expectedProjectionRevision': target['projectionRevision'],
              'config': {'model': 'gpt-4o-mini'}})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['deletedTurnIds'] == []
    assert len(worker_admissions) == 1


@pytest.mark.parametrize('caller', ['internal', 'trusted_dispatch'])
def test_only_the_owners_own_request_may_acknowledge_a_discard(chat_sidecar, caller):
    """Coordinators, the Router, Workers and recovery call without owner_request."""
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.error_envelope import BadRequest

    conversation_id, target, later = _old_interrupted_turn()
    body = {'commandId': f'synthetic-{caller}', 'operation': 'regenerate',
            'expectedProjectionRevision': target['projectionRevision'], 'config': {},
            'discardLaterTurnIds': [turn['turnId'] for turn in later]}
    kwargs = {'trusted_dispatch': object()} if caller == 'trusted_dispatch' else {}
    with pytest.raises(BadRequest) as refused:
        conversation_turn_commands.create_attempt(
            conversation_id, target['turnId'], 1, body, request_started_at=0.0, **kwargs)
    assert refused.value.field == 'discardLaterTurnIds'
    assert {turn['turnId'] for turn in later} <= _remaining(conversation_id)


def test_coordinator_regenerate_of_an_old_turn_is_refused_with_the_closure(
        chat_sidecar, monkeypatch):
    """The model-facing refusal names the later Turns and never deletes them."""
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.conversations import session_control

    conversation_id, target, later = _old_interrupted_turn()

    def control(_task, _scope, _arguments):
        # The shared command exactly as control_session issues it: no
        # acknowledgement and no owner_request.
        return conversation_turn_commands.create_attempt(
            conversation_id, target['turnId'], 1, {
                'commandId': 'coordinator-regenerate', 'operation': 'regenerate',
                'expectedProjectionRevision': target['projectionRevision'], 'config': {},
            }, request_started_at=0.0)

    monkeypatch.setattr(session_control, 'control_session', control)
    result = json.loads(session_control.execute_session_tool(
        {}, {'user_id': 1}, 'control_conversation', {'action': 'regenerate'}))
    assert result['code'] == 'later_turns_unacknowledged'
    assert result['later_turns']['turnIds'] == [turn['turnId'] for turn in later]
    assert 'only the owner' in result['recovery']
    assert {turn['turnId'] for turn in later} <= _remaining(conversation_id)
