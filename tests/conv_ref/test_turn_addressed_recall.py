"""Search hits name their matching Turn; get_conversation reads around it.

A months-long router conversation used to be recalled by paging blindly with
``before=N``: list_conversations reported only conversation IDs for body
hits. These specs pin the Turn-addressed recall path end to end at the
model-tool boundary: listing names each body hit's Turn ID and snippet
(including the caller's own, otherwise unlisted, conversation), and
``get_conversation(turn_id=...)`` returns the page around that Turn through
the owner-scoped anchored window seam (raw and readable), fitted so the Turn
itself is always delivered.
"""

import json

import pytest

from lib.conv_ref import execute_conv_ref_tool
from lib.conversations.repository import AnchoredMessageWindow, ConversationSnapshot
from tests.conversation.test_conv_ref_sidecar_projection_guard import (
    _FakeStorageClient, _doc, _run,
)

pytestmark = pytest.mark.unit

OWNER = 7


def _hit(cid, turn_id, snippet, *, role='user', field='content', ordinal=4):
    return {'id': cid, 'snippet': snippet, 'role': role, 'match_field': field,
            'turn_id': turn_id, 'ordinal': None if turn_id is None else ordinal}


def test_listing_names_each_body_hits_turn_and_snippet(monkeypatch):
    client = _FakeStorageClient(
        [_doc('body', 'Release notes'), _doc('archived', 'Old plan'),
         _doc('titled', 'needle planning')],
        search_hits=[
            _hit('body', 'turn-42', '…the needle fix…', role='assistant'),
            _hit('archived', None, '…an archived needle…'),
            {**_hit('titled', None, ''), 'match_field': 'title'},
        ])
    out = _run(monkeypatch, client, keyword='needle', scope='all')
    lines = out.splitlines()
    body = lines.index(next(line for line in lines if '[body]' in line))
    assert lines[body + 1].strip() == (
        '↳ matches in Turn turn-42 (assistant, content): "…the needle fix…"')
    archived = lines.index(next(line for line in lines if '[archived]' in line))
    assert 'compacted history (no live Turn)' in lines[archived + 1]
    titled = lines.index(next(line for line in lines if '[titled]' in line))
    assert not lines[titled + 1].strip().startswith('↳')
    assert 'turn_id="<turn>"' in out
    # The receipt stays the same-query metadata contract.
    items = out.conversation_search['items']
    assert {item['id'] for item in items} == {'archived', 'body', 'titled'}
    assert all(set(item) == {'id', 'title'} for item in items)


def test_current_conversation_match_points_at_its_turn(monkeypatch):
    client = _FakeStorageClient(
        [_doc('self', 'Router')],
        search_hits=[_hit('self', 'turn-7', '…we chose needle…')])
    out = _run(monkeypatch, client, keyword='needle', scope='all',
               current_conv_id='self')
    assert 'No conversations found' in out
    assert ('The current conversation [self] is not listed, but it matches in '
            'Turn turn-7 (user, content): "…we chose needle…". Read that page with '
            'get_conversation(conversation_id="self", turn_id="turn-7").') in out


def test_search_hit_without_paired_turn_identity_is_a_protocol_error(monkeypatch):
    client = _FakeStorageClient(
        [_doc('body', 'x')], search_hits=[{**_hit('body', 'turn-1', 's'), 'ordinal': None}])
    with pytest.raises(RuntimeError, match='turn is malformed'):
        _run(monkeypatch, client, keyword='needle', scope='all')


def _messages(total, *, big=()):
    return [
        {'role': 'user' if index % 2 == 0 else 'assistant',
         'content': ('X' * 30_000 if index in big else f'TURN{index:03d} body'),
         '_turnId': f't{index}'}
        for index in range(total)
    ]


