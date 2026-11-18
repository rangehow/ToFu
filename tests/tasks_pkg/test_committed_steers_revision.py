"""A Turn head read before the executor's own later write still reuses that write's records.

The round's boundary reads are sent at the previous round's ready end, while
that round's deferred events still land. Their Turn head can then carry a
revision older than the executor's last fenced write, whose records include
every steer committed up to it: no full Turn read is needed. A head newer than
that write is another writer's and is read in full.
"""
import pytest

from lib import turn_lifecycle
from lib.tasks_pkg import committed_steers

pytestmark = pytest.mark.unit

STEER = {'blockId': 'steer:1', 'deliveryState': 'pending', 'previews': [{'text': 'hi'}]}


@pytest.fixture
def turn(monkeypatch):
    state = {'head': 5, 'written': (7, [STEER]), 'full_reads': 0}

    def get_turn(conversation_id, turn_id, *, user_id, include_projection=True, reads=None):
        if include_projection:
            state['full_reads'] += 1
            return {'currentAttemptId': 'a1', 'projectionRevision': state['head'],
                    'projection': {'_userSteerInjects': [dict(STEER, blockId='steer:other')]}}
        return {'currentAttemptId': 'a1', 'projectionRevision': state['head']}

    monkeypatch.setattr(turn_lifecycle, 'get_turn', get_turn)
    monkeypatch.setattr(turn_lifecycle, 'executor_written_steers', lambda task: state['written'])
    from lib.tasks_pkg import conversation_delivery
    monkeypatch.setattr(conversation_delivery, 'completion_conversation_id', lambda task: 'c1')
    return state


def _task():
    return {'id': 't1', 'convId': 'c1', '_turnId': 'turn-1', '_attemptId': 'a1', '_userId': 1}


@pytest.mark.parametrize('head', [5, 7])
def test_a_head_at_or_before_the_executors_write_uses_its_records(turn, head):
    turn['head'] = head
    assert committed_steers.live_turn_steer_records(_task()) == [STEER]
    assert turn['full_reads'] == 0


def test_a_head_after_the_executors_write_reads_the_turn(turn):
    turn['head'] = 8
    records = committed_steers.live_turn_steer_records(_task())
    assert [record['blockId'] for record in records] == ['steer:other']
    assert turn['full_reads'] == 1
