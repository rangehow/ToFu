"""微信 ClawBot iLink transport: long-poll ingress and replies (docs/CHANNELS.md C12).

Responsibility: ``POST /ilink/bot/getupdates`` with the stored cursor
(``get_updates_buf``), held by the server up to its ``longpolling_timeout_ms``
(``x-wechat.longPollMs`` until it names one; a read outliving it is an empty
answer, as in Tencent's client); user messages (``message_type`` 1) become
``Inbound`` with their 微信 context token as the chat's reply context (an
item's ``ref_msg`` is a quote: the quoted message's server ID and its quoted
item), and the bot's own messages (2) are its sends, never input. The new cursor is the
batch checkpoint: the session stores it only after the batch was ingested,
so a failed route polls the same messages again. ``ret``/``errcode`` -14
(session expired) is ``needs_login``: the owner scans a new code.

``send`` is ``POST /ilink/bot/sendmessage`` with the chat's context token
(微信 offers no reply without one, so a chat that never wrote is refused as
``unsupported_target``) and the outbox item as ``client_id``. A pending QR
login is completed first (``lib.channels.wechat.login``). ``close`` ends a
long poll in flight (getupdates or the QR status) through the shared client's
owner interruption; a send in flight completes, so its outcome stays known.

Entry points: ``WeChatTransport``, ``LoginTransport``, ``open_transport``.
Dependencies: ``lib.channels.bots.http``, ``lib.http_transport.outbound``
(owner interruption), ``protocol``, ``login``, the vault.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.channels.bots.http import is_timeout, request_json
from lib.channels.bots.platform import Inbound, SessionEnded, failure
from lib.channels.model import WECHAT, QuotedAttachment, QuotedMessage
from lib.channels.wechat import admission, files, protocol
from lib.http_transport.interruption import RequestInterruption
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'wechat'


class WeChatUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.code = 'wechat_unavailable'


def _quote(items: Any) -> tuple[str, QuotedMessage | None]:
    """``(svr_id, copy)`` of an item's ``ref_msg`` (openclaw-weixin ``src/api/types.ts`` RefMessage): the
    quoted message's server ID, and its quoted item (else its ``title``, the summary) as the copy.
    iLink names no sender for a quoted message."""
    for item in items or []:
        reference = item.get('ref_msg') if isinstance(item, Mapping) else None
        if not isinstance(reference, Mapping):
            continue
        quoted = reference.get('message_item') if isinstance(reference.get('message_item'), Mapping) else None
        text = protocol.message_text([quoted]) if quoted else ''
        text = text or str(reference.get('title') or '').strip()
        created = (quoted or {}).get('create_time_ms')
        media = files.normalize_file(quoted)
        attachments = (QuotedAttachment('file', media.name, media.mime_type, media.size_bytes),) if media else ()
        copy = QuotedMessage(text=text if not media else '', attachments=attachments,
                             sent_at_ms=created if isinstance(created, int) else None) if (text or attachments) else None
        return str(reference.get('svr_id') or (quoted or {}).get('msg_id') or ''), copy
    return '', None


def normalize(message: Mapping[str, Any]) -> Inbound | None:
    if message.get('message_type') != 1:
        return None
    sender = str(message.get('from_user_id') or '')
    message_id = message.get('message_id') or message.get('seq')
    if not sender or message_id in (None, ''):
        return None
    group = str(message.get('group_id') or '')
    created = message.get('create_time_ms')
    context = str(message.get('context_token') or '')
    items = message.get('item_list') or []
    reference, quoted = _quote(items)
    media = tuple(file for item in items if (file := files.normalize_file(item)) is not None)
    return Inbound(kind='gid' if group else 'uid', peer=group or sender, sender=sender, sender_name=sender,
                   message_id=str(message_id), text=protocol.message_text([item for item in items
                                               if not isinstance(item, Mapping) or item.get('type') != 4]),
                   media=media,
                   sent_at_s=int(created) // 1000 if isinstance(created, int) else None,
                   reply={'contextToken': context} if context else {},
                   quote_message_id=reference, quoted=quoted)


class WeChatTransport:
    def __init__(self, token_source, *, api_base: str = WECHAT['apiBase'], cursor: str = '', http=request_json, cdn=files.http_request):
        self._token = token_source
        self._api = api_base.rstrip('/')
        self._cursor = cursor
        self._http = http
        self._cdn = cdn
        self._closing = False
        self._interruption = RequestInterruption()

    def run(self, sink) -> None:
        from lib.http_transport.outbound import network_interruption
        hold_ms = WECHAT['longPollMs']
        connected = False
        while not sink.stopped.is_set() and not self._closing:
            try:
                with network_interruption(self._interruption):
                    status, body = self._http(
                        'POST', self._api + '/ilink/bot/getupdates', headers=protocol.bot_headers(self._token()),
                        body={'get_updates_buf': self._cursor, 'base_info': protocol.base_info()},
                        timeout=hold_ms / 1000)
            except Exception as error:
                if self._closing:
                    return  # close() ended the poll; nothing was delivered
                if is_timeout(error):
                    continue
                raise
            if sink.stopped.is_set() or self._closing:
                return
            if status in (401, 403):
                raise SessionEnded('needs_login', '微信 refused the bot token; scan a new login code in Settings')
            if status != 200:
                raise WeChatUnavailable(f'iLink getupdates answered HTTP {status}')
            code = body.get('ret') or body.get('errcode') or 0
            if code == WECHAT['sessionExpiredCode']:
                raise SessionEnded('needs_login', '微信 ended the bot session; scan a new login code in Settings')
            if code:
                raise WeChatUnavailable(f'iLink getupdates returned {code}')
            if not connected:
                connected = True
                sink.connected({})
            if isinstance(body.get('longpolling_timeout_ms'), int) and body['longpolling_timeout_ms'] > 0:
                hold_ms = body['longpolling_timeout_ms']
            messages = body.get('msgs') or []
            batch = [message for message in (normalize(item) for item in messages) if message]
            if messages:  # counts only: IDs, context tokens and text are never logged
                logger.info('[Channels] wechat getupdates returned %d message(s), %d from users',
                            len(messages), len(batch))
            cursor = str(body.get('get_updates_buf') or '') or self._cursor
            if batch or cursor != self._cursor:
                sink.deliver(batch, {'cursor': cursor})
                self._cursor = cursor

    def send(self, kind: str, peer: str, text: str, *, item_id: str,
             reply: Mapping[str, Any], before_submit=None) -> dict[str, Any]:
        context = str(reply.get('contextToken') or '')
        if kind != 'uid' or not context:
            return failure('unsupported_target', '微信 replies only to a chat that wrote to the bot')
        try:
            token = self._token()
        except SessionEnded:
            return failure('auth_failed', '微信 bot token unavailable')
        return self._send_item(peer, context, item_id, {'type': 1, 'text_item': {'text': text}}, token, before_submit)

    def send_attachment(self, kind: str, peer: str, path: str, *, item_id: str, reply: Mapping[str, Any], before_submit=None, before_transfer=None) -> dict:
        context = str(reply.get('contextToken') or '')
        if kind != 'uid' or not context:
            return failure('unsupported_target', '微信 files require a private chat with an inbound context')
        try:
            token = self._token()
        except SessionEnded:
            return failure('auth_failed', '微信 bot token unavailable')
        if not token:
            return failure('auth_failed', '微信 bot token unavailable')
        item = files.upload(self._http, self._api, token, peer, path, cdn=self._cdn, authorize=before_transfer)
        if item.get('ok') is False:
            return item
        return self._send_item(peer, context, item_id, item, token, before_submit)

    def download_media(self, item, path: str) -> dict:
        return files.download(item, path, cdn=self._cdn)

    def _send_item(self, peer: str, context: str, item_id: str, item: dict, token: str, before_submit=None) -> dict:
        if not token:
            return failure('auth_failed', '微信 bot token unavailable')
        try:
            status, answer = admission.request(self._http, 'POST', self._api + '/ilink/bot/sendmessage',
                                    headers=protocol.bot_headers(token), require_object=True, authorize=before_submit,
                                    body={'msg': {'from_user_id': '', 'to_user_id': peer, 'client_id': item_id,
                                                  'message_type': 2, 'message_state': 2, 'context_token': context,
                                                  'item_list': [item]},
                                          'base_info': protocol.base_info()})
        except admission.RefusedBeforeWire as error:
            return error.receipt
        if status in (401, 403):
            return failure('auth_failed', '微信 refused the bot token')
        if status != 200:
            return failure('unknown', f'HTTP {status}')
        code = answer.get('ret') or answer.get('errcode') or 0
        if code == WECHAT['sessionExpiredCode']:
            return failure('auth_failed', '微信 ended the bot session')
        if code:
            return failure('invalid_input', f'iLink sendmessage returned {code}')
        if item['type'] == 4 and answer.get('ret') != 0 and not answer.get('message_id'):
            return failure('unknown', '微信 did not explicitly acknowledge the file message')
        # SendMessageResp.message_id: the server ID a quote of this message names (ref_msg.svr_id).
        return {'ok': True, 'externalId': str(answer.get('message_id') or '')}

    def close(self) -> None:
        self._closing = True
        self._interruption.interrupt()  # a long poll in flight ends now, not at iLink's hold


class LoginTransport:
    """Complete the pending QR login, then poll with the token it issued."""

    def __init__(self, owner: int, store, *, qrcode: str, api_base: str = WECHAT['apiBase']):
        self._owner, self._store, self._api = owner, store, api_base
        self._qrcode = qrcode
        self._inner: WeChatTransport | None = None
        self._closing = False
        self._interruption = RequestInterruption()

    def run(self, sink) -> None:
        from lib.channels.wechat.login import complete
        from lib.http_transport.outbound import network_interruption
        try:
            with network_interruption(self._interruption):
                confirmed = complete(self._owner, store=self._store, api_base=self._api, stop=sink.stopped,
                                     expected_qrcode=self._qrcode)
        except Exception:
            if self._closing:
                return  # close() ended the status poll; the login keeps waiting
            raise
        if not confirmed or self._closing:
            return
        from lib.channels.bots.session import account_binding
        account = self._store.account(CHANNEL) or {}
        if account.get('login') or not account.get('enabled'):
            return
        sink.binding = account_binding(account)
        self._inner = _polling(account)
        self._inner.run(sink)

    def send(self, kind: str, peer: str, text: str, *, item_id: str, reply: Mapping[str, Any], before_submit=None) -> dict[str, Any]:
        if self._inner is None:
            return failure('not_connected', 'The 微信 login is not complete')
        return self._inner.send(kind, peer, text, item_id=item_id, reply=reply, before_submit=before_submit)

    def send_attachment(self, kind: str, peer: str, path: str, *, item_id: str, reply: Mapping[str, Any], before_submit=None, before_transfer=None) -> dict:
        if self._inner is None:
            return failure('not_connected', 'The 微信 login is not complete')
        return self._inner.send_attachment(kind, peer, path, item_id=item_id, reply=reply,
                                           before_submit=before_submit, before_transfer=before_transfer)

    def download_media(self, item, path: str) -> dict:
        if self._inner is None:
            return {'ok': False, 'detail': '微信 login is not complete'}
        return self._inner.download_media(item, path)

    def close(self) -> None:
        self._closing = True
        self._interruption.interrupt()
        if self._inner is not None:
            self._inner.close()


def _polling(account: Mapping[str, Any]) -> WeChatTransport:
    name = str((account.get('credential') or {}).get('name') or '')

    def token() -> str:
        from lib.credentials_vault import get_entry
        value = get_entry(name) if name else None
        if not value:
            raise SessionEnded('needs_login', 'The 微信 bot token is missing; scan a new login code in Settings')
        return value
    return WeChatTransport(token, api_base=account.get('apiBaseUrl') or WECHAT['apiBase'],
                           cursor=str(account.get('cursor') or ''))


def open_transport(owner: int, account: Mapping[str, Any], *, store=None):
    if (account.get('login') or {}).get('state') in ('waiting', 'scanned'):
        return LoginTransport(owner, store, qrcode=str(account['login'].get('qrcode') or ''),
                              api_base=account.get('apiBaseUrl') or WECHAT['apiBase'])
    return _polling(account)


__all__ = ['CHANNEL', 'LoginTransport', 'WeChatTransport', 'normalize', 'open_transport']
