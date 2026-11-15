"""大象 quote replies (docs/CHANNEL_PROTOCOL.md P16): the platform's copy of a quoted message and the
one-message lookup the adapter declares (``history.message``).

The normalizer is ``lib/channels/dx/adapter.py`` on this base; the protocol-native bridge
(``bridge.mjs`` createNormalizer) takes it over in migration step 2, and these specs move with it.
"""
import json

import pytest

from lib.channels.dx.adapter import DxAdapter, event_from_history, event_from_live
from lib.channels.model import OUTBOX_MARKER_FIELD
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER
from tests.channels.test_dx_adapter import CONNECTED, GROUP, TICKET_EXPIRED, FakeBridge, _run, history_row, mid_at

pytestmark = pytest.mark.unit


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox):
    enabled(command_chat='dx:gid:900')
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': 1_761_000_000_000})
    return store


def test_a_live_quote_carries_the_platforms_copy_of_the_quoted_message():
    """The bridge decodes the quoted protobuf row (decodeQuotes); the adapter describes it like a message."""
    quoted_mid = mid_at(1_761_000_000_000, 3)
    row = {'mid': quoted_mid, 'fromUid': '2002', 'fromName': 'Ann', 'cts': 5, 'type': 4,
           'message': {'url': 'https://s3plus.internal.example.com/x/chart.png', 'size': 12},
           'extension': json.dumps({OUTBOX_MARKER_FIELD: 'ob42'})}
    event = event_from_live({'mid': mid_at(1_761_000_000_100), 'from': OWNER_UID, 'gid': '900', 'type': 20,
                             'message': {'searchText': 'this chart?'}}, source='im', self_uid=OWNER_UID,
                            quotes=[{'mid': quoted_mid, 'from': '2002', 'row': row, 'defaultText': '[图片]'}])
    assert (event.reply_to_external_id, event.reply_to_sender_id) == (quoted_mid, '2002')
    copy = event.quoted
    assert (copy.sender_id, copy.sender_name, copy.sent_at_ms) == ('2002', 'Ann', 1_761_000_000_000)
    assert copy.text == '[image]'
    assert [(item.kind, item.name) for item in copy.attachments] == [('image', f'image-{quoted_mid}.jpg')]
    assert copy.marker == 'ob42' and not copy.recalled
    assert 's3plus' not in repr(copy)  # a quoted original is described, never fetched: no URL is kept
    text_quote = event_from_live({'mid': mid_at(1_761_000_000_101), 'from': OWNER_UID, 'gid': '900', 'type': 20,
                                  'message': {'searchText': 'ok'}}, source='im', self_uid=OWNER_UID,
                                 quotes=[{'mid': quoted_mid, 'from': '2002',
                                          'row': {'mid': quoted_mid, 'from': '2002', 'type': 1,
                                                  'message': {'text': 'the quoted words'}}}])
    assert text_quote.quoted.text == 'the quoted words'


def test_a_bridge_without_quote_rows_still_names_the_quoted_message():
    """N-1 bridge output ({mid, from} only): the reference stays, there is no copy, the core looks it up."""
    event = event_from_live({'mid': mid_at(1_761_000_000_100), 'from': OWNER_UID, 'gid': '900', 'type': 20,
                             'message': {'searchText': '性能分数呢'}}, source='im', self_uid=OWNER_UID,
                            quotes=[{'mid': '1813704656489082880', 'from': OWNER_UID}])
    assert event.reply_to_external_id == '1813704656489082880' and event.quoted is None


def test_a_history_quote_carries_the_engines_compacted_copy():
    quoted = {'mid': mid_at(1_761_000_000_000, 7), 'fromUid': '2002', 'fromName': 'Ann', 'time': 9, 'type': 1,
              'text': 'compacted quote text'}
    event = event_from_history(history_row(mid_at(1_761_000_000_500), sender=OWNER_UID, text='re', quoted=quoted),
                               GROUP)
    assert event.reply_to_external_id == quoted['mid']
    assert (event.quoted.text, event.quoted.sender_name) == ('compacted quote text', 'Ann')


