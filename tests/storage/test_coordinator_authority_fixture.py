"""The shared preparation seam retains real binding and running lifecycle state."""
import pytest

from lib import turn_lifecycle as turns
from lib.conversations.project_coordination import ProjectCoordinationRepository
from tests.support.coordinator_authority import create_bound_coordinator, start_coordinator_turn

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)
def test_shared_preparation_retains_binding_and_running_attempt(semantic_authority, monkeypatch):
    factory, owner, _ = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    prepared = create_bound_coordinator(factory, owner, '/fixture-owner',
        folder_id='fixture', command_id='create-coordinator')
    binding = prepared.binding
    restored = ProjectCoordinationRepository(owner, client=factory,
                                             conversations=prepared.conversations)
    assert restored.read('/fixture-owner') == binding
    assert binding['availability'] == 'available' and binding['revision'] == 1
    pair = start_coordinator_turn(binding['conversationId'], owner,
        command_id='source', task_id='fixture-task', content='Owner request')
    attempt = turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)
    turn = turns.get_turn(binding['conversationId'], pair['turn']['turnId'], user_id=owner)
    assert attempt['status'] == 'running' and attempt['taskId'] == 'fixture-task'
    assert turn['currentAttemptId'] == attempt['attemptId']
    assert turns.get_turn(binding['conversationId'], pair['submittedTurn']['turnId'],
                          user_id=owner)['projection']['content'] == 'Owner request'
    restored.bind('/fixture-owner', '', expected_revision=1, command_id='withdraw')
    assert prepared.bindings.read('/fixture-owner')['revision'] == 2
    assert turns.get_attempt(attempt['attemptId'], user_id=owner) == attempt
