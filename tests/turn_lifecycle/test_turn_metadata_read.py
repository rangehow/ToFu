"""Owned Turn metadata reads avoid bodies while preserving public identity.

The shared storage fixture exercises the real SQL sidecar and native daemon;
facade fault probes enforce strict result/flag boundaries before context use.
"""
import pytest

from lib import turn_lifecycle as turns
from lib.storage.errors import StorageError
from tests.turn_lifecycle.test_task_event_delivery import storage as storage

pytestmark = pytest.mark.unit


def test_metadata_matches_default_owned_turn_without_growing_body(storage, monkeypatch):
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: storage.client)
    conversation = 'metadata-read'
    body = {'content': 'growing answer ' * 24000}
    created = turns.append_settled_turn(conversation,
        command_id='metadata-seed', actor='assistant', projection=body, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Metadata'})
    turn_id = created['turn']['turnId']
    full = turns.get_turn(conversation, turn_id, user_id=1)
    explicit = turns.get_turn(conversation, turn_id, user_id=1, include_projection=True)
    metadata = turns.get_turn(conversation, turn_id, user_id=1, include_projection=False)
    assert explicit == full
    assert metadata == {key: value for key, value in full.items() if key != 'projection'}
    assert full['projection']['content'] == body['content']
    assert 'projection' not in metadata and not any(key.startswith('_') for key in metadata)
    # SQL filters the owner before lookup; the native daemon also refuses a
    # payload owner outside its authenticated session before operation entry.
    with pytest.raises((turns.LifecycleNotFound, StorageError)) as foreign:
        turns.get_turn(conversation, turn_id, user_id=2, include_projection=False)
    if isinstance(foreign.value, StorageError):
        assert foreign.value.code == 'database_forbidden'
    with pytest.raises(turns.LifecycleNotFound):
        turns.get_turn('foreign-conversation', turn_id, user_id=1, include_projection=False)
    with pytest.raises(turns.LifecycleNotFound):
        turns.get_turn(conversation, 'missing', user_id=1, include_projection=False)
    for invalid in [None, 0, 1, 'false', [], {}]:
        with pytest.raises(StorageError) as caught:
            storage.client.query('turn.get', {'conversation_id': conversation,
                'turn_id': turn_id, 'user_id': 1, 'include_projection': invalid})
        assert caught.value.code == 'database_protocol_error'


@pytest.mark.parametrize('flag', [None, 0, 1, 'false', [], {}])
def test_invalid_projection_flag_is_rejected_before_storage(monkeypatch, flag):
    monkeypatch.setattr(turns, '_turn_client',
        lambda **kwargs: pytest.fail('Invalid projection flag reached storage'))
    with pytest.raises(TypeError, match='projection flag'):
        turns.get_turn('conversation', 'turn', user_id=7, include_projection=flag)


@pytest.mark.parametrize('private_field', ['projection', '_projectionHead'])
def test_metadata_response_cannot_smuggle_a_body(monkeypatch, private_field):
    class Client:
        def query(self, operation, payload):
            assert operation == 'turn.get' and payload['include_projection'] is False
            return {'turnId': 'turn', private_field: {'content': 'not metadata'}}
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: Client())
    with pytest.raises(StorageError) as caught:
        turns.get_turn('conversation', 'turn', user_id=7, include_projection=False)
    assert caught.value.code == 'database_protocol_error'


