"""Segmented project mailbox history: no lifetime count, stable cursors, exact receipts.

A three-original segment size makes many-segment histories cheap; the shipped
size is exercised by the legacy-record and >128 cases.
"""
from copy import deepcopy
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from lib.conversations import project_messages as pm
from lib.storage.errors import StorageError
from lib.tasks_pkg.message_input_receipts import matched_ids
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit

SEGMENT = 3


class LockedRecordClient(RecordClient):
    """Atomic record operations that yield between calls so writers interleave.

    As on both storage authorities, a put without ``expected_version`` writes
    the next version unconditionally and returns it, and a committed command ID
    replays its receipt (a changed payload under it conflicts).
    """
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.reads = []
        self.conflicts = 0
        self.receipts = {}

    def query(self, operation, payload):
        time.sleep(0)
        with self.lock:
            self.reads.append(payload['key'] if operation == 'record.get' else payload['prefix'])
            return super().query(operation, payload)

    def command(self, operation, payload, command_id):
        time.sleep(0)
        with self.lock:
            if command_id in self.receipts:
                committed, receipt = self.receipts[command_id]
                if committed != payload:
                    raise StorageError('database_conflict', 'command identity reused with a changed payload')
                return deepcopy(receipt)
            try:
                if operation == 'record.put' and 'expected_version' not in payload:
                    key = (payload['namespace'], payload['key'])
                    version = self.rows[key]['version'] + 1 if key in self.rows else 1
                    self.rows[key] = {'value': deepcopy(payload['value']), 'version': version}
                    receipt = {'key': payload['key'], 'version': version}
                else:
                    receipt = super().command(operation, payload, command_id)
            except StorageError:
                self.conflicts += 1
                raise
            self.receipts[command_id] = (deepcopy(payload), deepcopy(receipt))
            return receipt


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', SEGMENT)
    client = LockedRecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: client)
    conversations = {(owner, name): '/project' for owner in (1, 2) for name in ('alpha', 'beta', 'gamma', 'recipient')}

    def get(self, conv, *, user_id, include_messages):
        assert include_messages is False
        project = conversations.get((user_id, conv))
        return SimpleNamespace(metadata={'settings': {'projectPath': project}}) if project else None

    monkeypatch.setattr(pm.ConversationRepository, 'get', get)

    def task(conv, owner=1):
        return {'id': 'task-' + conv, 'convId': conv, '_userId': owner,
                'config': {'projectPath': conversations[(owner, conv)]}}

    return SimpleNamespace(client=client, conversations=conversations, task=task,
                           mailbox=pm.ProjectMailbox(1, '/project', 'recipient'))


def legacy_record(mailbox, count):
    """The pre-segmentation single record: one capped list, no locators, same message IDs."""
    def original(i):
        sender = 'alpha' if i % 2 else 'beta'
        return {'sender': sender, 'content': f'Legacy {i}', 'kind': 'reference', 'replyTo': '',
                'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{mailbox.key}:{sender}:{i}')), 'state': 'submitted',
                'revision': 1, 'senderTaskId': 'old-task', 'submittedAt': 1_700_000_000_000 + i, 'decision': None}
    return {'binding': mailbox.binding, 'messages': [original(i) for i in range(count)]}


def send(setup, sender, key, content=None, owner=1):
    return pm.execute('send_project_message', {
        'conversation_id': 'recipient', 'content': content or f'{sender} {key}', 'idempotency_key': key},
        setup.task(sender, owner))['message']


def walk(setup, conv, **args):
    """Every directory page, newest first, by following next_before_id."""
    pages, cursor = [], None
    while True:
        page = pm.execute('get_project_messages', {**args, **({'before_id': cursor} if cursor else {})},
                          setup.task(conv))
        pages.append(page)
        cursor = page['next_before_id']
        if cursor is None:
            return pages


def ids(pages):
    return [row['id'] for page in reversed(pages) for row in page['messages']]


