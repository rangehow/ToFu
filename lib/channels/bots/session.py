"""One bot platform connection of one owner (docs/CHANNELS.md C12).

Responsibility: run a platform transport (``lib.channels.bots.platform``) and
be its sink. Each inbound batch is, in order: the chat's reply context kept on
its thread record, a pending pairing code consumed (it binds the owner's
platform account and makes that private chat the command chat; never routed),
then every other message normalized to a ``ChannelEvent`` and ingested
through ``lib.channels.ingress`` (the shared C2/C7 boundary: only the bound
owner in the command chat is human input), and only then the transport's
checkpoint (a 微信 cursor) persisted, so a failed route replays as duplicates.
The session also executes its channel's outbox (C5) through the transport;
bot platforms keep no thread history, so an unknown send is sent again. A send
that no network route carried is deferred (``not_connected``), not unknown.

Stopping has two halves: ``request_stop`` signals the threads and closes the
transport on a thread of its own, waiting for nothing (shutdown asks it when
its drain begins); ``stop`` requests, then joins every thread within its
timeout.

Entry point: ``BotSession`` (``start``, ``request_stop``, ``stop``, ``alive``,
``tick``, ``deliver``, ``connected``, ``execute_outbox``). Dependencies: ``ChannelStore``,
``ingress``, ``outbox``, ``ids``, the platform transport modules and the
network authority's route-exhaustion evidence (``lib.http_transport``).
"""
from __future__ import annotations

import importlib
import json
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

from lib.channels import ingress, outbox
from lib.channels.bots import ids
from lib.channels.bots.platform import Inbound, SessionEnded, failure
from lib.channels.model import ChannelError, ChannelEvent, ThreadKey
from lib.channels.protocol.contract import adapter_descriptor
from lib.channels.store import ChannelStore, now_ms
from lib.http_transport.connection_setup import NetworkRoutesExhausted
from lib.log import get_logger

logger = get_logger(__name__)

TRANSPORTS = {'wecom': 'lib.channels.wecom.transport', 'qq': 'lib.channels.qq.transport',
              'wechat': 'lib.channels.wechat.transport'}
PAIRED_TEXT = '配对完成：以后你在这个私聊里发的消息会交给 Tofu 总负责人处理，其他人的消息只作为参考信息。'


def account_binding(account: Mapping[str, Any]) -> tuple:
    """The configuration a running session depends on; a change restarts it."""
    return (account.get('appId'), (account.get('credential') or {}).get('name'), account.get('apiBaseUrl'),
            (account.get('login') or {}).get('qrcode'))


def _code_text(text: str) -> str:
    return re.sub(r'\s+', '', str(text or '')).upper()


