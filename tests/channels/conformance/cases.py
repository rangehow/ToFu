"""The channel protocol's conformance cases (docs/CHANNEL_PROTOCOL.md, Conformance).

Each case states one protocol promise as platform stimuli (driver verbs) and
core observations (what was recorded, routed and received). A case raises
``NotApplicable`` when the platform offers nothing to exercise and
``AssertionError`` when the promise is broken. Cases are written against the
protocol, never against one adapter: the same case runs for every adapter.
"""
from __future__ import annotations

from lib.channels import quotes
from lib.channels.model import ChannelError
from lib.channels.protocol.contract import health_entry
from tests.channels.conformance.harness import Driver, NotApplicable, attachment_record, record_outbox_faults, unique


def _one(driver: Driver, thread, text):
    records = driver.core.with_text(thread, text)
    assert len(records) == 1, f'{text!r} recorded {len(records)} times'
    return records[0]


def duplicate_delivery(d: Driver) -> None:
    """At-least-once delivery: the platform delivers a message twice; the core records and routes it once."""
    d.connect()
    msg = d.post(d.watched_thread(), unique('duplicate'))
    d.redeliver(msg)
    d.settle()
    record = _one(d, msg.thread, msg.text)
    assert d.core.routed(record) == 1, f'routed {d.core.routed(record)} times'


def out_of_order(d: Driver) -> None:
    """An older message that reaches the core after a newer one of its thread is still recorded and routed."""
    d.connect()
    thread = d.watched_thread()
    older, newer = d.pair(thread)
    d.deliver(newer)
    d.deliver(older)
    d.settle()
    for msg in (older, newer):
        record = _one(d, thread, msg.text)
        assert d.core.routed(record) == 1, f'{msg.text!r} routed {d.core.routed(record)} times'


def edit(d: Driver) -> None:
    """An edit is recorded as a revision of the original event: never dropped, never a new message."""
    d.connect()
    thread = d.watched_thread()
    msg = d.post(thread, unique('original'))
    d.settle()
    revised = unique('edited')
    d.edit(msg, revised)
    d.settle()
    record = _one(d, thread, msg.text)
    assert not d.core.with_text(thread, revised) or record.get('revisions'), 'the edit became a new message'
    assert any(revised in (revision.get('text') or '') for revision in record.get('revisions') or ()), \
        'the edit was not recorded on the original event'


def recall(d: Driver) -> None:
    """A recall marks the original event recalled and removes its text from Tofu (owner default)."""
    d.connect()
    thread = d.watched_thread()
    msg = d.post(thread, unique('recalled'))
    d.settle()
    original = _one(d, thread, msg.text)
    d.recall(msg)
    d.settle()
    record = next((row for row in d.core.events(thread) if row['eventId'] == original['eventId']), None)
    assert record is not None and record.get('recalled') is True, 'the recall was not recorded'
    assert msg.text not in (record.get('text') or ''), 'the recalled text stayed in Tofu'


def auth_expiry(d: Driver) -> None:
    """A refused credential is auth_required with an owner remedy, and the supervisor stops restarting."""
    d.connect()
    d.expire_auth()
    health = d.health()
    assert health and health['state'] == 'auth_required', health
    entry = health_entry(health['code'])
    assert entry['retry'] == 'owner' and entry['remedy'] not in ('none', 'wait'), entry
    d.assert_no_restart()


def displaced_stays_down(d: Driver) -> None:
    """Another login taking the session is displaced: no reconnect or login push, across a restart, until restored."""
    d.connect()
    d.displace()
    health = d.health()
    assert health and health['state'] == 'displaced', health
    entry = health_entry(health['code'])
    assert (entry['retry'], entry['remedy']) == ('owner', 'restore_session'), entry
    d.assert_no_restart()
    d.assert_no_restart(boot=True)