def test_many_segments_page_back_completely_with_stable_cursors(setup):
    sent = [send(setup, 'alpha', str(i))['id'] for i in range(50)]
    head, _ = setup.mailbox.absorb()  # the recipient's turn
    assert head['sealed'] == 16 and len(head['messages']) == 2
    pages = walk(setup, 'recipient')
    assert ids(pages) == sent
    assert [page['total_messages'] for page in pages] == [50, 30, 10]
    assert [page['older_messages_omitted'] for page in pages] == [30, 10, 0]
    cursor = pages[0]['next_before_id']
    for i in range(50, 60):
        send(setup, 'alpha', str(i))
    later = pm.execute('get_project_messages', {'before_id': cursor}, setup.task('recipient'))
    assert later['messages'] == pages[1]['messages'] and later['total_messages'] == 30
    view = pm.page_view(1, '/project', 'recipient', cursor)
    assert [row['id'] for row in view['messages']] == [row['id'] for row in pages[1]['messages']]
    assert view['next_before_id'] == pages[1]['next_before_id']
    history = setup.mailbox.read()
    assert len(history) == 60 and [m['id'] for m in history[:50]] == sent
    assert all(setup.mailbox.get(m['id']) == m for m in history)


def test_outgoing_history_follows_one_sender_across_segments(setup):
    mine, theirs = [], []
    for i in range(120):
        sender = 'alpha' if i % 4 == 1 else 'beta'
        (mine if sender == 'alpha' else theirs).append(send(setup, sender, str(i))['id'])
    setup.mailbox.absorb()
    pages = walk(setup, 'alpha', direction='outgoing', conversation_id='recipient')
    assert ids(pages) == mine
    assert [page['total_messages'] for page in pages] == [30, 10]
    assert [page['older_messages_omitted'] for page in pages] == [10, 0]
    last = send(setup, 'alpha', 'open')  # cursor inside the open segment
    page = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                               'before_id': last['id']}, setup.task('alpha'))
    assert [row['id'] for row in page['messages']] == mine[-20:] and page['total_messages'] == 30
    with pytest.raises(pm.ProjectMessageError, match='Pagination'):
        pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                            'before_id': theirs[0]}, setup.task('alpha'))
    with pytest.raises(pm.ProjectMessageError, match='not found'):
        pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                            'message_id': theirs[0]}, setup.task('alpha'))
    assert ids(walk(setup, 'beta', direction='outgoing', conversation_id='recipient')) == theirs


def test_replay_after_uncertainty_returns_the_sealed_original(setup, monkeypatch):
    first = send(setup, 'alpha', 'first', 'Original evidence')
    for i in range(10):
        send(setup, 'beta', str(i))
    setup.mailbox.absorb()
    assert first['id'] not in {m['id'] for m in setup.mailbox.head()[0]['messages']}
    rows = deepcopy(setup.client.rows)
    assert send(setup, 'alpha', 'first', 'Original evidence') == first
    assert setup.client.rows == rows  # an exact replay writes nothing
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        send(setup, 'alpha', 'first', 'Rewritten evidence')
    while len(setup.mailbox.head()[0]['messages']) < SEGMENT:
        send(setup, 'beta', 'fill-' + str(len(setup.client.rows)))
        setup.mailbox.absorb()
    sealed = setup.mailbox.head()[0]['sealed']
    sealing = send(setup, 'gamma', 'sealing')  # absorbing it seals the full open segment
    original, lost = setup.client.command, []

    def command(operation, payload, command_id):
        result = original(operation, payload, command_id)
        if payload['key'] == setup.mailbox.key and not lost:
            lost.append(payload['value']['sealed'])
            raise StorageError('database_timeout', 'ACK lost after commit', retryable=True)
        return result

    monkeypatch.setattr(setup.client, 'command', command)
    setup.mailbox.absorb()
    assert lost == [sealed + 1]
    assert send(setup, 'gamma', 'sealing') == sealing
    history = [m['id'] for m in setup.mailbox.read()]
    assert len(history) == len(set(history)) and history[0] == first['id'] and history[-1] == sealing['id']


