"""A closed Turn's late background receipt and the retirement of its queue rows commit together.

Incident 2026-11-12 (receipt b15df8be): ``settle_closed_dispatch_completion``
attached the receipt to the closed Worker Turn as round-0 evidence but left its
queue row pending, so every later Turn of that conversation listed it again.
``turn.projection.update`` now carries ``background_command_retirement``: the
next projection must hold the round-0 block naming exactly these deliveries,
and the rows are removed in the same commit (disposition ``settled_as_evidence``,
never a model consumption). Every refusal leaves the projection and the rows as
they were.
"""
from __future__ import annotations

import pytest

from lib.background_command_projection import background_command_injection_record
from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
# The native engine does not implement the member yet; what it does instead is
# pinned in test_background_receipt_retirement_without_member.py.
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'postgres'], indirect=True)]

CONV = 'retire-conv'


@pytest.fixture
def authority(semantic_authority, monkeypatch):
    factory, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: factory(write=write))
    from lib.turn_lifecycle import create_turn_pair
    created = create_turn_pair(
        CONV, command_id='retire-create', input_projection={'content': 'run the build'},
        config={}, user_id=owner, conversation_defaults={'allowCreate': True, 'title': 'Retire'})
    client = factory(write=True)
    for queue_id, kind, message in [
            ('q-late', 'background_receipt', {'_backgroundCommand': 'bg_late', 'text': 'late output'}),
            ('q-keep', 'workflow_step', {'text': 'keep this'}),
            ('q-later', 'background_receipt', {'_backgroundCommand': 'bg_later', 'text': 'later output'})]:
        client.command('queue.enqueue', {'conv_id': CONV, 'user_id': owner, 'queue_id': queue_id,
                                         'kind': kind, 'message': message}, f'enqueue:{queue_id}')
    return client, owner, created['submittedTurn']['turnId']


def _turn(owner, turn_id):
    from lib.turn_lifecycle import get_turn
    return get_turn(CONV, turn_id, user_id=owner)


def _listed(client, owner, mode='background_page'):
    page = client.query('queue.list', {'conv_id': CONV, 'user_id': owner, 'mode': mode,
                                      'page_size': 50, 'cursor': None})
    rows = page['items'] if isinstance(page, dict) else page
    return [row['queueId'] for row in rows]


def _block(*deliveries, round_num=0):
    return background_command_injection_record({'roundNum': round_num, 'previews': [
        {'queueId': queue_id, 'commandId': command_id, 'text': f'{command_id} output'}
        for queue_id, command_id in deliveries]}, attempt_id='closed-attempt')


def _retire(owner, turn_id, block, *deliveries, disposition='settled_as_evidence',
            expected=None, attach=True, **extra):
    from lib.turn_lifecycle import update_turn_projection
    current = _turn(owner, turn_id)
    projection = dict(current['projection'])
    if attach:
        projection['_bgCommandInjects'] = [*projection.get('_bgCommandInjects', []), block]
    return update_turn_projection(
        CONV, turn_id, projection=projection,
        expected_projection_revision=(current['projectionRevision'] if expected is None else expected),
        user_id=owner, preserve_activity=True,
        background_retirement={
            'block_id': block['blockId'], 'disposition': disposition,
            'deliveries': [{'queue_id': queue_id, 'command_id': command_id}
                           for queue_id, command_id in deliveries], **extra})


def test_the_evidence_block_and_the_rows_retire_in_one_commit(authority):
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)
    block = _block(('q-late', 'bg_late'))

    result = _retire(owner, turn_id, block, ('q-late', 'bg_late'))

    assert result['removedQueueIds'] == ['q-late']
    after = _turn(owner, turn_id)
    assert after['projectionRevision'] == before['projectionRevision'] + 1
    assert after['projection']['_bgCommandInjects'] == [block]
    # Never listed again for a later Turn; unrelated rows keep their order.
    assert _listed(client, owner) == ['q-later']
    assert _listed(client, owner, mode='full') == ['q-keep', 'q-later']


def test_a_replay_after_the_rows_are_gone_retires_nothing_else(authority):
    client, owner, turn_id = authority
    block = _block(('q-late', 'bg_late'))
    _retire(owner, turn_id, block, ('q-late', 'bg_late'))
    # A pre-retirement attachment (block present, row pending) and a lost-ACK
    # replay both send the unchanged projection: missing rows are replay-safe.
    again = _retire(owner, turn_id, block, ('q-late', 'bg_late'), attach=False)
    assert again['removedQueueIds'] == []
    assert _listed(client, owner) == ['q-later']


def test_an_attached_block_whose_rows_stayed_pending_is_retired_unchanged(authority):
    from lib.turn_lifecycle import update_turn_projection
    client, owner, turn_id = authority
    block = _block(('q-late', 'bg_late'))
    current = _turn(owner, turn_id)
    # The pre-fix settlement: evidence attached, rows left queued.
    update_turn_projection(CONV, turn_id, projection={
        **current['projection'], '_bgCommandInjects': [block]},
        expected_projection_revision=current['projectionRevision'], user_id=owner,
        preserve_activity=True)
    assert _listed(client, owner) == ['q-late', 'q-later']

    result = _retire(owner, turn_id, block, ('q-late', 'bg_late'), attach=False)

    assert result['removedQueueIds'] == ['q-late']
    assert _turn(owner, turn_id)['projection']['_bgCommandInjects'] == [block]
    assert _listed(client, owner) == ['q-later']