def rate_limited(d: Driver) -> None:
    """A rate-limited send is not attempted again inside the platform's window, then goes out exactly once."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    d.rate_limit_sends()
    item = d.enqueue(thread, unique('limited'))
    d.execute()
    first = d.attempts(item)
    assert first >= 1, 'the send was never attempted'
    d.execute()
    assert d.attempts(item) == first, 'sent again inside the platform\'s rate-limit window'
    d.end_rate_limit()
    d.execute()
    d.execute()
    assert d.received(thread).count(item.text) == 1, d.received(thread)
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state


def partial_history_failure(d: Driver) -> None:
    """A failed history read keeps the connection and live delivery; the gap is replayed later."""
    d.connect()
    thread = d.watched_thread()
    gap = d.post_unseen(thread, unique('gap'))
    d.fail_next_history()
    d.reconnect()
    live = d.post(thread, unique('live'))
    d.settle()
    assert d.alive(), 'a failed history read tore the connection down'
    assert d.core.routed(_one(d, thread, live.text)) == 1, 'live events stopped after a failed history read'
    d.tick()
    d.settle()
    assert d.core.routed(_one(d, thread, gap.text)) == 1, 'the gap was not replayed'


def degraded_is_typed(d: Driver) -> None:
    """A failed history read is reported as degraded (history_failed), not as connected or disconnected."""
    d.connect()
    thread = d.watched_thread()
    d.post_unseen(thread, unique('gap'))
    d.fail_next_history()
    d.reconnect()
    d.settle()
    health = d.health()
    assert health and (health['state'], health['code']) == ('degraded', 'history_failed'), health


def runtime_fault_is_typed(d: Driver) -> None:
    """The adapter's own runtime refusing is runtime_missing: no login is offered (SSO_NODE_UNAVAILABLE lesson)."""
    d.runtime_fault()
    health = d.health()
    assert health and health['state'] == 'runtime_missing', health
    assert health_entry(health['code'])['remedy'] in ('repair_runtime', 'install_adapter', 'update_adapter'), health


def network_fault_is_typed(d: Driver) -> None:
    """A network failure is disconnected with a network or transport code, not an adapter error or a login."""
    d.connect()
    d.network_fault()
    health = d.health()
    assert health and health['state'] == 'disconnected', health
    assert health['code'] in ('network_unreachable', 'transport_lost', 'platform_unavailable'), health


def _reconcilable(d: Driver) -> bool:
    """Whether the platform offers any way to learn the outcome of a send (P4's ladder)."""
    descriptor = d.descriptor
    return bool(descriptor['outbound']['lookup'] or descriptor['history']['read']
                or descriptor['outbound']['idempotentResend'] != 'none')


def _settled_once(d: Driver, thread, item) -> None:
    """Exactly one copy reached the platform; the item is sent, or held unknown for the owner where
    the platform offers no way to reconcile (never re-sent blindly)."""
    copies = d.received(thread).count(item.text)
    assert copies == 1, f'{copies} copies reached the platform'
    expected = 'sent' if _reconcilable(d) else 'unknown'
    assert d.core.item(item.id).state == expected, d.core.item(item.id).state


def restart_mid_send(d: Driver) -> None:
    """The platform accepted a send and the process died before the receipt: after restart, no second copy."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    item = d.enqueue(thread, unique('restart'))
    d.deliver_without_receipt(item)
    d.restart()
    d.execute()
    d.execute()
    _settled_once(d, thread, item)


def unknown_reconciliation(d: Driver) -> None:
    """A delivered send whose answer was lost is reconciled, never sent blindly again."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    d.lose_next_answer()
    item = d.enqueue(thread, unique('unknown'))
    d.execute()
    assert d.core.item(item.id).state == 'unknown', d.core.item(item.id).state
    d.execute()
    d.execute()
    _settled_once(d, thread, item)


def _readable(d: Driver) -> bool:
    """Whether the platform offers a read that could prove a send missing (P4's lookup or history rung)."""
    return bool(d.descriptor['outbound']['lookup'] or d.descriptor['history']['read'])


def reconciliation_read_failure(d: Driver) -> None:
    """A failed or incomplete reconciliation read is no evidence that a send is missing: the item stays
    unknown and nothing is sent again until a complete read settles it (P4). OpenClaw 2026.11.1 replays an
    unknown send only when a fresh send is provably safe (docs/plugins/sdk-channel-outbound.md)."""
    if not _readable(d):
        raise NotApplicable('the platform offers no read that could prove a send missing')
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    d.lose_next_answer()
    item = d.enqueue(thread, unique('unproven'))
    d.execute()
    assert d.core.item(item.id).state == 'unknown', d.core.item(item.id).state
    d.fail_reconciliation_reads(thread)
    d.execute()
    d.execute()
    copies = d.received(thread).count(item.text)
    assert copies == 1, f'{copies} copies after failed reconciliation reads'
    assert d.core.item(item.id).state in ('unknown', 'sending'), d.core.item(item.id).state
    d.heal_reconciliation_reads(thread)
    d.execute()
    _settled_once(d, thread, item)


