"""Crash cuts at WeChat's submission boundary, on an isolated native authority."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from lib.channels import attachments, outbox, quotes
from lib.channels.bots import ids
from lib.channels.bots.session import BotSession
from lib.channels.model import ChannelEvent
from lib.channels.store import ChannelStore
from lib.channels.wechat import login
from lib.channels.wechat.transport import WeChatTransport
from tests.channels.test_wechat_files import ORIGINAL, PEER, _response, fixture_wire
from tests.support.semantic_authorities import semantic_authority as semantic_authority


pytestmark = pytest.mark.api


class Crash(BaseException):
    """Exit before application cleanup/retry logic can record a result."""


@pytest.mark.parametrize('semantic_authority', ['native'], indirect=True)
@pytest.mark.parametrize('cut', ['before_caption', 'before_file', 'file_no_ack', 'ack_before_index',
                                   'relogin_caption', 'relogin_file', 'caption_refused', 'session_stops', 'notice_rebind',
                                   'parallel_before_marker', 'parallel_file_ack', 'parallel_before_index', 'stale_scan_after_ack',
                                   'relogin_during_claim'])
def test_native_wechat_recovery(semantic_authority, cut, vault, tmp_path, monkeypatch):
    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    client, owner, engine = semantic_authority
    store = ChannelStore(owner, client=client)
    peer = ids.thread_key('wechat', 'uid', PEER)
    credential = f'channel.u{owner}.wechat.token.old'
    vault.set_entry(credential, 'fixture-old')
    store.update_account('wechat', lambda a: {**a, 'appId': 'bot', 'enabled': True,
        'commandChat': str(peer), 'identity': {'uid': PEER},
        'credential': {'name': credential, 'managed': True}})
    all_sends, notices, stale = [], [], []
    active = True
    reached, release = Event(), Event()

    def pause():
        reached.set()
        assert release.wait(10)

    def replace_login():
        store.update_account('wechat', lambda a: {**a, 'login': {'qrcode': 'fresh', 'state': 'waiting'}})
        assert login._confirm(store, {'bot_token': 'fixture-new', 'ilink_user_id': PEER,
                                     'ilink_bot_id': 'bot'}, 'https://ilinkai.weixin.qq.com', 'fresh')
        assert vault.get_entry(credential) is None
        assert vault.get_entry(store.account('wechat')['credential']['name']) == 'fixture-new'

    def token():
        value = vault.get_entry(credential)
        if active and cut == 'parallel_before_marker':
            pause()
        if active and cut == 'before_caption':
            raise Crash
        if active and cut == 'relogin_caption':
            replace_login()  # cut after the old sender read the old generation, before submission
        return value

    def api(method, url, *, headers, body, **kwargs):
        assert headers['Authorization'] == 'Bearer fixture-old'
        if url.endswith('/getuploadurl'):
            return 200, {'upload_param': 'test'}
        msg = body['msg']
        assert msg['to_user_id'] == PEER and msg['context_token'] == 'old-context'
        kind = msg['item_list'][0]['type']
        if msg['item_list'][0].get('text_item', {}).get('text', '').startswith('[附件未送达]'):
            notices.append(msg)
            return 200, {'ret': 0}
        all_sends.append(kind)
        if active and cut == 'parallel_file_ack' and kind == 4:
            pause()
        if active and cut == 'stale_scan_after_ack' and kind == 4:
            stale.extend(store.active_outbox(peer))
        if active and cut == 'caption_refused' and kind == 1:
            return 200, {'ret': -2}
        if active and cut == 'notice_rebind' and kind == 4:
            return 200, {'ret': -2}
        if active and cut == 'file_no_ack' and kind == 4:
            raise Crash
        return 200, {'ret': 0, 'message_id': '101' if kind == 1 else '102'}

    def cdn(*args, **kwargs):
        if active and cut == 'before_file':
            raise Crash
        if active and cut == 'relogin_file':
            replace_login()
        if active and cut == 'session_stops':
            session.stopped.set()
        return _response(200, headers={'x-encrypted-param': 'test'})

    remember = ChannelStore.remember_echo

    def echo(self, channel, marker, *, external_id, client_id):
        if active and cut == 'parallel_before_index' and external_id == '102':
            pause()
        if active and cut in ('ack_before_index', 'stale_scan_after_ack') and external_id == '102':
            raise Crash
        return remember(self, channel, marker, external_id=external_id, client_id=client_id)

    monkeypatch.setattr(ChannelStore, 'remember_echo', echo)
    transport = WeChatTransport(token, http=fixture_wire(api), cdn=fixture_wire(cdn))
    session = BotSession(owner, 'wechat', store=store, transport=transport)
    session.remember_reply(peer, {'contextToken': 'old-context'})
    path = tmp_path / 'original.pdf'
    path.write_bytes(ORIGINAL)
    record = attachments.admit(path.name, roots=[str(tmp_path)], owner=owner, conversation_id='test')
    item = outbox.enqueue(owner, str(peer), 'caption', idempotency_key='once', attachments=[record],
                          origin={'kind': 'tool', 'conversationId': '', 'turnId': ''}, store=store)
    begin = outbox._begin_part_submission

    def begin_with_rebind(*args):
        marked = begin(*args)
        if active and cut == 'relogin_during_claim' and marked:
            replace_login()
        return marked

    monkeypatch.setattr(outbox, '_begin_part_submission', begin_with_rebind)
    abandon = outbox._abandon

    def exit_after_refusal(*args):
        if active and cut == 'caption_refused':
            raise Crash
        return abandon(*args)

    monkeypatch.setattr(outbox, '_abandon', exit_after_refusal)
    if cut.startswith('parallel'):
        from lib.channels.bots.platform import SessionEnded

        def stopped_sender():
            raise SessionEnded('needs_login', 'no token should be read by the competing pass')

        competing = BotSession(owner, 'wechat', store=store,
                               transport=WeChatTransport(stopped_sender, http=fixture_wire(api), cdn=fixture_wire(cdn)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(session.execute_outbox)
            try:
                assert reached.wait(10)
                assert store.outbox_item(item.id)[0].state == 'sending'
                assert competing.execute_outbox() == 0
                if cut == 'parallel_before_index':
                    assert store.outbox_item(item.id)[0].extra['attachments'][0]['externalId'] == '102'
                    assert store.echo_item('wechat', external_id='102') is None
                    event = ChannelEvent(thread=peer, external_id='200', sender_id=PEER, sender_name='',
                                         text='What is this?', sent_at_ms=0, reply_to_message_id='102')
                    quote = quotes.resolve(store, event, owner_uid=PEER)
                    assert quote.attachments == (('file', 'original.pdf'),) and quote.source == 'outbox'
                assert store.outbox_item(item.id)[0].state == 'sending'
                active = False
            finally:
                release.set()
            assert result.result(timeout=10) == 1
        assert competing.execute_outbox() == 0  # the third pass cannot duplicate the caption
    elif cut.startswith('relogin') or cut in ('session_stops', 'notice_rebind'):
        session.execute_outbox()
    else:
        with pytest.raises(Crash):
            session.execute_outbox()
    if cut in ('ack_before_index', 'stale_scan_after_ack'):
        assert store.outbox_item(item.id)[0].extra['attachments'][0]['externalId'] == '102'
        assert store.echo_item('wechat', external_id='102') is None
    if cut == 'notice_rebind':
        [notice] = [x for x in store.active_outbox(peer) if x.origin['kind'] == 'attachment_notice']
        replace_login()
        BotSession(owner, 'wechat', store=store).remember_reply(peer, {'contextToken': 'new-context'})
    active = False
    engine.sigkill_authority()
    engine.reopen_after_sigkill()
    store = ChannelStore(owner, client=client)
    if cut == 'notice_rebind':
        new_entry = store.account('wechat')['credential']['name']

        def unexpected_send(*args, **kwargs):
            pytest.fail('a replacement login attempted to send the old notice')

        transport = WeChatTransport(lambda: vault.get_entry(new_entry), http=unexpected_send)
    session = BotSession(owner, 'wechat', store=store, transport=transport)
    if stale:
        with monkeypatch.context() as scan:
            scan.setattr(store, 'active_outbox', lambda *a, **kw: stale)
            assert session.execute_outbox() == 1
    session.execute_outbox()
    session.execute_outbox()
    item, _ = store.outbox_item(item.id)
    if cut == 'notice_rebind':
        assert store.outbox_item(notice.id)[0].state == 'failed' and all_sends == [1, 4] and not notices
    elif cut == 'file_no_ack':
        assert (item.state, item.error, all_sends) == ('unknown', 'delivery_unconfirmed', [1, 4])
    elif cut in ('relogin_caption', 'relogin_during_claim'):
        assert item.state == 'failed' and all_sends == []
    elif cut in ('relogin_file', 'caption_refused'):
        assert item.state == 'failed' and all_sends == [1]
    else:
        assert (item.state, all_sends) == ('sent', [1, 4])
        assert store.echo_item('wechat', external_id='102') == outbox.part_marker(item.id, 0)