def test_interrupted_seal_is_completed_by_the_next_absorption(setup, monkeypatch):
    held = [send(setup, 'alpha', str(i)) for i in range(SEGMENT)]
    setup.mailbox.absorb()
    later = send(setup, 'beta', 'interrupted')
    original = setup.client.command

    def crash(operation, payload, command_id):
        if payload['key'].endswith(':pending'):  # the seal's last write before its head CAS
            raise StorageError('database_unavailable', 'process lost mid-seal')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', crash)
    with pytest.raises(StorageError):
        setup.mailbox.absorb()
    monkeypatch.setattr(setup.client, 'command', original)
    # The partial copy is not authoritative: the head still owns its originals.
    assert setup.mailbox.head()[0]['sealed'] == 0
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in [*held, later]]
    considered = setup.mailbox.consider({'message_id': held[0]['id'], 'decision': 'accepted',
                                         'note': 'Recorded after the partial copy', 'expected_revision': 1}, 'task-r')
    assert setup.mailbox.head()[0]['sealed'] == 1  # the receipt's own absorption completed the seal
    assert setup.mailbox.read() == [considered, *held[1:], later]
    assert setup.mailbox.get(held[0]['id']) == considered
    outgoing = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient'},
                          setup.task('alpha'))
    assert outgoing['total_messages'] == SEGMENT


def test_same_identity_racing_commits_once_at_its_claimed_ticket(setup, monkeypatch):
    held = [send(setup, 'beta', str(i)) for i in range(SEGMENT - 1)]
    original, raced = setup.client.command, []

    def command(operation, payload, command_id):
        if ':arrival:' in payload['key'] and payload['value'].get('sender') == 'alpha' and not raced:
            raced.append(1)
            # Before this attempt writes its original another sender appends, and the
            # same identity retries in full under the ticket this attempt claimed.
            send(setup, 'gamma', 'filler')
            raced.append(send(setup, 'alpha', 'duplicate'))
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', command)
    first = send(setup, 'alpha', 'duplicate')
    assert first == raced[1]
    history = setup.mailbox.read()
    assert [m['sender'] for m in history] == ['beta'] * (SEGMENT - 1) + ['alpha', 'gamma']
    assert history[:SEGMENT - 1] == held and history[SEGMENT - 1] == first
    for i in range(SEGMENT):
        send(setup, 'beta', 'later-' + str(i))
    setup.mailbox.absorb()
    assert setup.mailbox.head()[0]['sealed'] == 2 and setup.mailbox.get(first['id']) == first
    assert send(setup, 'alpha', 'duplicate') == first


def test_competing_writers_during_a_seal_keep_every_receipt(setup, monkeypatch):
    held = [send(setup, 'alpha', str(i)) for i in range(SEGMENT)]
    setup.mailbox.absorb()
    loser = send(setup, 'beta', 'loser')
    original, raced = setup.client.command, []

    def command(operation, payload, command_id):
        if payload['key'] == setup.mailbox.key and payload['value']['sealed'] and not raced:
            raced.append(1)
            # A recipient turn decides an original after this pass copied the segment,
            # and its own absorption seals the same segment first.
            setup.mailbox.consider({'message_id': held[1]['id'], 'decision': 'rejected',
                                    'note': 'Raced', 'expected_revision': 1}, 'task-r')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', command)
    setup.mailbox.absorb()
    assert raced and setup.client.conflicts
    history = setup.mailbox.read()
    assert [m['id'] for m in history] == [m['id'] for m in [*held, loser]]
    assert history[1]['decision'] == 'rejected' and history[1]['revision'] == 2


