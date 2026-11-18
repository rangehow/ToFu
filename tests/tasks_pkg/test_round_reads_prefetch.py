"""A ready round end sends the next round's boundary reads; the round keeps them.

The steer check used to send the round's shared exchange on the Turn thread
and wait for it. ``prefetch_round_reads`` puts it on the wire at the previous
round's ``ready`` end (the send half of one pipelined exchange), so the
authority answers while the round starts; ``begin_round_reads`` keeps that
exchange, and without one sends its own the same way. Readers still get
exactly the listed answers once, and a client that cannot split an exchange
keeps the first-reader send.
"""
import threading

import pytest

from lib.tasks_pkg import round_reads

pytestmark = pytest.mark.unit

REQUESTS = [('turn.get', {'turn': 't1'}), ('record.get', {'id': 'r1'})]


class Client:
    def __init__(self):
        self.exchanges, self.sent, self.lone, self.discarded = [], [], [], []
        self.release = threading.Event()
        self.release.set()

    def _answer(self, requests):
        self.release.wait(10)
        self.exchanges.append([op for op, _ in requests])
        return [{'answer': op, 'exchange': len(self.exchanges)} for op, _ in requests]

    def query_many(self, requests, deadline=None):
        return self._answer(requests)

    def send_query_many(self, requests, deadline=None):
        self.sent.append([op for op, _ in requests])
        client = self

        class Pending:
            def answers(self):
                return client._answer(requests)

            def discard(self):
                client.discarded.append([op for op, _ in requests])
        return Pending()

    def query(self, operation, payload=None, **options):
        self.lone.append(operation)
        return {'answer': operation, 'live': True}


class Unsplittable(Client):
    send_query_many = None


@pytest.fixture
def wired(monkeypatch):
    client = Client()
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda *a, **k: client)
    monkeypatch.setattr(round_reads, '_requests', lambda task: list(REQUESTS))
    return client


def test_ready_end_puts_the_next_rounds_reads_on_the_wire(wired):
    client = wired
    task = {'id': 'task-1'}
    sent = round_reads.prefetch_round_reads(task)
    assert client.sent == [['turn.get', 'record.get']] and client.exchanges == []
    # The next round keeps the prefetched exchange and sends nothing new.
    assert round_reads.begin_round_reads(task) is sent
    assert len(client.sent) == 1
    assert round_reads.round_reads(task).query(*REQUESTS[0]) == {'answer': 'turn.get', 'exchange': 1}
    assert round_reads.round_reads(task).query(*REQUESTS[1]) == {'answer': 'record.get', 'exchange': 1}
    assert client.lone == [] and len(client.exchanges) == 1
    # Each answer serves its listed read once; a repeat reads live.
    assert round_reads.round_reads(task).query(*REQUESTS[0])['live'] is True
    round_reads.end_round_reads(task)
    assert round_reads.round_reads(task) is None


def test_a_round_without_a_ready_end_sends_afresh(wired):
    client = wired
    task = {'id': 'task-3'}
    first = round_reads.begin_round_reads(task)
    second = round_reads.begin_round_reads(task)
    assert second is not first and len(client.sent) == 2


def test_concurrent_readers_share_the_one_sent_exchange(wired):
    client = wired
    client.release.clear()
    task = {'id': 'task-4'}
    reads = round_reads.prefetch_round_reads(task)
    answers = {}
    readers = [threading.Thread(target=lambda i=i: answers.setdefault(i, reads.query(*REQUESTS[i])))
               for i in range(2)]
    for reader in readers:
        reader.start()
    readers[0].join(0.2)
    assert readers[0].is_alive(), 'readers wait for the sent exchange'
    client.release.set()
    for reader in readers:
        reader.join(10)
    assert [answers[i]['exchange'] for i in range(2)] == [1, 1]
    assert len(client.exchanges) == 1 and client.lone == []


def test_a_client_that_cannot_split_keeps_the_first_reader_send(monkeypatch):
    client = Unsplittable()
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda *a, **k: client)
    monkeypatch.setattr(round_reads, '_requests', lambda task: list(REQUESTS))
    task = {'id': 'task-5'}
    reads = round_reads.prefetch_round_reads(task)
    assert client.exchanges == []
    assert reads.query(*REQUESTS[1]) == {'answer': 'record.get', 'exchange': 1}


def test_an_untaken_exchange_is_drained_when_the_loop_ends(wired):
    client = wired
    task = {'id': 'task-6'}
    round_reads.prefetch_round_reads(task)  # the last round end: no next round
    round_reads.end_round_reads(task)
    assert client.discarded == [['turn.get', 'record.get']] and client.exchanges == []
    # A taken exchange has nothing left to drain.
    reads = round_reads.begin_round_reads(task)
    reads.query(*REQUESTS[0])
    round_reads.end_round_reads(task)
    assert len(client.discarded) == 1


def test_a_failed_exchange_falls_back_to_live_reads():
    from lib.storage.port import PrefetchedReads

    class Failing(Client):
        send_query_many = None

        def query_many(self, requests, deadline=None):
            raise RuntimeError('exchange lost')

    client = Failing()
    reads = PrefetchedReads(client, list(REQUESTS))
    assert reads.query(*REQUESTS[0]) == {'answer': 'turn.get', 'live': True}
    assert client.lone == ['turn.get']