def test_content_preview_is_metadata_plus_the_authority_cut_prefix(storage, monkeypatch):
    """A history page shows 2,000 characters of a multi-MB Worker projection.

    Measured 2026-11-15: one assignment-history build read 31 complete Worker
    Turns (57.4 MiB, up to 6.5 MiB each) in the API host for a 355 KiB page.
    """
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: storage.client)
    conversation = 'preview-read'
    content = '结果：\U0001F600' + 'growing answer ' * 24000
    created = turns.append_settled_turn(conversation,
        command_id='preview-seed', actor='assistant', projection={'content': content}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Preview'})
    turn_id = created['turn']['turnId']
    metadata = turns.get_turn(conversation, turn_id, user_id=1, include_projection=False)

    preview = turns.get_turn(conversation, turn_id, user_id=1, include_projection=False,
                             content_preview_chars=2000)

    assert preview == {**metadata, 'contentPreview': content[:2000]}
    assert turns.get_turn(conversation, turn_id, user_id=1, include_projection=False,
                          content_preview_chars=4)['contentPreview'] == '结果：\U0001F600'
    with pytest.raises(turns.LifecycleNotFound):
        turns.get_turn(conversation, 'missing', user_id=1, include_projection=False,
                       content_preview_chars=2000)
    for flag, chars in [(None, 8), (True, 8), (False, 0), (False, -1), (False, '8')]:
        payload = {'conversation_id': conversation, 'turn_id': turn_id, 'user_id': 1,
                   'content_preview_chars': chars}
        if flag is not None:
            payload['include_projection'] = flag
        with pytest.raises(StorageError) as caught:
            storage.client.query('turn.get', payload)
        assert caught.value.code == 'database_protocol_error'


def test_content_preview_of_non_text_content_is_null(storage, monkeypatch):
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: storage.client)
    created = turns.append_settled_turn('preview-parts',
        command_id='preview-parts-seed', actor='assistant', projection={'content': ['parts']},
        user_id=1, conversation_defaults={'allowCreate': True, 'title': 'Preview'})
    preview = turns.get_turn('preview-parts', created['turn']['turnId'], user_id=1,
                             include_projection=False, content_preview_chars=2000)
    assert preview['contentPreview'] is None


@pytest.mark.parametrize('response', [{'turnId': 'turn'}, {'turnId': 'turn', 'contentPreview': 'x' * 9},
                                      {'turnId': 'turn', 'contentPreview': {'content': 'x'}}])
def test_content_preview_response_is_bounded_and_present(monkeypatch, response):
    reads = []

    class Client:
        def query(self, operation, payload):
            reads.append(payload)
            return response
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: Client())
    with pytest.raises(StorageError) as caught:
        turns.get_turn('conversation', 'turn', user_id=7, include_projection=False,
                       content_preview_chars=8)
    assert caught.value.code == 'database_protocol_error'
    assert reads[0] == {'conversation_id': 'conversation', 'turn_id': 'turn', 'user_id': 7,
                        'include_projection': False, 'content_preview_chars': 8}
    # A row without the preview reads like an engine built before the parameter:
    # the one follow-up is the plain full read, never a second preview request.
    assert reads[1:] == ([{'conversation_id': 'conversation', 'turn_id': 'turn', 'user_id': 7}]
                         if 'contentPreview' not in response else [])


def test_engine_ignoring_content_preview_gets_the_same_cut_from_the_projection(storage, monkeypatch):
    class EngineBeforeContentPreview:
        def query(self, operation, payload):
            payload = {key: value for key, value in payload.items() if key != 'content_preview_chars'}
            return storage.client.query(operation, payload)

    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: storage.client)
    content = '结果：\U0001F600' + 'growing answer ' * 400
    created = turns.append_settled_turn('preview-legacy',
        command_id='preview-legacy-seed', actor='assistant', projection={'content': content},
        user_id=1, conversation_defaults={'allowCreate': True, 'title': 'Preview'})
    turn_id = created['turn']['turnId']
    current = turns.get_turn('preview-legacy', turn_id, user_id=1, include_projection=False,
                             content_preview_chars=2000)
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: EngineBeforeContentPreview())
    assert turns.get_turn('preview-legacy', turn_id, user_id=1, include_projection=False,
                          content_preview_chars=2000) == current
    assert current['contentPreview'] == content[:2000]


@pytest.mark.parametrize('include_projection,chars', [(True, 8), (False, 0), (False, True), (False, '8')])
def test_invalid_content_preview_is_rejected_before_storage(monkeypatch, include_projection, chars):
    monkeypatch.setattr(turns, '_turn_client',
        lambda **kwargs: pytest.fail('Invalid content preview reached storage'))
    with pytest.raises(TypeError, match='content preview'):
        turns.get_turn('conversation', 'turn', user_id=7, include_projection=include_projection,
                       content_preview_chars=chars)