def test_old_originals_keep_explicit_consideration_and_input_receipts(setup):
    old = send(setup, 'alpha', 'old', 'Old request')
    backlog = [send(setup, 'beta', str(i))['id'] for i in range(3 * SEGMENT)]
    recipient = setup.task('recipient')
    detail = pm.execute('get_project_messages', {'message_id': old['id']}, recipient)['messages'][0]
    assert detail == old
    setup.mailbox.absorb()
    head = setup.mailbox.head()
    decision = {'message_id': old['id'], 'decision': 'deferred', 'note': 'Later', 'expected_revision': 1}
    considered = pm.execute('consider_project_message', decision, recipient)['message']
    assert considered['decision'] == 'deferred' and considered['revision'] == 2
    assert setup.mailbox.head() == head  # a sealed receipt never rewrites the open segment
    assert pm.execute('consider_project_message', decision, recipient)['message'] == considered
    with pytest.raises(pm.ProjectMessageError, match='revision'):
        pm.execute('consider_project_message', {**decision, 'decision': 'accepted'}, recipient)
    sent = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                               'message_id': old['id']}, setup.task('alpha'))
    assert sent['messages'][0]['decision'] == 'deferred'
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(recipient, messages, 0)
    assert messages[-1]['_projectMessageIds'] == backlog[:pm.LIMITS['batchSize']]
    pm.inject(recipient, messages, 1)
    assert messages[-1]['_projectMessageIds'] == backlog[pm.LIMITS['batchSize']:]
    assert recipient['_projectMessageAttempt']['ids'] == backlog
    recipient['_verifiedMessageInputIds'] = matched_ids(recipient, {'messages': messages})
    pm.confirm(recipient, 1)
    states = {m['id']: m['state'] for m in setup.mailbox.read()}
    assert all(states[item] == 'input_confirmed' for item in backlog) and states[old['id']] == 'submitted'
    pending = setup.client.rows[(pm.CONTRACT['namespace'], setup.mailbox.key + ':pending')]['value']
    assert pending['senders'] == {}
    pm.inject(recipient, messages, 2)
    assert len(messages) == 3 and recipient['_projectMessageAttempt']['ids'] == []


def test_departed_sender_backlog_is_kept_and_skipped_without_reading_its_segments(setup):
    departed = [send(setup, 'gamma', str(i))['id'] for i in range(4 * SEGMENT)]
    current = [send(setup, 'alpha', str(i))['id'] for i in range(2 * SEGMENT)]
    setup.conversations[(1, 'gamma')] = '/other'
    departed_segments = {f'{setup.mailbox.key}:segment:{n}' for n in range(4)}
    setup.mailbox.absorb()  # an earlier turn of the recipient
    setup.client.reads.clear()
    recipient = setup.task('recipient')
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(recipient, messages, 0)
    assert messages[-1]['_projectMessageIds'] == current
    assert not departed_segments & set(setup.client.reads)
    assert all(m['state'] == 'submitted' for m in setup.mailbox.read() if m['sender'] == 'gamma')
    setup.conversations[(1, 'gamma')] = '/project'  # returning restores eligibility
    returned = setup.task('recipient')
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(returned, messages, 0)
    assert messages[-1]['_projectMessageIds'] == departed[:pm.LIMITS['batchSize']]


def test_legacy_single_record_mailbox_stays_readable_and_grows_into_segments(setup, monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', 128)
    mailbox = pm.ProjectMailbox(1, '/project', 'recipient')
    record = legacy_record(mailbox, 128)
    setup.client.rows[(pm.CONTRACT['namespace'], mailbox.key)] = {'value': deepcopy(record), 'version': 7}
    legacy = record['messages']
    assert ids(walk(setup, 'recipient')) == [m['id'] for m in legacy]
    assert mailbox.get(legacy[0]['id']) == legacy[0]
    considered = mailbox.consider({'message_id': legacy[5]['id'], 'decision': 'accepted', 'note': 'Kept',
                                   'expected_revision': 1}, 'task-r')
    grown = send(setup, 'gamma', 'after the old lifetime count')
    head, _ = mailbox.absorb()
    assert head['sealed'] == 1 and head['messages'] == [grown] and head['unlocated'] == 0
    assert mailbox.read() == [*legacy[:5], considered, *legacy[6:], grown]
    assert mailbox.submit('alpha', {'content': 'Legacy 5', 'idempotency_key': '5'}, 'replay') == considered
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        mailbox.submit('alpha', {'content': 'Rewritten', 'idempotency_key': '5'}, 'replay')
    assert ids(walk(setup, 'recipient')) == [m['id'] for m in mailbox.read()]


def test_concurrent_senders_commit_every_original_exactly_once(setup):
    senders, failures = ('alpha', 'beta', 'gamma'), []

    def run(sender):
        try:
            for i in range(15):
                for _ in range(64):
                    try:
                        send(setup, sender, str(i))
                        break
                    except pm.ProjectMessageError as exc:
                        assert 'Concurrent/uncertain' in str(exc)  # retry the same identity
                else:
                    raise AssertionError('send never committed')
        except BaseException as exc:  # surfaced on the main thread
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(sender,)) for sender in senders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    history = setup.mailbox.read()
    assert len(history) == len({m['id'] for m in history}) == 45
    for sender in senders:
        own = [m for m in history if m['sender'] == sender]
        assert [m['content'] for m in own] == [f'{sender} {i}' for i in range(15)]
        assert ids(walk(setup, sender, direction='outgoing', conversation_id='recipient')) == [m['id'] for m in own]
    assert all(setup.mailbox.get(m['id']) == m for m in history)


