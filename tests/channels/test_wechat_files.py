"""Personal WeChat FILE wire and common ingress/outbox journeys (all IO faked)."""
import base64
import hashlib
import json

import pytest
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from lib.channels import attachments, tools
from lib.channels.bots import ids
from lib.channels.bots.session import BotSession
from lib.channels.model import ChannelError
from lib.channels.wechat.transport import WeChatTransport, normalize
from tests.channels.bot_fakes import WECHAT_TOKEN, ilink_message
from tests.channels.conftest import OWNER, ROUTER
from tests.channels.test_tools import _task

pytestmark = pytest.mark.unit
PEER = 'owner@im.wechat'
CHAT = ids.thread_key('wechat', 'uid', PEER)
ORIGINAL = b'%PDF-1.7\n\x00\xff original\n'


@pytest.fixture
def binding(store, router, turns, vault, monkeypatch, tmp_path):
    store.update_account('wechat', lambda account: {
        **account, 'enabled': True, 'appId': 'bot@im.bot', 'commandChat': str(CHAT),
        'identity': {'uid': PEER, 'name': ''},
        'credential': {'name': f'channel.u{OWNER}.wechat.token.fixture', 'managed': True}})
    monkeypatch.setattr(attachments, '_scope', lambda task: ([str(tmp_path)], lambda p: p, set()))
    (tmp_path / 'proof.pdf').write_bytes(ORIGINAL)
    return store


def fixture_wire(send):
    """A synthetic physical send, obeying the same admission hook as the real adapter."""
    from lib.http_transport.outbound import current_wire_send_authority

    def call(*args, **kwargs):
        guard = current_wire_send_authority()
        if guard is not None:
            guard()
        return send(*args, **kwargs)
    return call


def _response(status, *, headers=None, data=b''):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = data
    response._content_consumed = True
    return response


@pytest.mark.parametrize('cut', ['ok', 'caption_empty', 'upload_retry', 'oversize', 'lost_ack', 'host_refusal', 'auth_refusal', 'rebind'])
def test_wechat_file_outbox(binding, cut):
    requests_seen, uploads, messages = [], [], []

    def http(method, url, *, headers, body, **opts):
        assert method == 'POST' and headers['Authorization'] == f'Bearer {WECHAT_TOKEN}'
        requests_seen.append(url.rsplit('/', 1)[-1])
        if url.endswith('/getuploadurl'):
            uploads.append(body)
            assert body['to_user_id'] == PEER and body['media_type'] == 3
            assert body['rawsize'] == len(ORIGINAL) and body['filesize'] == 32
            assert body['rawfilemd5'] == hashlib.md5(ORIGINAL, usedforsecurity=False).hexdigest()
            if cut == 'host_refusal':
                return 200, {'upload_full_url': 'https://novac2c.cdn.weixin.qq.com.invalid/upload'}
            if cut == 'auth_refusal':
                return 403, {}
            return 200, {'upload_param': 'a+b&c'}
        msg = body['msg']
        assert msg['to_user_id'] == PEER and msg['context_token'] == 'owner-context'
        messages.append(msg)
        if msg['item_list'][0]['type'] == 1 and cut == 'caption_empty':
            return 200, {}
        if msg['item_list'][0]['type'] == 4 and cut == 'lost_ack':
            raise requests.ConnectionError('request accepted, response lost')
        return 200, {'ret': 0}

    def cdn(method, url, *, data, headers, allow_redirects, stream):
        assert method == 'POST' and not allow_redirects and stream
        assert 'encrypted_query_param=a%2Bb%26c' in url
        assert headers == {'Content-Type': 'application/octet-stream'}  # no bot credential
        key = bytes.fromhex(uploads[-1]['aeskey'])
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        unpad = padding.PKCS7(128).unpadder()
        assert unpad.update(dec.update(data) + dec.finalize()) + unpad.finalize() == ORIGINAL
        if cut == 'upload_retry' and len(uploads) == 1:
            return _response(200)  # no upload ACK header -> never submit a file message
        if cut == 'oversize':
            return _response(413)
        return _response(200, headers={'x-encrypted-param': 'download-ref'})

    transport = WeChatTransport(lambda: WECHAT_TOKEN, http=fixture_wire(http), cdn=fixture_wire(cdn))
    session = BotSession(OWNER, 'wechat', store=binding, transport=transport)
    session.remember_reply(CHAT, {'contextToken': 'owner-context'})
    session.remember_reply(ids.thread_key('wechat', 'uid', 'another'), {'contextToken': 'wrong-context'})
    args = {'thread': str(CHAT), 'text': 'Attached', 'idempotency_key': 'file-key',
            'attachments': [{'path': 'proof.pdf'}]}
    result = tools.execute('channel_send', args, _task())
    item = result['item']
    lookup = {'item_id': item['id']}
    assert f'channel_outbox({json.dumps(lookup)})' in result['note']
    assert 'active' in result['note'] and 'caption' in result['note']
    assert item['state'] == 'queued' and item['attachments'][0]['type'] == 'application/pdf'
    if cut == 'rebind':
        binding.update_account('wechat', lambda account: {**account, 'appId': 'different@im.bot'})
    session.execute_outbox()
    if cut == 'upload_retry':
        assert binding.outbox_item(item['id'])[0].state == 'queued'
        assert requests_seen == ['sendmessage', 'getuploadurl']
    # Rebuild the executor over the same durable records and repeat the tool call.
    session = BotSession(OWNER, 'wechat', store=binding, transport=transport)
    session.execute_outbox()
    settled = tools.execute('channel_send', args, _task())['item']
    assert settled['id'] == item['id']
    before = binding.outbox_item(item['id'])[0].to_record()
    status = tools.execute('channel_outbox', lookup, _task())
    assert status['item'] == settled
    assert 'caption acceptance is unverified' in status['note']
    assert 'not handset delivery' in status['note']
    assert binding.outbox_item(item['id'])[0].to_record() == before
    if settled['state'] == 'sent':
        assert tools.execute('channel_outbox', {'thread': str(CHAT)}, _task())['items'] == []
    if cut == 'rebind':
        assert settled['state'] == 'failed' and not requests_seen
        return
    assert sum(msg['client_id'] == item['id'] for msg in messages) == 1
    wire_files = [msg for msg in messages if msg['item_list'][0]['type'] == 4]
    if cut in ('auth_refusal', 'host_refusal', 'oversize'):
        assert not wire_files and settled['state'] == 'failed'
        assert any('[附件未送达]' in str(msg['item_list']) for msg in messages)
        assert settled['attachments'][0]['error'] == ('auth_failed' if cut == 'auth_refusal' else 'attachment_rejected')
        return
    [msg] = wire_files
    assert msg['client_id'].startswith(item['id'] + '/')
    [wire] = msg['item_list']
    assert wire == {'type': 4, 'file_item': {'file_name': 'proof.pdf', 'len': str(len(ORIGINAL)), 'media': {
        'encrypt_query_param': 'download-ref', 'encrypt_type': 1,
        'aes_key': base64.b64encode(uploads[-1]['aeskey'].encode()).decode()}}}
    if cut == 'lost_ack':
        assert (settled['state'], settled['error']) == ('unknown', 'delivery_unconfirmed')
        assert settled['deliveryEvidence'] == 'delivery_unconfirmed'
        assert 'confirmation' not in settled['attachments'][0]
    else:
        assert settled['state'] == 'sent' and settled['attachments'][0]['confirmation'] == 'provider_ack'
        assert settled['deliveryEvidence'] == 'acceptance_unverified'


