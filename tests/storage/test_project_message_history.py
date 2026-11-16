"""Segmented project mailbox history on real SQLite, PostgreSQL and native authorities.

Online, the mailbox uses only generic record point reads, expected-version writes
and unconditional writes (whose returned version is a send's ticket), so both
storage authorities serve it unchanged: no listing, deletion or eviction. The
offline conversion back to the head-era form pages its claims with record.list.
The fixture never opens or switches the user's selected live authority.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest

from lib.conversations import project_messages as pm

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api

SEGMENT = 3
REPORTERS = 110


@pytest.fixture
def postgres_cluster(_postgres_bootstrap):
    # The read and the write demand pools each open one server connection per
    # concurrent request, so REPORTERS sends at once (each reads, then writes)
    # need that many in each pool beyond the default cluster's room.
    from tests.support.postgres_cluster import owned_postgres_cluster
    with owned_postgres_cluster(max_connections=2 * REPORTERS + 16, bootstrap=_postgres_bootstrap) as dsn:
        yield dsn


@pytest.fixture
def authority(semantic_authority, monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', SEGMENT)
    factory, owner, _ = semantic_authority
    project = '/project/' + uuid.uuid4().hex

    def mailbox(*, user=owner, path=project):
        return pm.ProjectMailbox(user, path, 'recipient', client=factory)

    return mailbox, factory, owner


def submit(box, sender, key, content=None):
    return box.submit(sender, {'content': content or f'{sender} {key}', 'idempotency_key': key}, 'task-' + sender)


def every_page(box, sender=''):
    rows, cursor = [], ''
    while True:
        page = box.page(cursor, sender)
        rows[:0] = page['messages']
        if page['total'] == len(page['messages']):
            return rows
        cursor = page['messages'][0]['id']


def test_many_segment_history_pages_replays_and_receipts_old_originals(authority):
    mailbox, _, _ = authority
    box = mailbox()
    sent = [submit(box, 'alpha' if i % 4 == 1 else 'beta', str(i)) for i in range(100)]
    assert every_page(box) == sent  # read before the recipient absorbs
    head, _ = box.absorb()
    assert head['sealed'] == 33 and len(head['messages']) == 1
    assert every_page(box) == sent
    alpha = [m for m in sent if m['sender'] == 'alpha']
    assert box.page('', 'alpha')['total'] == len(alpha) == 25
    assert every_page(box, 'alpha') == alpha
    first = sent[0]
    assert submit(box, 'beta', '0') == first  # replay after an uncertain ACK finds the sealed original
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        submit(box, 'beta', '0', 'Rewritten')
    considered = box.consider({'message_id': first['id'], 'decision': 'accepted', 'note': 'Old original',
                               'expected_revision': 1}, 'task-r')
    assert considered['revision'] == 2 and box.get(first['id']) == considered
    with pytest.raises(pm.ProjectMessageError, match='revision'):
        box.consider({'message_id': first['id'], 'decision': 'rejected', 'note': 'stale',
                      'expected_revision': 1}, 'task-r')
    box.confirm([sent[1]['id'], sent[2]['id'], sent[-1]['id']], 'task-r', 0)
    assert [box.get(m['id'])['state'] for m in (sent[1], sent[2], sent[-1])] == ['input_confirmed'] * 3
    head, _ = box.head()
    assert [m['id'] for _, m in box.pending(head, lambda sender: True, set())] == [m['id'] for m in sent[3:-1]]
    assert [m['id'] for _, m in box.pending(head, lambda sender: sender == 'alpha', set())] == [
        m['id'] for m in alpha if m['id'] not in {sent[1]['id']}]


def test_concurrent_appends_cross_segment_boundaries_exactly_once(authority):
    mailbox, _, _ = authority

    def run(sender):
        for i in range(12):
            for _ in range(64):
                try:
                    submit(mailbox(), sender, str(i))
                    break
                except pm.ProjectMessageError:
                    continue  # uncertain: retry the same identity
            else:
                raise AssertionError('send never committed')

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(run, ['alpha', 'beta', 'gamma']))
    box = mailbox()
    history = box.read()
    assert len(history) == len({m['id'] for m in history}) == 36
    for sender in ('alpha', 'beta', 'gamma'):
        own = [m for m in history if m['sender'] == sender]
        assert [m['content'] for m in own] == [f'{sender} {i}' for i in range(12)]
        assert every_page(box, sender) == own
    assert box.find([m['id'] for m in history]) == {m['id']: m for m in history}


def test_a_hundred_concurrent_reporters_commit_at_first_try_and_none_is_lost(authority):
    mailbox, _, _ = authority
    reporters = [f'worker-{i}' for i in range(REPORTERS)]
    start = threading.Barrier(len(reporters))

    def report(sender):
        start.wait()
        return submit(mailbox(), sender, 'report', f'{sender} verified its checkout')

    with ThreadPoolExecutor(len(reporters)) as pool:
        sent = list(pool.map(report, reporters))  # any refused send raises here
    box = mailbox()
    history = box.read()
    assert sorted(m['id'] for m in history) == sorted(m['id'] for m in sent)
    assert box.head()[0]['messages'] == []  # no send wrote the head
    assert [submit(box, sender, 'report', f'{sender} verified its checkout') for sender in reporters[:5]] == sent[:5]
    box.absorb()
    assert box.read() == history and every_page(box) == history


def test_legacy_single_record_mailbox_is_read_and_sealed_without_loss(authority, monkeypatch):
    mailbox, factory, _ = authority
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', 128)
    box = mailbox()
    # The pre-segmentation single record: one capped list, no locators, same message IDs.
    legacy = [{'sender': sender, 'content': f'{sender} {i}', 'kind': 'reference', 'replyTo': '',
               'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{box.key}:{sender}:{i}')), 'state': 'submitted',
               'revision': 1, 'senderTaskId': 'old-task', 'submittedAt': 1_700_000_000_000 + i, 'decision': None}
              for i, sender in ((i, 'alpha' if i % 2 else 'beta') for i in range(128))]
    record = {'namespace': pm.CONTRACT['namespace'], 'key': box.key}
    factory(write=True).command('record.put', {**record, 'expected_version': 0,
                                               'value': {'binding': box.binding, 'messages': legacy}},
                                'legacy-' + uuid.uuid4().hex)
    assert box.read() == legacy and every_page(box) == legacy
    grown = submit(box, 'gamma', 'after the old lifetime count')
    assert box.absorb()[0]['sealed'] == 1
    assert every_page(box) == [*legacy, grown]
    assert box.get(legacy[0]['id']) == legacy[0] and submit(box, 'beta', '0') == legacy[0]


def test_owner_and_project_histories_are_isolated(authority):
    mailbox, _, owner = authority
    mine = mailbox()
    sent = [submit(mine, 'alpha', str(i)) for i in range(2 * SEGMENT + 1)]
    for other in (mailbox(path='/project/' + uuid.uuid4().hex), mailbox(user=owner + 1)):
        assert other.read() == [] and all(other.get(m['id']) is None for m in sent)
        with pytest.raises(pm.ProjectMessageError, match='Pagination'):
            other.page(sent[0]['id'])
        with pytest.raises(pm.ProjectMessageError, match='not found'):
            other.consider({'message_id': sent[0]['id'], 'decision': 'accepted', 'note': '',
                            'expected_revision': 1}, 'task-r')
        same_key = submit(other, 'alpha', '0')
        assert same_key['id'] != sent[0]['id'] and mine.get(same_key['id']) is None
    assert mine.read() == sent


def test_a_mailbox_converts_back_and_resumes_its_tickets(authority):
    """The offline conversion finds every claim with record.list on each authority; the converted head
    reads the whole history, a second conversion writes nothing, and the next ticket resumes after it."""
    mailbox, factory, _ = authority
    box = mailbox()
    sent = [submit(box, sender, str(i)) for i, sender in enumerate(('alpha', 'beta', 'gamma', 'alpha', 'beta'))]
    box.absorb()
    sent.append(submit(box, 'gamma', 'still arriving'))
    assert box.convert_back() == {'tickets': 1, 'claims': 6, 'head': True}

    def stored(key):
        return factory().query('record.get', {'namespace': pm.CONTRACT['namespace'], 'key': key})['value']

    head = stored(box.key)
    assert 'open' not in head and head['tickets'] == 6 and head['voided'] == [] and head['unlocated'] == 0
    for position, message in enumerate(sent):
        assert stored(box._key('message', message=message['id'])) == {'segment': position // SEGMENT}
    assert [m['id'] for m in box.read()] == [m['id'] for m in sent]
    assert box.convert_back() == {'tickets': 0, 'claims': 0, 'head': False}
    later = submit(box, 'alpha', 'after the roll-forward')
    head, _ = box.absorb()
    assert head['tickets'] == 7 and head['voided'] == []
    assert [m['id'] for m in box.read()] == [m['id'] for m in sent + [later]]