def test_owner_and_project_histories_are_isolated(setup):
    mine = [send(setup, 'alpha', str(i))['id'] for i in range(2 * SEGMENT + 1)]
    foreign = send(setup, 'alpha', '0', owner=2)
    assert foreign['id'] not in mine
    for mailbox in (pm.ProjectMailbox(2, '/project', 'recipient'), pm.ProjectMailbox(1, '/other', 'recipient')):
        assert all(mailbox.get(item) is None for item in mine)
        with pytest.raises(pm.ProjectMessageError, match='Pagination'):
            mailbox.page(mine[0])
        with pytest.raises(pm.ProjectMessageError, match='not found'):
            mailbox.consider({'message_id': mine[0], 'decision': 'accepted', 'note': '', 'expected_revision': 1}, 't')
    assert [m['id'] for m in pm.ProjectMailbox(2, '/project', 'recipient').read()] == [foreign['id']]
    assert setup.mailbox.get(foreign['id']) is None
    with pytest.raises(pm.ProjectMessageError, match='not found'):
        pm.execute('get_project_messages', {'message_id': mine[0]}, setup.task('recipient', owner=2))


def test_a_burst_of_reporters_commits_at_first_try_without_writing_the_head(setup, monkeypatch):
    workers = [f'worker-{i}' for i in range(120)]
    for worker in workers:
        setup.conversations[(1, worker)] = '/project'
    written, original = [], setup.client.command

    def observed(operation, payload, command_id):
        written.append(payload['key'])
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', observed)
    start, sent, failures = threading.Barrier(len(workers)), {}, []

    def report(worker):
        start.wait()
        try:  # one model round: no retry
            sent[worker] = send(setup, worker, 'report', f'{worker} verified its checkout')
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=report, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert setup.mailbox.key not in written and setup.client.conflicts == 0
    history = setup.mailbox.read()
    assert sorted(m['id'] for m in history) == sorted(m['id'] for m in sent.values())
    setup.mailbox.absorb()  # the recipient's turn
    assert setup.mailbox.read() == history
    assert ids(walk(setup, 'recipient')) == [m['id'] for m in history]
    for worker in workers[:3]:
        assert ids(walk(setup, worker, direction='outgoing', conversation_id='recipient')) == [sent[worker]['id']]


def test_an_exact_retry_returns_the_same_record_and_writes_nothing(setup):
    first = send(setup, 'alpha', 'once', 'Evidence')
    for absorbed in (False, True):
        if absorbed:
            setup.mailbox.absorb()
            for i in range(2 * SEGMENT):
                send(setup, 'beta', f'later-{i}')
            setup.mailbox.absorb()
        rows = deepcopy(setup.client.rows)
        assert send(setup, 'alpha', 'once', 'Evidence') == first
        assert setup.client.rows == rows
        with pytest.raises(pm.ProjectMessageError, match='changed'):
            send(setup, 'alpha', 'once', 'Rewritten evidence')
    assert [m['id'] for m in setup.mailbox.read()].count(first['id']) == 1