class _Sender:
    """The outbox sender port (``lib.channels.outbox.OutboxSender``) over the transport."""

    def __init__(self, session: 'BotSession'):
        self._session = session

    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]:
        if reply_to_external_id:
            return failure('invalid_input', 'This bot cannot send native quote replies')
        transport = self._session.transport()
        record = self._session.store.thread(thread) or {}
        try:
            result = transport.send(thread.kind, ids.peer(thread), text, item_id=marker,
                                    reply=dict(record.get('botReply') or {}))
        except NetworkRoutesExhausted:
            # Every route failed before the request went out (docs/NETWORK.md), so the platform
            # has nothing: a deferral, never an unknown send and its Watchdog fault.
            return failure('not_connected', 'No network route to the platform connected')
        if result.get('reply') is not None:
            self._session.remember_reply(thread, result['reply'])
        return result

    def max_text_bytes(self, thread: ThreadKey) -> int | None:
        """The bot's documented text limit (x-adapters ``maxTextBytes``): the outbox chunks longer text."""
        return adapter_descriptor(self._session.channel)['identities']['bot']['maxTextBytes']

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]:
        return []  # bot platforms expose no thread history: an unknown send is sent again

    def send_attachment(self, thread: ThreadKey, path: str, *, kind: str, marker: str) -> dict[str, Any]:
        return failure('attachment_rejected', 'This bot cannot send an unbound attachment')

    def send_bound_part(self, item, text: str, path: str | None, *, marker: str,
                        submitting) -> dict[str, Any]:
        """The caption and FILE share the queued credential generation and recipient fence."""
        session = self._session
        thread = ThreadKey.parse(item.thread)

        def current():
            account = session.store.account(thread.channel) or {}
            return (session.channel == thread.channel == 'wechat'
                    and account.get('enabled') and not account.get('login')
                    and session.binding == account_binding(account)
                    and outbox._attachment_binding_matches(account, item))

        def refusal():
            if not current():
                return failure('unsupported_target', 'The 微信 bot binding changed')
            if session.stopped.is_set():
                return failure('not_connected', 'The 微信 session is stopping before submission')
            return None

        admitted = False

        def before_submit():
            nonlocal admitted
            rejected = refusal()
            if rejected is not None:
                return rejected
            if not admitted:
                if not submitting():
                    return failure('unknown', 'The outbox submission was already claimed')
                admitted = True
            return refusal()

        rejected = refusal()
        if rejected is not None:
            return rejected
        record = session.store.thread(thread) or {}
        if record.get('botReplyBinding') != item.extra['attachmentBinding']:
            return failure('unsupported_target', 'The current 微信 bot needs a new message in this chat')
        transport = session.transport()
        rejected = refusal()
        if rejected is not None:
            return rejected
        reply = dict(record.get('botReply') or {})
        try:
            if path is None:
                return transport.send(thread.kind, ids.peer(thread), text, item_id=marker, reply=reply,
                                      before_submit=before_submit)
            return transport.send_attachment(thread.kind, ids.peer(thread), path, item_id=marker, reply=reply,
                                             before_submit=before_submit, before_transfer=refusal)
        except NetworkRoutesExhausted:
            return failure('not_connected', 'No network route to the platform connected')

    def history_from(self, thread: ThreadKey, external_id: str) -> list[ChannelEvent]:
        return []