def test_daxiangs_own_quote_preview_stands_in_when_the_quoted_row_shows_no_content():
    """defaultText is 大象's preview of the quote: the text when the decoded row carries none of its own."""
    quoted_mid = mid_at(1_761_000_000_000, 9)
    live = event_from_live({'mid': mid_at(1_761_000_000_100), 'from': OWNER_UID, 'gid': '900', 'type': 20,
                            'message': {'searchText': '?'}}, source='im', self_uid=OWNER_UID,
                           quotes=[{'mid': quoted_mid, 'from': '2002', 'row': {'mid': quoted_mid, 'type': 1},
                                    'defaultText': '周报草稿'}])
    assert live.quoted.text == '周报草稿'
    row = history_row(mid_at(1_761_000_000_500), sender=OWNER_UID, text='re', quoted={'mid': quoted_mid, 'type': 1})
    row['raw']['quotedMessage'][0]['defaultText'] = '周报草稿'
    assert event_from_history(row, GROUP).quoted.text == '周报草稿'
    described = event_from_live({'mid': mid_at(1_761_000_000_101), 'from': OWNER_UID, 'gid': '900', 'type': 20,
                                 'message': {'searchText': '?'}}, source='im', self_uid=OWNER_UID,
                                quotes=[{'mid': quoted_mid, 'from': '2002', 'defaultText': 'preview',
                                         'row': {'mid': quoted_mid, 'type': 1, 'message': {'text': 'own text'}}}])
    assert described.quoted.text == 'own text'  # the message's own content wins over the preview


class LookupBridge(FakeBridge):
    def __init__(self, history, failure=None):
        super().__init__([], history)
        self.failure = failure

    def request(self, op, **args):
        if op == 'history' and self.failure is not None:
            self.requests.append((op, args))
            return self.failure
        return super().request(op, **args)


def _lookup_adapter(store, bridge):
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    adapter._connected.set()
    return adapter


def test_the_quote_lookup_reads_exactly_one_message(store):
    """One request: the message below ID+1 (the range cursor is an exclusive upper bound), limit 1."""
    wanted, newer = mid_at(1_761_000_000_000, 1), mid_at(1_761_000_009_000, 1)
    bridge = LookupBridge({'dx:gid:500': [history_row(wanted, text='the old plan'),
                                          history_row(newer, text='much newer')]})
    answer = _lookup_adapter(store, bridge).lookup_message(GROUP, wanted, wanted)
    assert bridge.requests == [('history', {'gid': '500', 'limit': 1, 'cursor': str(int(wanted) + 1)})]
    assert answer.found and answer.message.text == 'the old plan' and answer.message.sender_id == '2002'


def test_the_quote_lookup_answers_not_found_recalled_and_failure_truthfully(store):
    from lib.channels.model import ChannelError
    from lib.channels.quotes import LookupUnavailable
    wanted, older = mid_at(1_761_000_005_000, 1), mid_at(1_761_000_000_000, 1)
    missing = _lookup_adapter(store, LookupBridge({'dx:gid:500': [history_row(older, text='older')]}))
    assert missing.lookup_message(GROUP, wanted, wanted).found is False  # the row below is another message
    cancelled = history_row(wanted, text='withdrawn')
    cancelled['raw']['isCancel'] = True
    recalled = _lookup_adapter(store, LookupBridge({'dx:gid:500': [cancelled]})).lookup_message(GROUP, wanted, wanted)
    assert recalled.found and recalled.message.recalled and 'withdrawn' not in recalled.message.text
    failing = _lookup_adapter(store, LookupBridge({}, failure=TICKET_EXPIRED))
    with pytest.raises(ChannelError) as failed:
        failing.lookup_message(GROUP, wanted, wanted)
    assert failed.value.code == 'history_failed'
    assert failing._expired_ticket  # the expired ticket is noted like any other failed read (C1)
    offline = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: LookupBridge({}))
    with pytest.raises(LookupUnavailable):
        offline.lookup_message(GROUP, wanted, wanted)


def test_a_live_quote_of_an_unrecorded_message_is_looked_up_through_the_session(channel, dispatch):
    old = mid_at(1_760_000_000_000, 1)
    bridge = LookupBridge({'dx:gid:900': [history_row(old, sender='2002', text='week-old decision')]})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    quote = {'type': 'message', 'source': 'im', 'quotes': [{'mid': old, 'from': '2002'}], 'message': {
        'mid': mid_at(1_761_000_004_000), 'from': OWNER_UID, 'gid': '900', 'type': 20,
        'message': {'searchText': 'still true?'}}}
    _run(adapter, bridge, CONNECTED, quote)
    adapter.stop(1)
    [message] = [call['message'] for call in dispatch.to(ROUTER) if call['message']['content'] == 'still true?']
    assert 'week-old decision' in message['replyQuotes'][0]
    assert ('history', {'gid': '900', 'limit': 1, 'cursor': str(int(old) + 1)}) in bridge.requests