@pytest.mark.parametrize('lost', ['before_claim', 'before_original'])
@pytest.mark.parametrize('retry', ['before_void', 'after_void'])
def test_a_sender_lost_after_its_ticket_is_delivered_exactly_once_by_its_retry(setup, monkeypatch, lost, retry):
    before = [send(setup, 'beta', str(i)) for i in range(2)]
    original = setup.client.command

    def crash(operation, payload, command_id):
        if payload['key'].startswith(setup.mailbox.key + (':message:' if lost == 'before_claim' else ':arrival:')):
            raise StorageError('database_unavailable', 'process lost after drawing its ticket')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', crash)
    with pytest.raises(StorageError):
        send(setup, 'alpha', 'lost', 'Report')
    monkeypatch.setattr(setup.client, 'command', original)
    after = [send(setup, 'beta', f'after-{i}') for i in range(2)]
    # The lost ticket never holds back later originals, for readers or for absorption.
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in before + after]
    setup.mailbox.absorb()  # the first pass that finds the ticket missing stalls on it
    assert setup.mailbox.head()[0]['stalled'] == 3
    if retry == 'after_void':
        head, _ = setup.mailbox.absorb()  # the next pass voids it and absorbs past it
        assert head['voided'] == [3] and head['tickets'] == 5 and head['stalled'] is None
        assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in before + after]
    retried = send(setup, 'alpha', 'lost', 'Report')
    assert send(setup, 'alpha', 'lost', 'Report') == retried
    expected = (before + [retried] + after if retry == 'before_void' else before + after + [retried])
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in expected]
    setup.mailbox.absorb()
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in expected]
    assert setup.mailbox.get(retried['id']) == retried


def test_merged_readers_and_the_absorbed_history_agree_on_order(setup):
    sent = []
    for turn in range(6):
        sent += [send(setup, sender, f'{turn}-{i}') for i, sender in enumerate(('alpha', 'beta', 'gamma', 'beta'))]
        merged = setup.mailbox.read()
        pages = ids(walk(setup, 'recipient'))
        mine = ids(walk(setup, 'beta', direction='outgoing', conversation_id='recipient'))
        if turn % 2:
            setup.mailbox.absorb()
        assert setup.mailbox.read() == merged
        assert ids(walk(setup, 'recipient')) == pages == [m['id'] for m in merged]
        assert ids(walk(setup, 'beta', direction='outgoing', conversation_id='recipient')) == mine
        assert mine == [m['id'] for m in merged if m['sender'] == 'beta']
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in sent]
    head, _ = setup.mailbox.head()  # the last absorption took every ticket
    assert head['tickets'] == len(sent) == head['sealed'] * SEGMENT + len(head['messages'])


def test_a_retry_of_a_head_era_original_without_a_locator_never_duplicates_it(setup, monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', 128)
    mailbox = pm.ProjectMailbox(1, '/project', 'recipient')
    record = legacy_record(mailbox, 4)
    setup.client.rows[(pm.CONTRACT['namespace'], mailbox.key)] = {'value': deepcopy(record), 'version': 7}
    legacy = record['messages']
    retried = mailbox.submit('alpha', {'content': 'Legacy 1', 'idempotency_key': '1'}, 'replay')
    assert retried['id'] == legacy[1]['id']
    assert mailbox.read() == legacy  # readers never see the original twice
    head, _ = mailbox.absorb()
    assert head['voided'] == [1] and head['unlocated'] == 0  # the copy is dropped, every original located
    assert mailbox.read() == legacy and ids(walk(setup, 'recipient')) == [m['id'] for m in legacy]
    assert mailbox.submit('alpha', {'content': 'Legacy 1', 'idempotency_key': '1'}, 'replay') == legacy[1]


def test_a_pass_that_meets_the_identical_seal_and_head_already_committed_completes_without_a_retry(setup, monkeypatch):
    """CAS-CONFLICT-1 (11-13): two recipient passes that absorb the same arrivals write identical records.
    In the N=210 rounds cells 203 of 208 mailbox conflicts found the record already holding the attempted
    value, yet the later pass aborted its seal (absorption deferred, acknowledgement lost) or spent a
    retry. Such a conflict is this write's own outcome, committed by the other writer: it succeeds at
    the version read, with no second write."""
    held = [send(setup, 'alpha', str(i)) for i in range(SEGMENT)]
    setup.mailbox.absorb()
    later = send(setup, 'beta', 'later')
    other = pm.ProjectMailbox(1, '/project', 'recipient')
    segment = setup.mailbox._key('segment', segment=0)
    original, raced, ours, inside = setup.client.command, [], [], []

    def command(operation, payload, command_id):
        if not inside:
            if payload['key'] == segment and not raced:
                raced.append(1)
                inside.append(1)  # the other pass seals the same segment and absorbs the same arrival first
                try:
                    other.absorb()
                finally:
                    inside.clear()
            if payload['key'] in (segment, setup.mailbox.key):
                ours.append(payload['key'])
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', command)
    head, revision = setup.mailbox.absorb()
    stored, stored_revision = setup.mailbox.head()
    assert raced and revision == stored_revision and head['tickets'] == stored['tickets'] == SEGMENT + 1
    assert stored['sealed'] == head['sealed'] == 1
    assert ours.count(segment) == 1 and ours.count(setup.mailbox.key) == 1  # each record written once
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in [*held, later]]


