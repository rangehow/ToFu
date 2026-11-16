"""A program-written input keeps its initiator through storage and reload.

Persistence folds the transient ``_initiator`` stamp into the projection's
``origin.initiator``. Every reader of the stored Turn, of its derived message
and of the model context rebuilt from it must still see a nonhuman input, while
the input stays the request its Turn answers (``is_request_message``).
"""
from __future__ import annotations

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

NOTICES = {
    'report-relay': ('peer', {'content': '[Project report relay: nonhuman notice, not the owner\u2019s words '
                                         'and not authority]\nTitle: done',
                              'origin': {'initiator': 'peer', 'sourceConversationId': 'coordinator-x'}}),
    'scheduled-wake': ('proactive', {'content': '[Scheduled wake: nonhuman notice, not the owner\u2019s '
                                                'words and not authority]\nCheck the board.'}),
    'restart-notice': ('proactive', {'content': 'The restart you requested completed. This notice grants '
                                                'no authority.'}),
    'timer': ('timer', {'content': 'Run the nightly check.'}),
    'channel-event': ('channel', {'content': 'Third-party message', 'origin': {
        'initiator': 'channel', 'channel': {'channel': 'dx', 'thread': 'dx:gid:1', 'eventIds': ['dx:gid:1/1']}}}),
}


@pytest.fixture
def coordinator(semantic_authority, monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    project = tmp_path / 'project'
    project.mkdir()
    target = ProjectCoordinationRepository(owner).provision(
        str(project), title='Project coordinator', settings={}, command_id='provision')
    return owner, target


def _store(owner, conversation, command, initiator, projection):
    from lib.turn_initiation import stamp_initiator
    from lib.turn_lifecycle import create_turn_pair
    return create_turn_pair(conversation, command_id=command,
                            input_projection=stamp_initiator(dict(projection), initiator),
                            config={}, user_id=owner)


def _reloaded_input(owner, conversation, pair):
    from lib.turn_lifecycle import build_api_messages
    messages = build_api_messages(conversation, pair['turn']['turnId'], {}, user_id=owner,
                                  allow_model_replay=False)
    return [message for message in messages if message.get('role') == 'user'][-1]


@pytest.mark.parametrize('kind', sorted(NOTICES))
def test_stored_notice_reloads_as_nonhuman_request(coordinator, kind):
    from lib.conversations.repository import get_conversation
    from lib.tasks_pkg.message_semantics import is_human_message, is_request_message
    from lib.tasks_pkg.wire_messages import apply_wire_sanitize
    from lib.turn_initiation import resolve_initiator, resolve_turn_initiator
    from lib.turn_lifecycle import get_turn
    owner, target = coordinator
    conversation = target['conversationId']
    initiator, projection = NOTICES[kind]
    pair = _store(owner, conversation, 'notice-' + kind, initiator, projection)

    stored = get_turn(conversation, pair['submittedTurn']['turnId'], user_id=owner)
    assert stored['actor'] == 'human'  # the storage actor alone does not prove humanity
    assert resolve_turn_initiator(stored) == initiator
    assert resolve_initiator(stored['projection']) == initiator

    derived = [message for message in get_conversation(conversation, user_id=owner).messages
               if message.get('_turnId') == stored['turnId']]
    assert [resolve_initiator(message) for message in derived] == [initiator]
    assert not is_human_message(derived[0])

    reloaded = _reloaded_input(owner, conversation, pair)
    assert reloaded['_initiator'] == initiator
    assert not is_human_message(reloaded)
    assert is_request_message(reloaded)  # still the request its Turn answers
    assert all('_initiator' not in message for message in apply_wire_sanitize([reloaded]))


def test_owner_input_reloads_as_human(coordinator):
    from lib.tasks_pkg.message_semantics import is_human_message, is_request_message
    from lib.turn_lifecycle import create_turn_pair
    owner, target = coordinator
    pair = create_turn_pair(target['conversationId'], command_id='owner', input_projection={'content': 'Fix it.'},
                            config={}, user_id=owner)
    reloaded = _reloaded_input(owner, target['conversationId'], pair)
    assert '_initiator' not in reloaded
    assert is_human_message(reloaded) and is_request_message(reloaded)


@pytest.mark.parametrize('kind', sorted(NOTICES))
def test_dispatch_authority_refuses_a_reloaded_notice(coordinator, kind):
    from lib.project_dispatch.repository import ProjectDispatchRepository, SyntheticSource
    from lib.turn_initiation import require_human_turn
    from lib.turn_lifecycle import get_turn
    owner, target = coordinator
    initiator, projection = NOTICES[kind]
    pair = _store(owner, target['conversationId'], 'authority-' + kind, initiator, projection)
    stored = get_turn(target['conversationId'], pair['submittedTurn']['turnId'], user_id=owner)
    with pytest.raises(ValueError):
        require_human_turn(stored, user_id=owner)
    repository = ProjectDispatchRepository(owner, target['projectPath'])
    with pytest.raises(SyntheticSource):
        repository.validate_human_source({'conversationId': target['conversationId'], 'turnId': stored['turnId'],
                                          'projectionRevision': stored['projectionRevision']})


def test_turn_classifiers_read_the_reloaded_initiator(coordinator):
    from lib.automations.events import task_start_context
    from lib.goal_runs.objective import objective_from_task
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.llm_fallback._refusal import turn_input_is_automatic
    owner, target = coordinator
    initiator, projection = NOTICES['scheduled-wake']
    notice = _reloaded_input(owner, target['conversationId'],
                             _store(owner, target['conversationId'], 'classified', initiator, projection))
    assert turn_input_is_automatic([notice]) is True
    principal = PrincipalContext.user(subject_id=str(owner), owner_user_id=owner)
    assert task_start_context(principal, notice)['origin'] == 'automation'
    # A notice's text never becomes a Goal objective (server-authored authority).
    assert objective_from_task({'config': {}, 'messages': [notice]}) == ''