class BotSession:
    def __init__(self, owner: int, channel: str, *, store: ChannelStore | None = None, transport=None):
        if channel not in TRANSPORTS:
            raise ChannelError('invalid_channel', f'{channel} is not a bot channel')
        self.store = store or ChannelStore(owner)
        self.owner = self.store.owner
        self.channel = channel
        self.stopped = threading.Event()
        self._stop_requested = False
        self._stop_lock = threading.Lock()
        self._wake = threading.Event()
        self._transport = transport
        self._transport_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._outcome: BaseException | None = None
        self.binding = account_binding(self.store.account(channel) or {})

    # ── lifecycle ────────────────────────────────────────────────────
    def transport(self):
        with self._transport_lock:
            if self._transport is None:
                module = importlib.import_module(TRANSPORTS[self.channel])
                self._transport = module.open_transport(self.owner, self.store.account(self.channel) or {},
                                                        store=self.store)
            return self._transport

    def start(self) -> None:
        self.transport()
        outbox.register_executor(self.owner, self.tick, channel=self.channel)
        for target, name in ((self._run, 'run'), (self._outbox_loop, 'outbox')):
            thread = threading.Thread(target=target, name=f'{self.channel}-{name}-{self.owner}', daemon=True)
            self._threads.append(thread)
            thread.start()

    def request_stop(self) -> None:
        """Begin stopping and wait for nothing; ``stop`` joins.

        The transport closes on its own thread, joined like the others: a
        WebSocket ``close()`` waits for the platform's closing handshake.
        """
        with self._stop_lock:
            if self._stop_requested:
                return
            self._stop_requested = True
        self.stopped.set()
        self._wake.set()
        outbox.register_executor(self.owner, None, channel=self.channel)
        transport = self._transport
        if transport is not None:
            closer = threading.Thread(target=self._close, args=(transport,),
                                      name=f'{self.channel}-close-{self.owner}', daemon=True)
            self._threads.append(closer)
            closer.start()

    def _close(self, transport) -> None:
        try:
            transport.close()
        except Exception:
            logger.warning('[Channels] %s transport close failed owner=%s', self.channel, self.owner,
                           exc_info=True)

    def stop(self, timeout: float = 2.0) -> bool:
        self.request_stop()
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def alive(self) -> bool:
        return not self.stopped.is_set() and any(thread.is_alive() for thread in self._threads)

    def tick(self) -> None:
        self._wake.set()

    def outcome(self) -> BaseException | None:
        return self._outcome

    def _status(self, state: str, detail: str = '', **kwargs) -> None:
        # A stopped session never overwrites the owner's newer decision (disable, new secret).
        self.store.set_status(self.channel, state, detail, **kwargs,
                              when=lambda account: bool(account.get('enabled')) and not self._stop_requested
                              and account_binding(account) == self.binding)

    def _report_end(self, state: str, detail: str) -> None:
        """Record why the session ended, best effort: the supervisor restarts it at the next tick.

        The failure that ended the session is often storage, which this report
        needs too; an unstored report must not kill the thread instead.
        """
        try:
            self._status(state, detail)
        except Exception as error:
            logger.warning('[Channels] %s status %s not stored owner=%s: %s', self.channel, state,
                           self.owner, getattr(error, 'code', type(error).__name__))

    def _run(self) -> None:
        try:
            self.transport().run(self)
            if not self._stop_requested:
                self._report_end('disconnected',
                                 'The platform closed the connection; reconnecting at the next tick')
        except SessionEnded as ended:
            self._report_end(ended.state, ended.detail)
        except Exception as error:
            self._outcome = error
            logger.warning('[Channels] %s session failed owner=%s', self.channel, self.owner, exc_info=True)
            self._report_end('error', f'session failed: {getattr(error, "code", type(error).__name__)}')
        finally:
            self.stopped.set()
            self._wake.set()

    def _outbox_loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self.stopped.is_set():
                return
            try:
                self.execute_outbox()
            except Exception:
                logger.warning('[Channels] %s outbox pass failed owner=%s', self.channel, self.owner,
                               exc_info=True)

    def execute_outbox(self) -> int:
        return outbox.execute(self.owner, _Sender(self), channel=self.channel, store=self.store)

    # ── sink ─────────────────────────────────────────────────────────
    def connected(self, info: Mapping[str, Any]) -> None:
        self._status('connected', '', connected_at_ms=now_ms())
        self._wake.set()

    def deliver(self, batch: list[Inbound], checkpoint: Mapping[str, Any] | None = None) -> None:
        if self._stop_requested:
            return  # a late answer of a stopped session (a 微信 long poll) writes nothing
        events = []
        for message in batch:
            try:
                thread = ids.thread_key(self.channel, message.kind, message.peer)
            except ChannelError:
                logger.warning('[Channels] %s message with an unusable chat ID skipped owner=%s',
                               self.channel, self.owner)
                continue
            if message.reply:
                self.remember_reply(thread, message.reply)
            if self._pair(message, thread):
                continue
            events.append(self._event(message, thread))
        if events:
            media = self.transport() if self.channel == 'wechat' and any(event.media for event in events) else None
            ingress.ingest_batch(self.owner, events, store=self.store, **({'media': media} if media else {}))
        if checkpoint:
            self.store.update_account(self.channel, lambda account: {**account, **dict(checkpoint)})
        self._wake.set()

    def remember_reply(self, thread: ThreadKey, reply: Mapping[str, Any]) -> None:
        value = dict(reply)
        updates = {'botReply': value}
        if self.channel == 'wechat':
            account = self.store.account(self.channel) or {}
            if self.binding != account_binding(account):
                return
            updates['botReplyBinding'] = outbox.attachment_binding(account)
        self.store.update_thread(thread, lambda record: {**record, **updates}
                                 if any(record.get(k) != v for k, v in updates.items()) else record)
        if self.channel == 'wechat':
            self._mirror_command_chat_reply(thread, value)

    def _mirror_command_chat_reply(self, thread: ThreadKey, value: Mapping[str, Any]) -> None:
        """Keep the command chat's 微信 reply context where the out-of-band alert reads it.

        微信 sends need the chat's latest context token, which otherwise lives
        only on this TofuDB thread record. The reserved vault entry
        ``channel.u<owner>.wechat.reply`` (hidden from agents) holds the owner's
        user ID and that token, so ``serverctl_pkg/oob_alert.py`` can reach the
        owner while storage is down (docs/STORAGE_OPERATIONS.md). Best effort:
        a failed mirror never affects ingress, and no value is logged.
        """
        try:
            if not value.get('contextToken') or thread.kind != 'uid':
                return
            if (self.store.account(self.channel) or {}).get('commandChat') != str(thread):
                return
            from lib.credentials_vault import get_entry, set_entry
            name = f'channel.u{self.owner}.wechat.reply'
            mirrored = json.dumps({'to': ids.peer(thread), 'contextToken': str(value['contextToken'])},
                                  sort_keys=True)
            if get_entry(name) != mirrored:
                set_entry(name, mirrored, note=f'微信 command-chat reply context (Tofu owner {self.owner})')
        except Exception as error:
            logger.warning('[Channels] wechat reply context not mirrored owner=%s: %s', self.owner,
                           type(error).__name__)

    def _event(self, message: Inbound, thread: ThreadKey) -> ChannelEvent:
        seconds = int(time.time()) if message.sent_at_s is None else message.sent_at_s
        # Recorded under the platform's message ID at first sighting (minted then when the platform
        # sends no time): a redelivery maps to the same event, and a quote naming the ID finds it (P16).
        external = self.store.provider_message_id(
            self.channel, message.message_id, lambda: ids.external_id(seconds, message.message_id))
        title = message.title or (message.sender_name if thread.kind == 'uid' else '') or message.peer
        return ChannelEvent(thread=thread, external_id=external, sender_id=message.sender,
                            sender_name=message.sender_name or message.sender, text=message.text,
                            sent_at_ms=int(external[:10]) * 1000, thread_title=title,
                            reply_to_message_id=message.quote_message_id, quoted=message.quoted, media=message.media)

    def _pair(self, message: Inbound, thread: ThreadKey) -> bool:
        """A pending pairing code sent by its author in a private chat binds that author."""
        if message.kind != 'uid' or message.sender != message.peer or not message.text:
            return False
        code = ((self.store.account(self.channel) or {}).get('pairing') or {}).get('code')
        if not code or _code_text(message.text) != _code_text(code):
            return False
        bound = []

        def change(account):
            if ((account.get('pairing') or {}).get('code')) != code:
                return account
            bound.append(True)
            account.update(identity={'uid': message.sender, 'name': message.sender_name or ''},
                           commandChat=str(thread), pairing=None)
            return account
        self.store.update_account(self.channel, change)
        if not bound:
            return False
        logger.info('[Channels] %s owner paired owner=%s', self.channel, self.owner)
        self.store.update_thread(thread, lambda record: record if record.get('title')
                                 else {**record, 'title': message.sender_name or message.sender})
        try:
            outbox.enqueue(self.owner, str(thread), PAIRED_TEXT, idempotency_key='pairing:' + message.message_id,
                           origin={'kind': 'tool', 'conversationId': '', 'turnId': ''}, store=self.store)
        except ChannelError as error:
            logger.warning('[Channels] %s pairing confirmation not enqueued: %s', self.channel, error.code)
        return True


__all__ = ['BotSession', 'PAIRED_TEXT', 'TRANSPORTS', 'account_binding']