def test_a_cas_conflict_on_a_record_already_holding_the_attempted_value_is_that_writes_success(setup):
    key = setup.mailbox._key('pending')
    value = {'binding': setup.mailbox.binding, 'senders': {}}
    assert pm.ProjectMailbox(1, '/project', 'recipient')._put(key, value, 0) == 1
    assert setup.mailbox._put(key, value, 0) == 1
    with pytest.raises(StorageError) as refused:
        setup.mailbox._put(key, {**value, 'senders': {'alpha': [0]}}, 0)
    assert refused.value.code == 'database_conflict'  # a different committed value still refuses the write
    assert setup.mailbox._pending_index() == ({}, 1)


def test_a_later_confirmation_of_a_sealed_original_keeps_the_first_receipt(setup, monkeypatch):
    """The 5 unequal conflicts at N=210: two confirmations of the same sealed originals. The later one's
    copy differs only in its own receipt fields; its retry reads the receipt and writes nothing."""
    held = [send(setup, 'alpha', str(i)) for i in range(SEGMENT)]
    setup.mailbox.absorb()
    send(setup, 'beta', 'next')
    setup.mailbox.absorb()
    first, second = pm.ProjectMailbox(1, '/project', 'recipient'), pm.ProjectMailbox(1, '/project', 'recipient')
    segment, ids = setup.mailbox._key('segment', segment=0), [held[0]['id']]
    original, raced = setup.client.command, []

    def command(operation, payload, command_id):
        if payload['key'] == segment and not raced:
            raced.append(1)
            first.confirm(ids, 'task-recipient', 4)  # another round's confirmation lands first
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', command)
    conflicts = setup.client.conflicts
    second.confirm(ids, 'task-recipient', 7)
    receipt = setup.mailbox.get(held[0]['id'])
    assert raced and setup.client.conflicts == conflicts + 1
    assert receipt['state'] == 'input_confirmed' and receipt['inputRound'] == 5 and receipt['revision'] == 2
def _earlier_writer_append(setup, mailbox, sender, key):
    """An append by a writer that predates tickets (01f24726f2): it claims {segment} and writes back the head
    it read, so fields it does not know survive. This helper never seals."""
    namespace = pm.CONTRACT['namespace']
    row = setup.client.rows[(namespace, mailbox.key)]
    head = deepcopy(row['value'])
    assert len(head['messages']) < SEGMENT
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{mailbox.key}:{sender}:{key}'))
    message = {'sender': sender, 'content': f'{sender} {key}', 'kind': 'reference', 'replyTo': '',
               'id': message_id, 'state': 'submitted', 'revision': 1, 'senderTaskId': 'earlier-writer',
               'submittedAt': int(time.time() * 1000), 'decision': None}
    setup.client.rows[(namespace, mailbox._key('message', message=message_id))] = {
        'value': {'segment': head['sealed']}, 'version': 1}
    head['messages'].append(message)
    setup.client.rows[(namespace, mailbox.key)] = {'value': head, 'version': row['version'] + 1}
    return message


