"""Channel application service: owner-facing views and commands.

Responsibility: the one owner-scoped command/query surface shared by the HTTP
routes and the model tools — thread listing and configuration, subscriptions,
outbox listing and decisions, and the 大象 account commands, which the
installed dx channel adapter implements (``lib.channels.adapters``;
``channel_unavailable`` without it).
Handlers stay stateless; state lives in ``ChannelStore``.

Entry points: ``account_view``, ``update_account``, ``create_command_chat``
(delegated to the dx adapter),
``thread_page``, ``thread_view``, ``configure_thread``, ``subscribe``,
``unsubscribe``, ``outbox_page``, ``outbox_attachment`` (a draft's image
thumbnails), ``decide_outbox``. Dependencies:
``ChannelStore``, ``policy``, ``outbox``, ``lib.channels.adapters``.
"""
from __future__ import annotations

from typing import Any

from lib.channels import outbox, policy
from lib.channels.model import (
    CHANNELS, CONFIGURABLE_MODES, OWNER_NOTE_MAX_CHARS, REPLY_POLICIES, ChannelError, OutboxItem, ThreadKey,
)
from lib.channels.store import ChannelStore, now_ms


CHANNEL = 'dx'
_UNSET = object()


def _store(owner: int, store: ChannelStore | None) -> ChannelStore:
    return store or ChannelStore(owner)


def _dx():
    from lib.channels.adapters import require_channel_adapter
    return require_channel_adapter(CHANNEL)


def account_view(owner: int, *, store: ChannelStore | None = None) -> dict[str, Any]:
    """Contract ``ChannelAccountView`` of the 大象 account (the installed dx adapter's)."""
    return _dx().account_view(owner, store=store)


def update_account(owner: int, update: dict[str, Any], *, store: ChannelStore | None = None,
                   robot_probe=None) -> dict[str, Any]:
    """Apply ``ChannelAccountUpdate`` to the 大象 account through the installed dx adapter."""
    return _dx().update_account(owner, update, store=store, robot_probe=robot_probe)


def create_command_chat(owner: int, *, store: ChannelStore | None = None,
                        group_creator=None, robot_factory=None) -> dict[str, Any]:
    """Create the 大象 ``Tofu Router`` command chat through the installed dx adapter (C6)."""
    return _dx().create_command_chat(owner, store=store, group_creator=group_creator,
                                     robot_factory=robot_factory)


def _owner_note(value: Any) -> str:
    """An owner note from Settings (C11): text up to the contract bound; null or blank clears it."""
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > OWNER_NOTE_MAX_CHARS:
        raise ChannelError('invalid_note', f'A note is text of at most {OWNER_NOTE_MAX_CHARS} characters')
    return value.strip()


def thread_view(record: dict[str, Any], account: dict[str, Any] | None) -> dict[str, Any]:
    """Contract ``ChannelThreadView``."""
    thread = ThreadKey.parse(record['key'])
    return {
        'key': str(thread), 'kind': thread.kind, 'title': record.get('title') or '',
        'mode': policy.effective_mode(thread, record, account),
        'modeOverride': record.get('mode') or None,
        'replyPolicy': policy.effective_reply_policy(record),
        'replyPolicyOverride': record.get('replyPolicy') or None,
        'isCommandChat': policy.is_command_chat(thread, account),
        'subscribers': list(record.get('subscribers') or []),
        'lastEventAtMs': record.get('lastEventAtMs'),
        'watermark': record.get('watermark'),
        'note': str(record.get('note') or ''),
    }


def thread_page(owner: int, *, cursor: str = '', mode: str | None = None,
                channel: str | None = None, store: ChannelStore | None = None) -> dict[str, Any]:
    """One page of threads, optionally of one ``channel`` (each viewed with its own account)."""
    store = _store(owner, store)
    if channel is not None and channel not in CHANNELS:
        raise ChannelError('invalid_channel', f'channel must be one of {list(CHANNELS)}')
    records, following = store.thread_page(after=cursor, channel=channel)
    accounts: dict[str, dict[str, Any] | None] = {}
    items = []
    for record in records:
        name = ThreadKey.parse(record['key']).channel
        if name not in accounts:
            accounts[name] = store.account(name)
        items.append(thread_view(record, accounts[name]))
    if mode:
        items = [item for item in items if item['mode'] == mode]
    return {'items': items, 'nextCursor': following}


def configure_thread(owner: int, thread: str, *, mode: Any = _UNSET, reply_policy: Any = _UNSET,
                     note: Any = _UNSET, store: ChannelStore | None = None) -> dict[str, Any]:
    """Owner decision on one thread's mode / reply policy (``None`` = default) or its owner note.

    Notes come only from Settings: no model tool writes one (C11).
    """
    store = _store(owner, store)
    key = ThreadKey.parse(thread)
    if mode is not _UNSET and mode is not None and mode not in CONFIGURABLE_MODES:
        raise ChannelError('invalid_mode', f'mode must be one of {list(CONFIGURABLE_MODES)}')
    if reply_policy is not _UNSET and reply_policy is not None and reply_policy not in REPLY_POLICIES:
        raise ChannelError('invalid_reply_policy', f'reply_policy must be one of {list(REPLY_POLICIES)}')
    note = _owner_note(note) if note is not _UNSET else _UNSET
    account = store.account(key.channel)

    def change(value):
        before = policy.effective_mode(key, value, account)
        if mode is not _UNSET:
            value['mode'] = mode
        if reply_policy is not _UNSET:
            value['replyPolicy'] = reply_policy
        if note is not _UNSET:
            value['note'] = note
        after = policy.effective_mode(key, value, account)
        if before == 'ignore' and after != 'ignore':
            # A newly watched thread starts now; its ignored past is not replayed.
            value['watermark'] = None
            value['sinceMs'] = now_ms()
        if after != before:
            # Adapters that read threads directly in watch (GitHub, C8) start
            # there instead of replaying what the previous mode left unread.
            value['modeChangedAtMs'] = now_ms()
        return value
    return thread_view(store.update_thread(key, change), account)