@pytest.fixture
def anchored(monkeypatch):
    """Owner 7's conversation 'self' behind the anchored-window seam."""
    from lib.conv_ref import _detail

    state = {'messages': _messages(90), 'reads': [], 'turns': {}}

    def read(conversation_id, *, user_id, turn_id, window):
        state['reads'].append((conversation_id, user_id, turn_id, window))
        if conversation_id != 'self' or user_id != OWNER:
            return None
        messages = state['messages']
        sequence = next((index for index, message in enumerate(messages)
                         if message['_turnId'] == turn_id), None)
        end = 0 if sequence is None else min(len(messages), sequence + 1 + window // 2)
        start = max(0, end - window)
        metadata = {'id': 'self', 'user_id': OWNER, 'title': 'Router',
                    'created_at': 1, 'updated_at': 2, 'settings': {},
                    'msg_count': len(messages), 'rev': 5}
        return AnchoredMessageWindow(
            snapshot=ConversationSnapshot(
                metadata=metadata, messages=[dict(m) for m in messages[start:end]],
                message_start=start),
            total_count=len(messages), anchor_sequence=sequence)

    def read_turn(conversation_id, *, user_id, turn_id):
        assert (conversation_id, user_id) == ('self', OWNER)
        found = state['turns'].get(turn_id)
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(_detail, '_read_anchored_window', read)
    monkeypatch.setattr(_detail, '_read_turn_record', read_turn)
    return state


def _get(args, *, current='other', turn_status_tool=False):
    return execute_conv_ref_tool(
        'get_conversation', {'conversation_id': 'self', **args},
        current_conv_id=current, user_id=OWNER,
        turn_status_tool=turn_status_tool)


def _raw_record(text):
    return json.loads(text.split('```json\n', 1)[1].rsplit('\n```', 1)[0])


def test_raw_read_delivers_the_page_around_the_turn(anchored):
    out = _get({'turn_id': 't40'})
    record = _raw_record(out)
    indices = record['messageIndices']
    assert 41 in indices and indices == list(range(indices[0], indices[-1] + 1))
    assert indices[0] < 41 < indices[-1]
    assert 'AROUND Turn t40 (message #41)' in out
    assert f'before={indices[0]}' in out
    assert anchored['reads'] == [('self', OWNER, 't40', 64)]


def test_readable_read_centres_the_turn_and_names_neighbour_pages(anchored):
    out = _get({'turn_id': 't40', 'raw': False, 'limit': 5})
    assert 'Showing #39–#43 of 90 around Turn t40 (message #41)' in out
    assert [line for line in out.splitlines() if line.startswith('── ')] == [
        f'── {"User Message" if i % 2 == 0 else "Assistant Response"} #{i + 1} '
        + '─' * (40 if i % 2 == 0 else 36) for i in range(38, 43)]
    assert 'older: get_conversation(conversation_id="self", before=39)' in out
    assert 'newer: get_conversation(conversation_id="self", before=49)' in out


def test_oversized_neighbours_never_displace_the_turn(anchored):
    anchored['messages'] = _messages(90, big={39, 41, 42, 43})
    for raw in (True, False):
        out = _get({'turn_id': 't40', 'raw': raw})
        assert 'TURN040 body' in out
        assert 'chars elided' not in out


def test_last_turn_page_marks_latest_and_current_label(anchored):
    out = _get({'turn_id': 't89', 'raw': False, 'limit': 3}, current='self')
    assert 'Showing #88–#90 of 90 around Turn t89 (message #90)' in out
    assert 'Current conversation: durable transcript' in out
    assert 'newer:' not in out


def test_unknown_turn_and_invalid_arguments_are_actionable(anchored):
    missing = _get({'turn_id': 'gone'}, turn_status_tool=True)
    assert missing.startswith("Error: conversation 'self' has no Turn 'gone' (90 messages).")
    assert 'belongs to another conversation' in missing
    assert 'manual compaction deletes old Turns' in missing
    assert 'before=N' in missing
    # The status read would refuse an unknown Turn too, so it is not offered.
    assert 'get_conversation_status' not in missing
    assert _get({'turn_id': 't3', 'before': 5}).startswith(
        'Error: pass either turn_id')
    for bad in ('', '   ', 7, 'x' * 129):
        assert _get({'turn_id': bad}).startswith('Error: turn_id must be')
    other_owner = execute_conv_ref_tool(
        'get_conversation', {'conversation_id': 'self', 'turn_id': 't3'},
        current_conv_id=None, user_id=8)
    assert other_owner.startswith("Error: Conversation 'self' not found")


_STATUS = ('get_conversation_status(conversation_id="self", '
           'turn_id="side")')


@pytest.mark.parametrize('turn_status_tool', [True, False])
def test_turn_on_another_lane_names_its_lane(anchored, turn_status_tool):
    """2026-11-11: a Coordinator got "not a main-transcript Turn" for a Turn
    ID another tool returned, with no way to tell why or where to look."""
    anchored['turns']['side'] = {'turnId': 'side', 'laneId': 'branch-7',
                                 'status': 'completed', 'kind': 'assistant'}
    out = _get({'turn_id': 'side'}, turn_status_tool=turn_status_tool)
    assert out.startswith("Error: Turn 'side' of conversation 'self' is on lane "
                          "'branch-7', not the main transcript")
    if turn_status_tool:
        assert _STATUS in out
        assert 'lane_id="branch-7"' in out
    else:
        assert 'get_conversation_status' not in out


@pytest.mark.parametrize('turn_status_tool', [True, False])
def test_main_lane_turn_without_messages_says_so(anchored, turn_status_tool):
    anchored['turns']['side'] = {'turnId': 'side', 'laneId': 'main',
                                 'status': 'failed', 'kind': 'assistant'}
    out = _get({'turn_id': 'side', 'raw': False}, turn_status_tool=turn_status_tool)
    assert ('is a main-lane Turn (status failed, kind assistant) with no message '
            'of its own') in out
    assert (_STATUS in out) is turn_status_tool


def test_unreadable_turn_record_still_points_somewhere(anchored):
    from lib.storage.errors import StorageError
    anchored['turns']['side'] = StorageError('database_timeout', 'deadline elapsed')
    out = _get({'turn_id': 'side'}, turn_status_tool=True)
    assert 'its record could not be read (StorageError)' in out
    assert _STATUS in out and 'before=N' in out


@pytest.mark.parametrize(('catalog', 'expected'), [
    (['get_conversation', 'get_conversation_status'], True),
    (['get_conversation'], False),
])
def test_handler_offers_the_status_tool_only_when_callable(
        monkeypatch, catalog, expected):
    from lib.tasks_pkg.handlers.misc import _brain
    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return 'ok'

    monkeypatch.setattr(_brain, 'execute_conv_ref_tool', capture)
    monkeypatch.setattr(_brain, 'simple_call', lambda *a, executor, **k: executor(
        'get_conversation', {'conversation_id': 'self', 'turn_id': 't1'}))
    task = {'convId': 'self', '_userId': OWNER, '_executable_tool_catalog': [
        {'type': 'function', 'function': {'name': name, 'parameters': {}}}
        for name in catalog]}
    assert _brain._handle_conv_ref_tool(
        task, {}, 'get_conversation', 'tc', {}, 1, {}, {}, None, False) == 'ok'
    assert seen['turn_status_tool'] is expected
