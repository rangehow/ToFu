"""Read-only Resume source pin contract through each real storage authority."""

import pytest

from lib.conversation_sync.dispatch_contract import ResumeSourceSnapshot, read_resume_source_snapshot
from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit


def test_resume_plan_read_pins_only_the_exact_owned_resumable_source(semantic_authority, monkeypatch):
    factory, owner, engine = semantic_authority
    writer = factory(write=True)
    reader = factory(write=False)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda: reader)
    conversation = 'resume-plan-parity'
    scope = {'conversation_id': conversation, 'user_id': owner}
    writer.command('conversation.create', {
        'conv_id': conversation, 'user_id': owner, 'title': 'Resume plan parity',
        'settings': {},
    }, 'resume-plan-create')
    sources = {}
    for status in ('failed', 'interrupted', 'truncated', 'completed'):
        turn_id = f'resume-plan-{status}'
        settled = writer.command('turn.append_settled', {
            **scope, 'turn_id': turn_id, 'command_id': turn_id,
            'actor': 'assistant', 'status': status,
            'projection': {'content': 'private projection sentinel'},
            'config': {'privateSentinel': 'accepted configuration'},
        }, turn_id)
        sources[status] = {
            **scope, 'turn_id': turn_id, 'attempt_id': settled['attempt']['attemptId'],
            'projection_revision': settled['turn']['projectionRevision'],
        }

    def read(source, **overrides):
        return reader.query('turn.resume.plan.read', {**source, **overrides})

    before = reader.query('turn.sync.snapshot', scope)
    unavailable = {'status': 'source_unavailable'}
    stale = {'status': 'stale_source'}
    for status in ('failed', 'interrupted', 'truncated'):
        source = sources[status]
        assert read(source) == {
            'status': 'source_pinned', 'sourceStatus': status,
            'conversationId': conversation, 'turnId': source['turn_id'],
            'attemptId': source['attempt_id'], 'projectionRevision': source['projection_revision'],
        }
        assert read_resume_source_snapshot(**source) == ResumeSourceSnapshot(
            conversation, source['turn_id'], source['attempt_id'],
            source['projection_revision'], status)

    failed = sources['failed']
    assert read(failed, projection_revision=failed['projection_revision'] + 1) == stale
    assert read(failed, attempt_id=sources['truncated']['attempt_id']) == stale
    assert read(failed, turn_id='missing-turn') == unavailable
    assert read(failed, conversation_id='missing-conversation') == unavailable
    assert read(sources['completed']) == unavailable
    if engine is None:
        assert read(failed, user_id=owner + 1) == unavailable
    else:
        # The native daemon binds an owner to the session and rejects a
        # mismatched claim before it can inspect the requested source.
        with pytest.raises(StorageError) as error:
            read(failed, user_id=owner + 1)
        assert error.value.code == 'database_forbidden'

    for invalid_revision in (-1, True, 2**63):
        with pytest.raises(StorageError) as error:
            read(failed, projection_revision=invalid_revision)
        assert error.value.code == 'database_protocol_error'
    assert reader.query('turn.sync.snapshot', scope) == before
