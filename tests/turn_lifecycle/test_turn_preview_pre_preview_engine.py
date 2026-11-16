"""A metadata Turn read with a content preview survives an engine that predates the preview.

The restart keep rule can retain an engine built before ``content_preview_chars``
existed; it answers ``turn.get`` with metadata and no ``contentPreview``.
``get_turn``/``get_turns`` then cut the preview from the public projection
themselves, for the same owner, instead of failing the read.
"""
from __future__ import annotations

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)]


class _PrePreviewEngine:
    """A storage client whose ``turn.get`` ignores ``content_preview_chars``."""

    def __init__(self, client):
        self._client = client
        self.turn_gets = []

    def query(self, operation, payload, *args, **kwargs):
        if operation == 'turn.get':
            self.turn_gets.append(dict(payload))
            payload = {key: value for key, value in payload.items() if key != 'content_preview_chars'}
        return self._client.query(operation, payload, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def settled(semantic_authority, monkeypatch, tmp_path):
    from lib.turn_lifecycle import append_settled_turn, create_turn_pair

    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = 'preview-conversation'
    create_turn_pair(conversation, command_id='request', input_projection={'content': 'Report back'},
                     config={}, user_id=owner, conversation_defaults={
                         'allowCreate': True, 'title': 'Preview', 'settings': {'projectPath': str(tmp_path)}})
    turn = append_settled_turn(conversation, command_id='report', actor='assistant', user_id=owner,
                               kind='coordinator_report.progress',
                               projection={'content': 'Milestone verified on both authorities',
                                           'origin': {'initiator': 'brain'}})
    engine = _PrePreviewEngine(client())
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: engine)
    return conversation, turn['turn']['turnId'], owner, engine


def test_get_turn_cuts_the_preview_itself_from_an_engine_that_ignores_it(settled):
    from lib.turn_lifecycle import get_turn

    conversation, turn_id, owner, engine = settled
    row = get_turn(conversation, turn_id, user_id=owner, include_projection=False, content_preview_chars=9)
    assert row['contentPreview'] == 'Milestone'
    assert 'projection' not in row
    assert [read['user_id'] for read in engine.turn_gets] == [owner, owner]  # the fallback reads as the owner


def test_get_turns_cuts_the_preview_for_each_read(settled):
    from lib.turn_lifecycle import get_turns

    conversation, turn_id, owner, _engine = settled
    [row] = get_turns([(conversation, turn_id, False, 4)], user_id=owner)
    assert row['contentPreview'] == 'Mile'