def _unchanged(client, owner, turn_id, before, listed):
    after = _turn(owner, turn_id)
    assert after['projectionRevision'] == before['projectionRevision']
    assert after['projection'] == before['projection']
    assert _listed(client, owner) == listed


def test_a_stale_projection_keeps_the_rows(authority):
    from lib.turn_lifecycle import LifecycleConflict
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)
    with pytest.raises(LifecycleConflict) as refused:
        _retire(owner, turn_id, _block(('q-late', 'bg_late')), ('q-late', 'bg_late'),
                expected=before['projectionRevision'] + 7)
    assert refused.value.code == 'stale_projection'
    _unchanged(client, owner, turn_id, before, ['q-late', 'q-later'])


def test_a_row_of_another_command_rolls_back_the_attachment(authority):
    from lib.turn_lifecycle import LifecycleConflict
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)
    # The block and the deliveries agree, but the stored row names bg_late.
    with pytest.raises(LifecycleConflict) as refused:
        _retire(owner, turn_id, _block(('q-late', 'bg_other')), ('q-late', 'bg_other'))
    assert refused.value.code == 'database_conflict'
    _unchanged(client, owner, turn_id, before, ['q-late', 'q-later'])


@pytest.mark.parametrize('case', [
    'model_injection_block', 'unknown_disposition', 'no_deliveries',
    'duplicate_deliveries', 'deliveries_disagree_with_block', 'not_an_object'])
def test_a_malformed_retirement_changes_nothing(authority, case):
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)
    with pytest.raises(StorageError) as refused:
        if case == 'model_injection_block':
            _retire(owner, turn_id, _block(('q-late', 'bg_late'), round_num=3), ('q-late', 'bg_late'))
        elif case == 'unknown_disposition':
            _retire(owner, turn_id, _block(('q-late', 'bg_late')), ('q-late', 'bg_late'),
                    disposition='consumed')
        elif case == 'no_deliveries':
            _retire(owner, turn_id, _block(('q-late', 'bg_late')))
        elif case == 'duplicate_deliveries':
            _retire(owner, turn_id, _block(('q-late', 'bg_late')), ('q-late', 'bg_late'),
                    ('q-late', 'bg_late'))
        elif case == 'deliveries_disagree_with_block':
            _retire(owner, turn_id, _block(('q-late', 'bg_late')), ('q-later', 'bg_later'))
        else:
            client.command('turn.projection.update', {
                'conversation_id': CONV, 'user_id': owner, 'turn_id': turn_id,
                'projection': before['projection'],
                'expected_projection_revision': before['projectionRevision'],
                'background_command_retirement': ['q-late']}, 'retire-not-an-object')
    assert refused.value.code in {'database_protocol_error', 'database_invalid_payload'}
    _unchanged(client, owner, turn_id, before, ['q-late', 'q-later'])


def test_a_guidance_answer_cannot_retire_receipts(authority):
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)
    block = _block(('q-late', 'bg_late'))
    with pytest.raises(StorageError) as refused:
        client.command('turn.projection.update', {
            'conversation_id': CONV, 'user_id': owner, 'turn_id': turn_id,
            'projection': {**before['projection'], '_bgCommandInjects': [block]},
            'expected_projection_revision': before['projectionRevision'],
            'human_guidance_answer': {'guidanceId': 'g', 'response': 'yes', 'commandId': 'c'},
            'background_command_retirement': {
                'block_id': block['blockId'], 'disposition': 'settled_as_evidence',
                'deliveries': [{'queue_id': 'q-late', 'command_id': 'bg_late'}]},
        }, 'retire-with-guidance')
    assert refused.value.code in {'database_protocol_error', 'database_invalid_payload'}
    _unchanged(client, owner, turn_id, before, ['q-late', 'q-later'])


def test_a_reused_command_id_with_other_deliveries_is_refused_never_replayed(authority):
    """The request digest covers the member: one command id never aliases other deliveries.

    ``turn.projection.update`` stores no receipt, so a reused command id runs
    again and meets the projection CAS; it can never return the first result.
    """
    client, owner, turn_id = authority
    before = _turn(owner, turn_id)

    def command(block, *deliveries):
        return client.command('turn.projection.update', {
            'conversation_id': CONV, 'user_id': owner, 'turn_id': turn_id,
            'projection': {**before['projection'], '_bgCommandInjects': [block]},
            'expected_projection_revision': before['projectionRevision'],
            'preserve_activity': True,
            'background_command_retirement': {
                'block_id': block['blockId'], 'disposition': 'settled_as_evidence',
                'deliveries': [{'queue_id': queue_id, 'command_id': command_id}
                               for queue_id, command_id in deliveries]},
        }, 'retire-reused-command-id')

    first = command(_block(('q-late', 'bg_late')), ('q-late', 'bg_late'))
    assert first['removedQueueIds'] == ['q-late']
    with pytest.raises(StorageError) as refused:
        command(_block(('q-later', 'bg_later')), ('q-later', 'bg_later'))
    assert refused.value.code in {'turn_projection_stale', 'idempotency_conflict',
                                  'database_idempotency_conflict'}
    assert _listed(client, owner) == ['q-later']
    attached = _turn(owner, turn_id)['projection']['_bgCommandInjects']
    assert [block['previews'][0]['queueId'] for block in attached] == ['q-late']