def test_wechat_refuses_quotes_and_credentials_before_enqueue(binding, tmp_path):
    args = {'thread': str(CHAT), 'text': 'answer', 'idempotency_key': 'k'}
    (tmp_path / 'id_rsa').write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nfixture')
    for fields, code in [({'reply_to': str(CHAT) + '/123'}, 'reply_to_unsupported'),
                         ({'attachments': [{'path': 'id_rsa'}]}, 'attachment_secret')]:
        with pytest.raises(ChannelError) as error:
            tools.execute('channel_send', {**args, **fields}, _task())
        assert error.value.code == code
    assert not binding.active_outbox(str(CHAT))


def test_wechat_original_and_quote_reach_ingress_without_credentials(binding, dispatch, monkeypatch):
    key = b'a' * 16
    pad = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = enc.update(pad.update(ORIGINAL) + pad.finalize()) + enc.finalize()
    reads = []

    def cdn(method, url, **opts):
        assert method == 'GET' and opts == {'allow_redirects': False, 'stream': True}
        reads.append(url)
        return _response(200, data=encrypted)

    def retain(path, filename, mime, *, user_id, origin):
        from pathlib import Path
        assert Path(path).read_bytes() == ORIGINAL
        assert (filename, mime, user_id, origin['channel']) == ('proof.pdf', 'application/pdf', OWNER, 'wechat')
        return {'name': filename, 'mimeType': mime, 'sizeBytes': len(ORIGINAL), 'documentId': 'fixture'}

    monkeypatch.setattr('lib.media_attachments.ingest_received_file', retain)
    file = {'type': 4, 'file_item': {'file_name': '../proof.pdf', 'len': str(len(ORIGINAL)), 'media': {
        'encrypt_query_param': 'opaque-secret-cdn-param', 'aes_key': base64.b64encode(key).decode()}}}
    original = normalize(ilink_message(11, '', item_list=[file]))
    quote = normalize(ilink_message(12, '', item_list=[{'type': 1, 'text_item': {'text': 'Read this'},
        'ref_msg': {'svr_id': 11, 'message_item': file}}]))
    session = BotSession(OWNER, 'wechat', store=binding,
                         transport=WeChatTransport(lambda: WECHAT_TOKEN, cdn=cdn))
    session.deliver([original, quote], {'cursor': 'next'})
    session.deliver([original, quote], {'cursor': 'next'})
    assert len(reads) == 1 and binding.account('wechat')['cursor'] == 'next'
    public = json.dumps(dispatch.to(ROUTER), ensure_ascii=False)
    assert 'replyQuotes' in public and 'proof.pdf' in public and 'Read this' in public
    assert 'opaque-secret-cdn-param' not in public and file['file_item']['media']['aes_key'] not in public