def stalled_thread_isolation(d: Driver) -> None:
    """An unknown item whose reconciliation read keeps failing holds only its own thread: another thread's
    queued item goes out in the same pass (C5). The stalled thread is the one the executor visits first
    (the store's order: thread key, then queue position), so the stall cannot hide behind the order."""
    if not _readable(d):
        raise NotApplicable('the platform offers no read that could fail during reconciliation')
    d.connect()
    stalled, other = sorted(d.send_threads(), key=str)
    for thread in (stalled, other):
        d.prepare_send(thread)
    d.lose_next_answer()
    held = d.enqueue(stalled, unique('held'))
    d.execute()
    assert d.core.item(held.id).state == 'unknown', d.core.item(held.id).state
    d.fail_reconciliation_reads(stalled)
    item = d.enqueue(other, unique('independent'))
    d.execute()
    assert d.received(other).count(item.text) == 1, 'a stalled reconciliation in another thread held this send'
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state


def accepted_confirmation(d: Driver) -> None:
    """A file the platform accepted (receipt ``accepted``) is confirmed only by its echo or the thread (P4,
    C10). When the next pass cannot confirm it, it never waits silently or forever: a failed read holds it
    as history_failed, a history that answers without it holds it as needs_owner_confirmation, and each
    raises one outbox fault. It is never sent again and holds no other thread, and the first read that
    shows it settles it. On 2026-11-09 an accepted PDF sat awaiting_confirmation for good behind an
    expired SSO ticket."""
    d.connect()
    thread, other = sorted(d.send_threads(), key=str)
    file = d.descriptor['identities'][d.host.identity_for(thread)]['attachments'].get('file')
    if not file:
        raise NotApplicable('the platform carries no files natively')
    if file.get('confirmation') == 'ack':
        raise NotApplicable('the provider ACK settles the file; no thread history is available')
    for each in (thread, other):
        d.prepare_send(each)
    faults = record_outbox_faults(d.env)
    d.accept_files_unseen(thread)
    item = d.enqueue(thread, unique('with a file'), attachments=[attachment_record(d.env.tmp_path)])
    d.execute()
    accepted = d.core.item(item.id)
    assert (accepted.state, accepted.error) == ('unknown', 'awaiting_confirmation'), (accepted.state, accepted.error)
    assert faults == [], 'a file awaiting its confirmation point was reported'
    d.fail_reconciliation_reads(thread)
    independent = d.enqueue(other, unique('independent'))
    d.execute()
    held = d.core.item(item.id)
    assert (held.state, held.error) == ('unknown', 'history_failed'), (held.state, held.error)
    assert d.received(other).count(independent.text) == 1, 'an unconfirmed file held another thread'
    d.heal_reconciliation_reads(thread)
    d.execute()
    held = d.core.item(item.id)
    assert (held.state, held.error) == ('unknown', 'needs_owner_confirmation'), (held.state, held.error)
    assert [fault[0] for fault in faults] == [item.id, item.id], faults
    d.show_files(thread)
    d.execute()
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state
    assert d.file_sends(thread) == 1, f'the file went {d.file_sends(thread)} times'


def unknown_keeps_its_identity(d: Driver) -> None:
    """A bot send whose answer was lost is reconciled under the bot and never repeated as the owner: only a
    definite refusal moves the rest of an item to the next identity (P7). OpenClaw 2026.11.1: failure is not
    permission to send the same payload through another path (docs/plugins/sdk-channel-outbound.md)."""
    if not {'bot', 'self'} <= set(d.descriptor['identities']):
        raise NotApplicable('the platform has one outbound identity')
    d.connect()
    thread = d.bot_thread()
    d.prepare_send(thread)
    d.lose_next_bot_answer()
    item = d.enqueue(thread, unique('bot answer lost'))
    d.execute()
    assert d.core.item(item.id).state == 'unknown', d.core.item(item.id).state
    d.execute()
    d.execute()
    senders = [identity for identity, text in d.received_as(thread) if text == item.text]
    assert senders == ['bot'], f'the item reached the platform as {senders}'
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state