def test_a_head_converted_back_with_its_tickets_resumes_them_without_voiding_any(setup):
    """Roll back below the ticketed layout and forward again: the converted head keeps its tickets through
    the earlier writers' appends, and the next pass absorbs only tickets drawn after the roll-forward."""
    mailbox = setup.mailbox
    before = [send(setup, sender, str(i)) for i, sender in enumerate(('alpha', 'beta', 'gamma', 'alpha'))]
    assert mailbox.convert_back() == {'tickets': 4, 'claims': 4, 'head': True}
    assert [m['id'] for m in mailbox.read()] == [m['id'] for m in before]
    during = [_earlier_writer_append(setup, mailbox, 'beta', 'during the rollback'),
              _earlier_writer_append(setup, mailbox, 'gamma', 'also during it')]
    setup.client.reads.clear()
    after = [send(setup, 'alpha', 'rolled forward'), send(setup, 'beta', 'rolled forward too')]
    head, _ = mailbox.absorb()
    assert head['tickets'] == 6 and head['voided'] == []
    assert not {mailbox._key('arrival', ticket=t) for t in range(1, 5)} & set(setup.client.reads)
    expected = [m['id'] for m in before + during + after]
    assert [m['id'] for m in mailbox.read()] == expected
    assert ids(walk(setup, 'recipient')) == expected
    assert all(mailbox.get(message_id)['id'] == message_id for message_id in expected)
    assert send(setup, 'alpha', '0')['id'] == before[0]['id']
    assert send(setup, 'beta', 'during the rollback')['id'] == during[0]['id']
    assert [m['id'] for m in mailbox.read()] == expected


def _mailbox_rows(setup, mailbox):
    namespace = pm.CONTRACT['namespace']
    return {key: deepcopy(row) for (space, key), row in setup.client.rows.items()
            if space == namespace and key.startswith(mailbox.key)}


def test_a_mailbox_converted_back_holds_every_message_in_the_head_era_form(setup, monkeypatch):
    """R4b's down-converter, offline: every drawn ticket absorbed (the one never written voided), every claim
    naming a segment and the head stored under ``messages`` with every original located, so writers that
    predate tickets read the whole history. Converting again writes nothing."""
    mailbox = setup.mailbox
    absorbed = [send(setup, sender, str(i)) for i, sender in enumerate(('alpha', 'beta', 'gamma', 'alpha', 'beta'))]
    mailbox.absorb()
    arriving = [send(setup, 'gamma', 'not yet absorbed'), send(setup, 'alpha', 'nor this one')]
    original = setup.client.command

    def lose_original(operation, payload, command_id):
        if payload['key'].startswith(mailbox.key + ':arrival:'):
            raise StorageError('database_unavailable', 'process lost after claiming its ticket')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', lose_original)
    with pytest.raises(StorageError):
        send(setup, 'beta', 'lost', 'Report')
    monkeypatch.setattr(setup.client, 'command', original)
    lost = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{mailbox.key}:beta:lost'))
    history = [m['id'] for m in absorbed + arriving]

    assert mailbox.convert_back() == {'tickets': 3, 'claims': 8, 'head': True}

    rows = _mailbox_rows(setup, mailbox)
    stored = rows[mailbox.key]['value']
    assert 'open' not in stored and stored['unlocated'] == 0 and stored['stalled'] is None
    assert stored['tickets'] == 8 and stored['voided'] == [8]
    assert pm._valid_messages(stored['messages']) and stored['sealed'] * SEGMENT + len(stored['messages']) == 7
    claims = {key.rsplit(':', 1)[1]: row['value'] for key, row in rows.items() if ':message:' in key}
    assert claims == {**{message_id: {'segment': position // SEGMENT} for position, message_id in enumerate(history)},
                      lost: {'segment': 0}}
    for number in range(stored['sealed']):
        segment = rows[mailbox._key('segment', segment=number)]['value']['messages']
        assert pm._valid_messages(segment) and [m['id'] for m in segment] == history[number * SEGMENT:(number + 1) * SEGMENT]
    assert [m['id'] for m in stored['messages']] == history[stored['sealed'] * SEGMENT:]
    assert [m['id'] for m in mailbox.read()] == history
    assert mailbox.convert_back() == {'tickets': 0, 'claims': 0, 'head': False}
    assert _mailbox_rows(setup, mailbox) == rows


def test_a_mailbox_that_never_took_a_ticket_is_left_as_it_is(setup, monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', 128)
    mailbox = pm.ProjectMailbox(1, '/project', 'recipient')
    setup.client.rows[(pm.CONTRACT['namespace'], mailbox.key)] = {'value': legacy_record(mailbox, 4), 'version': 7}
    rows = _mailbox_rows(setup, mailbox)
    assert mailbox.convert_back() == {'tickets': 0, 'claims': 0, 'head': False}
    assert _mailbox_rows(setup, mailbox) == rows