def test_wechat_quote_id_cannot_read_outbox_text_from_another_peer(binding, dispatch):
    from lib.channels import outbox
    other = ids.thread_key('wechat', 'uid', 'another')
    item = outbox.enqueue(OWNER, str(other), 'not-for-this-chat', idempotency_key='other',
                          origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'}, store=binding)
    stored, version = binding.outbox_item(item.id)
    stored.state, stored.external_id = 'sent', '123'
    binding.save_outbox_item(stored, version)
    binding.remember_echo('wechat', item.id, external_id='123', client_id=None)
    quote = normalize(ilink_message(27, '', item_list=[{'type': 1, 'text_item': {'text': 'What is this?'},
                      'ref_msg': {'svr_id': 123}}]))
    BotSession(OWNER, 'wechat', store=binding).deliver([quote])
    [command] = dispatch.to(ROUTER)
    assert command['message']['content'] == 'What is this?'
    assert 'not-for-this-chat' not in json.dumps(command['message']['replyQuotes'])


@pytest.mark.parametrize('origin', ['attachment_notice', 'draft_notice'])
def test_legacy_unbound_file_notice_does_not_use_current_wechat_token(binding, monkeypatch, tmp_path, origin):
    from lib.channels import outbox

    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    source = outbox.enqueue(OWNER, str(CHAT), 'old source with filename fallback', idempotency_key='source',
                            origin={'kind': 'tool'}, store=binding)
    source, version = binding.outbox_item(source.id)
    source.state = 'draft'
    source.extra['degradedAttachments'] = [{'name': 'legacy.pdf'}]
    binding.save_outbox_item(source, version)
    item = outbox.enqueue(OWNER, str(CHAT), 'old notice with a filename',
                          idempotency_key=origin.replace('_', '-') + ':' + source.id,
                          origin={'kind': origin, 'conversationId': ROUTER, 'turnId': 't'},
                          store=binding)

    def fail_on_token():
        pytest.fail('unbound notice reached the replacement credential')

    session = BotSession(OWNER, 'wechat', store=binding, transport=WeChatTransport(fail_on_token))
    session.remember_reply(CHAT, {'contextToken': 'current-context'})
    session.execute_outbox()
    result, _ = binding.outbox_item(item.id)
    assert (result.state, result.error) == ('failed', 'unsupported_target')


@pytest.mark.parametrize('legacy_state', ['queued', 'draft'])
def test_legacy_original_cannot_send_filenames_after_replacement(binding, monkeypatch, tmp_path, legacy_state):
    from lib.channels import outbox

    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    item = outbox.enqueue(OWNER, str(CHAT), 'caption', idempotency_key='legacy-original',
                          origin={'kind': 'tool'}, store=binding)
    row, version = binding.outbox_item(item.id)
    row.state = legacy_state
    row.extra['degradedAttachments'] = [{'name': 'legacy.pdf'}]
    row.text += '\n\n[legacy.pdf]'
    binding.save_outbox_item(row, version)
    binding.update_account('wechat', lambda account: {**account, 'credential': {
        'name': f'channel.u{OWNER}.wechat.token.replacement', 'managed': True}})
    if legacy_state == 'draft':
        assert outbox.approve(OWNER, item.id, store=binding).state == 'queued'
    calls = []

    def token():
        calls.append('token')
        return 'replacement-token'

    def http(*args, **kwargs):
        calls.append(kwargs['body']['msg']['item_list'][0]['text_item']['text'])
        return 200, {'ret': 0}

    session = BotSession(OWNER, 'wechat', store=binding, transport=WeChatTransport(token, http=http))
    session.remember_reply(CHAT, {'contextToken': 'replacement-context'})
    session.execute_outbox()
    result, _ = binding.outbox_item(item.id)
    assert calls == []
    assert (result.state, result.error) == ('failed', 'unsupported_target')


@pytest.mark.parametrize('channel', ['wechat', 'qq', 'wecom'])
def test_ordinary_wechat_text_and_other_bot_fallback_still_send(binding, monkeypatch, tmp_path, channel):
    from lib.channels import outbox

    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    chat = ids.thread_key(channel, 'uid', PEER)
    binding.update_account(channel, lambda account: {
        **account, 'enabled': True, 'commandChat': str(chat), 'identity': {'uid': PEER}})
    class Transport:
        def send(self, kind, peer, text, **kwargs):
            sent.append(text)
            return {'ok': True}

    sent = []
    records = [] if channel == 'wechat' else [attachments.admit(
        'proof.pdf', roots=[str(tmp_path)], owner=OWNER, conversation_id=ROUTER, carried=False)]
    item = outbox.enqueue(OWNER, str(chat), 'caption', idempotency_key='control',
                          origin={'kind': 'tool'}, attachments=records, store=binding)
    assert BotSession(OWNER, channel, store=binding, transport=Transport()).execute_outbox() == 1
    assert sent == [item.text]
    assert ('proof.pdf' in sent[0]) == (channel != 'wechat')
    assert binding.outbox_item(item.id)[0].state == 'sent'
