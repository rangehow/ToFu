"""In-call projection-copy accounting and legacy transcript parity."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _conversations as conversations
from lib.storage_authority_sql.operations_pkg._common import _dump
from tests.storage_authority_sql.test_conversation_point_read_work import database
from tests.storage_authority_sql.test_turns_delta_sync import _seed_turn
__all__ = ['database']


pytestmark = pytest.mark.unit


def reference_overlay(turn):
    projection = dict(turn.get('projection') or {})
    projection.pop('role', None)
    projection = conversations._with_legacy_compaction_fields(projection)
    created_at = int(turn.get('createdAt') or 0)
    return {
        **projection,
        'role': conversations._turn_actor_to_legacy_role(str(turn.get('actor') or '')),
        '_turnId': turn.get('turnId'),
        '_attemptId': turn.get('currentAttemptId'),
        '_turnActor': turn.get('actor'),
        '_turnKind': turn.get('kind'),
        '_turnLaneId': turn.get('laneId') or 'main',
        '_turnStatus': turn.get('status'),
        '_turnSettlement': turn.get('settlement') or {},
        '_commandPending': None,
        '_projectionRevision': int(turn.get('projectionRevision') or 0),
        'timestamp': projection.get('timestamp') or created_at,
    }


def read_with_copy_count(session, overlay):
    copies = 0
    compact = conversations._with_legacy_compaction_fields

    class ObservedProjection(dict):
        # Overriding iteration makes a full mapping copy observable through keys.
        def __iter__(self):
            return super().__iter__()

        def keys(self):
            nonlocal copies
            copies += 1
            return super().keys()

    def observed(projection):
        return ObservedProjection(compact(projection))

    with patch.object(conversations, '_with_legacy_compaction_fields', observed), \
            patch.object(conversations, '_turn_to_legacy_message', overlay):
        result = conversations._conversation_get(
            session, {'conv_id': 'conv-d', 'user_id': 1})
    return result, copies


@pytest.mark.parametrize('cohort,count', [('mixed-actors', 12), ('tools-and-compaction', 9)])
def test_full_transcript_avoids_second_projection_copy(database, cohort, count):
    for index in range(count):
        projection = {
            'content': f'{cohort}-{index}', 'thinking': 'reasoning' * (index + 1),
            'role': 'stale-role', '_turnId': 'stale-id', '_attemptId': 'stale-attempt',
            '_turnStatus': 'stale-status', '_commandPending': True,
            'timestamp': 0 if index % 2 else 3000 + index,
            'data': {'nested': [{'values': [index, {'text': 'value'}]}]},
            'toolRounds': [{'calls': [{'name': 'fixture', 'arguments': {'x': [index]}}]}],
        }
        if cohort == 'tools-and-compaction' and index % 3 == 0:
            projection['compaction'] = {
                'blockId': 'compaction', 'archiveId': f'archive-{index}',
                'estimatedPromptTokens': 500, 'foldedToolRounds': 2,
                'timestamp': 1000, 'tokensBefore': 900, 'tokensAfter': 500,
            }
        _seed_turn(database, f'turn-{index}', index, 2000 + index,
                   actor=('human', 'assistant', 'critic', 'virtual_user')[index % 4],
                   projection=projection)
    _seed_turn(database, 'branch', 100, 4000, lane='branch-1',
               projection={'content': 'not-main', 'data': {'hidden': [1]}})
    database.connection.execute('BEGIN')
    try:
        expected, old_copies = read_with_copy_count(database, reference_overlay)
        actual, new_copies = read_with_copy_count(database, conversations._turn_to_legacy_message)
        assert actual == expected
        uninstrumented = conversations._conversation_get(
            database, {'conv_id': 'conv-d', 'user_id': 1})
        assert uninstrumented == expected
        assert all(type(message) is dict for message in uninstrumented['messages'])
        assert len(actual['messages']) == count
        assert old_copies == count and new_copies == 0
        print(f'{cohort}: {count} Turns, full-projection spreads {old_copies} -> {new_copies}; exact results')
    finally:
        database.connection.rollback()
    actual['messages'][0]['data']['nested'][0]['values'].append('caller-change')
    actual['messages'].append({'content': 'caller-message'})
    fresh, _ = read_with_copy_count(database, conversations._turn_to_legacy_message)
    assert fresh == expected
    database.execute(
        'UPDATE storage_conversation_turns SET current_attempt_id=?,projection_revision=2,'
        'projection_json=? WHERE turn_id=?',
        ('attempt-revised', _dump({'content': 'revised', 'data': {'items': [2]}}), 'turn-0'))
    revised, _ = read_with_copy_count(database, conversations._turn_to_legacy_message)
    reference, _ = read_with_copy_count(database, reference_overlay)
    assert revised == reference
    assert revised['messages'][0]['_attemptId'] == 'attempt-revised'
    assert revised['messages'][0]['_projectionRevision'] == 2
    assert revised['messages'][0]['content'] == 'revised'


def test_overlay_keeps_shallow_alias_contract_and_does_not_mutate_source():
    projection = {'role': 'original', 'data': {'list': [1, {'nested': [2]}]},
                  'compaction': {'blockId': 'compaction', 'archiveId': 'frozen'}}
    turn = {'projection': projection, 'actor': 'human', 'createdAt': 123,
            'settlement': {'outcome': 'completed'}}
    before = deepcopy(turn)
    actual = conversations._turn_to_legacy_message(turn)
    assert actual == reference_overlay(turn)
    assert turn == before and actual is not projection
    assert actual['data'] is projection['data']
    assert actual['_turnSettlement'] is turn['settlement']
    actual['role'] = 'changed'
    actual['_compactions'][0]['archiveId'] = 'changed'
    assert turn == before


@pytest.mark.parametrize('field,value', [('projection_json', '[]'), ('settlement_json', '[]')])
def test_invalid_authority_objects_keep_exact_error(database, field, value):
    _seed_turn(database, 'invalid', 1, 2000)
    database.execute(f'UPDATE storage_conversation_turns SET {field}=?', (value,))
    errors = []
    for overlay in (reference_overlay, conversations._turn_to_legacy_message):
        with pytest.raises(StorageError) as raised:
            read_with_copy_count(database, overlay)
        errors.append((raised.value.code, raised.value.message))
    assert errors[0] == errors[1] and errors[0][0] == 'database_integrity'


def test_frozen_archive_never_enters_turn_overlay(database):
    archived = [{'role': 'user', 'content': 'frozen', 'data': {'list': [1, 2]}}]
    database.execute('UPDATE storage_conversations SET messages_json=?,msg_count=1', (_dump(archived),))
    with patch.object(conversations, '_turn_to_legacy_message', side_effect=AssertionError('archive overlay')):
        result = conversations._conversation_get(database, {'conv_id': 'conv-d', 'user_id': 1})
    assert result['messages'] == archived


def test_head_fold_and_revised_attempt_keep_exact_overlay_and_cached_source(database):
    from lib.storage_authority_sql.operations_pkg._turns_core import _turn_public
    from lib.storage_authority_sql.turn_projection_cache import TurnProjectionCache
    from lib.turn_projection_patch import build_projection_patch
    from tests.turn_lifecycle.test_turn_projection_head import _Session, _event, _row

    _seed_turn(database, 'head', 1, 2000)
    row = database.fetch_one('SELECT * FROM storage_conversation_turns WHERE turn_id=?', ('head',))
    base = {'role': 'stored-role', 'content': 'base', 'data': {'items': [1]}}
    current = {**base, 'content': 'folded', 'toolRounds': [{'calls': [{'id': 'call'}]}]}
    row.update(_row(base, materialized=1, revision=2, count=1, patch_bytes=100))
    patch_value = build_projection_patch(base, current, base_revision=1, target_revision=2)
    session = _Session([_event(2, patch_value)])
    session.turn_projection_cache = TurnProjectionCache(1024 * 1024)
    public = _turn_public(session, row)
    before = deepcopy(public)
    actual = conversations._turn_to_legacy_message(public)
    assert actual == reference_overlay(public)
    assert public == before and public['projection'] == current
    again = _turn_public(session, row)
    assert session.fetches == 1 and again['projection'] == current
    assert actual['data'] is again['projection']['data']
    row.update(current_attempt_id='attempt-revised', projection_json=_dump(base),
               projection_materialized_revision=None, projection_patch_count=0,
               projection_patch_bytes=0)
    revised = _turn_public(session, row)
    assert revised['projection'] == base
    result = conversations._turn_to_legacy_message(revised)
    assert result == reference_overlay(revised)
    assert result['_attemptId'] == 'attempt-revised' and result['_projectionRevision'] == 2
    assert result['content'] == 'base'