def attachment_degradation(d: Driver) -> None:
    """A file the platform cannot carry is degraded to a note naming it: never refused, never dropped."""
    d.connect()
    thread = d.send_thread()
    identity = d.descriptor['identities'][d.host.identity_for(thread)]
    if 'file' in identity['attachments']:
        raise NotApplicable('the platform carries files natively')
    d.prepare_send(thread)
    record = attachment_record(d.env.tmp_path)
    try:
        item = d.enqueue(thread, unique('with a file'), attachments=[record])
    except ChannelError as error:
        raise AssertionError(f'refused instead of degraded: {error.code}') from error
    d.execute()
    sent = [text for text in d.received(thread) if item.text.split()[-1] in text]
    assert sent and any(record['name'] in text for text in sent), 'the file was dropped without a note'


def echo_loop(d: Driver) -> None:
    """This account's own send coming back from the platform is never input (no loop)."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    item = d.enqueue(thread, unique('echoed'))
    d.execute()
    assert d.core.item(item.id).state == 'sent'
    d.echo(item)
    d.settle()
    routed = [record for record in d.core.with_text(thread, item.text) if d.core.routed(record)]
    assert not routed, 'an echo of our own send was routed as input'


def self_send_outside_outbox(d: Driver) -> None:
    """A message this account sent through another Tofu path (no outbox item, Tofu's marker), replayed by
    backfill or delivered live, is never owner input: the 2026-11-09 restore replayed 19 such CLI sends
    from the command chat as owner commands."""
    d.connect()
    msg = d.self_send_outside_outbox(unique('sent by a Tofu tool'))
    d.settle()
    for record in d.core.with_text(msg.thread, msg.text):
        assert record.get('disposition') not in ('command', 'candidate'), record.get('disposition')


def other_bots(d: Driver) -> None:
    """Another bot's message is never a command and never a candidate for review or auto-reply."""
    d.connect()
    msg = d.bot_post(unique('bot says'))
    d.settle()
    for record in d.core.with_text(msg.thread, msg.text):
        assert record.get('disposition') not in ('command', 'candidate'), record.get('disposition')


def _delivered(d: Driver, msg) -> dict:
    """The input projection of the one turn dispatch that carried ``msg``'s event."""
    record = _one(d, msg.thread, msg.text)
    messages = d.core.deliveries(record)
    assert len(messages) == 1, f'{msg.text!r} was delivered {len(messages)} times'
    return messages[0]


def _quote_of(message: dict) -> str:
    found = message.get('replyQuotes') or []
    assert len(found) == 1, f'expected one quote above the owner\'s words, got {found!r}'
    return found[0]


def quote_own_send(d: Driver) -> None:
    """The owner quotes Tofu's own message: the Router sees its text, labelled as Tofu's, above his
    verbatim words. On 2026-11-09 his quote-reply 「性能分数呢」 reached the Router bare, though the quote
    named our outbox item."""
    d.connect()
    thread = d.command_thread()
    d.prepare_send(thread)
    item = d.enqueue(thread, unique('tofu said'))
    d.execute()
    sent = d.core.item(item.id)
    assert sent.state == 'sent', sent.state
    reply = d.quote(thread, d.sent(sent), unique('what about it'))
    d.settle()
    message = _delivered(d, reply)
    assert message['content'] == reply.text, 'the owner\'s words were not delivered verbatim'
    quote = _quote_of(message)
    assert item.text in quote, f'the quoted text is missing: {quote!r}'
    if d.quote_names_message:
        assert quotes.AUTHOR_LABELS['tofu'] in quote, quote
    else:
        # Only the platform's copy speaks for the quoted message: Tofu's text never passes as the owner's.
        assert quotes.AUTHOR_LABELS['owner'] not in quote, quote


def quote_owner_message(d: Driver) -> None:
    """Quoting the owner's own earlier message shows it as his earlier message, never as a new command."""
    d.connect()
    thread = d.command_thread()
    first = d.owner_post(thread, unique('owner first'))
    d.settle()
    reply = d.quote(thread, first, unique('again'))
    d.settle()
    message = _delivered(d, reply)
    assert message['content'] == reply.text, 'the owner\'s words were not delivered verbatim'
    quote = _quote_of(message)
    assert first.text in quote, f'the quoted text is missing: {quote!r}'
    authors = ('owner',) if d.quote_names_message else ('owner', 'unknown')  # as the copy names its sender
    assert any(quotes.AUTHOR_LABELS[author] in quote for author in authors), quote


def quote_beyond_records(d: Driver) -> None:
    """A quote of a message Tofu never recorded is resolved from the platform (its copy, or a lookup of
    that one message where the adapter declares one) or shown explicitly as unreadable, never bare."""
    d.connect()
    thread = d.command_thread()
    old = d.unrecorded(thread, unique('never recorded'))
    reply = d.quote(thread, old, unique('this one?'))
    d.settle()
    message = _delivered(d, reply)
    assert message['content'] == reply.text
    quote = _quote_of(message)
    assert old.text in quote or quote.startswith(quotes.UNREADABLE_PREFIX), quote
    if d.descriptor['history'].get('message'):
        assert old.text in quote, f'a declared lookup did not resolve the quote: {quote!r}'


def quote_from_another_person(d: Driver) -> None:
    """Another person's quote reaches the Router as evidence: the quoted message keeps its own author's
    standing (not the owner's), escaped inside the event's ``<channel-quote>``."""
    d.connect()
    thread = d.quote_thread()
    original = d.quotable(thread, unique('first said'))
    d.settle()
    reply = d.quote(thread, original, unique('replying'), owner=False)
    d.settle()
    content = _delivered(d, reply)['content']
    authors = ('other',) if d.quote_names_message else ('other', 'unknown')  # as the copy names its sender
    assert '<channel-quote ' in content and any(f'author="{author}"' in content for author in authors), content
    assert 'state="resolved"' in content and original.text in content, content


def quote_recalled(d: Driver) -> None:
    """A quote of a recalled message shows that it was recalled, never the old text (P2's recall rule).
    The quote's own copy of the message is as the quoting client embedded it, so a quote that reaches
    the core after the recall (a catch-up) is hidden only by the core's recall record."""
    d.connect()
    thread = d.watched_thread()
    original = d.post(thread, unique('soon recalled'))
    d.settle()
    d.recall(original)
    d.settle()
    reply = d.quote(thread, original, unique('what was that'), owner=False)
    d.settle()
    content = _delivered(d, reply)['content']
    assert '<channel-quote ' in content and 'state="recalled"' in content, content
    assert original.text not in content, 'the recalled text reached the Router'


def quote_lookup_failure(d: Driver) -> None:
    """A failed lookup (history reads failing, as with 2026-11-09's rescode=92002) shows the quote as
    unreadable with its reason, and the message, like every one after it, is still delivered."""
    if not d.descriptor['history'].get('message'):
        raise NotApplicable('the platform offers no lookup of one message by ID')
    d.connect()
    thread = d.command_thread()
    old = d.unrecorded(thread, unique('unreachable'))
    d.fail_quote_lookups(thread)
    reply = d.quote(thread, old, unique('and this?'), copy=False)
    after = d.owner_post(thread, unique('meanwhile'))
    d.settle()
    quote = _quote_of(_delivered(d, reply))
    assert quote.startswith(quotes.UNREADABLE_PREFIX) and 'lookup_failed' in quote, quote
    assert old.text not in quote
    assert _delivered(d, after)['content'] == after.text, 'a failed lookup held the next message'


CASES = {
    'duplicate_delivery': duplicate_delivery,
    'out_of_order': out_of_order,
    'edit': edit,
    'recall': recall,
    'auth_expiry': auth_expiry,
    'displaced_stays_down': displaced_stays_down,
    'rate_limited': rate_limited,
    'partial_history_failure': partial_history_failure,
    'degraded_is_typed': degraded_is_typed,
    'runtime_fault_is_typed': runtime_fault_is_typed,
    'network_fault_is_typed': network_fault_is_typed,
    'restart_mid_send': restart_mid_send,
    'unknown_reconciliation': unknown_reconciliation,
    'reconciliation_read_failure': reconciliation_read_failure,
    'stalled_thread_isolation': stalled_thread_isolation,
    'accepted_confirmation': accepted_confirmation,
    'unknown_keeps_its_identity': unknown_keeps_its_identity,
    'attachment_degradation': attachment_degradation,
    'echo_loop': echo_loop,
    'self_send_outside_outbox': self_send_outside_outbox,
    'other_bots': other_bots,
    'quote_own_send': quote_own_send,
    'quote_owner_message': quote_owner_message,
    'quote_beyond_records': quote_beyond_records,
    'quote_from_another_person': quote_from_another_person,
    'quote_recalled': quote_recalled,
    'quote_lookup_failure': quote_lookup_failure,
}

__all__ = ['CASES']