def subscribe(owner: int, thread: str, conversation_id: str, *,
              store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    key = ThreadKey.parse(thread)
    _require_owned_conversation(store.owner, conversation_id)
    return thread_view(store.subscribe(key, conversation_id), store.account(key.channel))


def unsubscribe(owner: int, thread: str, conversation_id: str, *,
                store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    key = ThreadKey.parse(thread)
    return thread_view(store.unsubscribe(key, conversation_id), store.account(key.channel))


def _require_owned_conversation(owner: int, conversation_id: str) -> None:
    from lib.conversations.repository import ConversationRepository
    if not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 256:
        raise ChannelError('invalid_conversation', 'A conversation ID is required')
    snapshot = ConversationRepository().get(conversation_id, user_id=owner, include_messages=False)
    settings = (snapshot.metadata.get('settings') if snapshot else None) or {}
    if snapshot is None or settings.get('archived') or settings.get('readOnly'):
        raise ChannelError('conversation_unavailable', 'Subscriber must be an active conversation of this owner')


def _titles(store: ChannelStore, items: list[OutboxItem]) -> dict[str, str]:
    titles = {}
    for item in items:
        if item.thread not in titles:
            titles[item.thread] = (store.thread(ThreadKey.parse(item.thread)) or {}).get('title') or ''
    return titles


def outbox_page(owner: int, *, thread: str | None = None, channel: str | None = None,
                store: ChannelStore | None = None) -> dict[str, Any]:
    """Active outbox items, optionally of one ``thread`` or one ``channel``."""
    store = _store(owner, store)
    if channel is not None and channel not in CHANNELS:
        raise ChannelError('invalid_channel', f'channel must be one of {list(CHANNELS)}')
    items = store.active_outbox(str(ThreadKey.parse(thread)) if thread else None, channel=channel)
    titles = _titles(store, items)
    return {'items': [item.view(titles[item.thread]) for item in items]}


def outbox_item_view(owner: int, item_id: str, *, store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    item, _ = store.outbox_item(item_id)
    if item is None:
        raise ChannelError('not_found', 'Outbox item not found')
    return item.view(_titles(store, [item])[item.thread])


def outbox_attachment(owner: int, item_id: str, index: int, *,
                      store: ChannelStore | None = None) -> tuple[bytes, str]:
    """An original image attached to an outbox item, re-read and verified: ``(bytes, MIME type)``.

    Settings shows these as the draft's thumbnails; documents and preview
    copies have none.
    """
    store = _store(owner, store)
    item, _ = store.outbox_item(item_id)
    records = (item.extra.get('attachments') if item is not None else None) or []
    if not 0 <= index < len(records) or records[index]['kind'] != 'image' \
            or records[index].get('renditionOf') is not None:
        raise ChannelError('not_found', 'No original image at that position of the outbox item')
    from lib.channels.attachments import read_original
    return read_original(records[index], owner=store.owner), records[index]['type']


def decide_outbox(owner: int, item_id: str, decision: str, *, by: str = 'owner_settings',
                  turn_id: str = '', store: ChannelStore | None = None) -> dict[str, Any]:
    """The owner's decision on an item; ``by`` and ``turn_id`` say where it was given (audit, C11)."""
    store = _store(owner, store)
    if decision == 'approve':
        item = outbox.approve(store.owner, item_id, by=by, turn_id=turn_id, store=store)
    elif decision == 'discard':
        item = outbox.discard(store.owner, item_id, by=by, turn_id=turn_id, store=store)
    else:
        raise ChannelError('invalid_decision', 'decision must be approve or discard')
    return item.view(_titles(store, [item])[item.thread])


def audit_page(owner: int, *, channel: str, before: str = '',
               store: ChannelStore | None = None) -> dict[str, Any]:
    """Contract ``AuditPage``: what Tofu recorded for other people on the owner's behalf, newest first."""
    store = _store(owner, store)
    if channel not in CHANNELS:
        raise ChannelError('invalid_channel', f'channel must be one of {list(CHANNELS)}')
    if not isinstance(before, str) or len(before) > 512:
        raise ChannelError('invalid_cursor', 'The cursor is not valid')
    items, following = store.audit_page(channel, before=before)
    titles = _titles(store, items)
    return {'items': [item.view(titles[item.thread]) for item in items], 'nextBefore': following}


__all__ = ['account_view', 'audit_page', 'configure_thread', 'create_command_chat',
           'decide_outbox', 'outbox_attachment', 'outbox_item_view', 'outbox_page', 'subscribe',
           'thread_page', 'thread_view', 'unsubscribe', 'update_account']
