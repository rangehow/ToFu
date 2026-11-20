"""Per-Turn deliverable facts: derived once, kept exact by the authority's Turn delta."""
from types import SimpleNamespace

import pytest

from lib import ttl_cache
from lib.conversations.lead_turn_facts import TURN_TOMBSTONE_RETENTION_SECONDS, LeadTurnFacts
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Lane:
    """A fake authority: Turns with projection revisions, an updated-at clock and tombstones."""

    def __init__(self, *turns):
        self.clock = 1_000
        self.turns = {}
        self.deleted = {}
        self.walks = 0
        self.deltas = []
        self.refuse = None
        for turn_id, lane in turns:
            self.put(turn_id, lane)

    def put(self, turn_id, lane='main'):
        self.clock += 10
        old = self.turns.get(turn_id)
        self.turns[turn_id] = {'turnId': turn_id, 'laneId': lane, 'updated': self.clock,
                               'ordinal': old['ordinal'] if old else len(self.turns) + 1,
                               'projectionRevision': (old['projectionRevision'] + 1) if old else 1}

    def delete(self, turn_id):
        self.clock += 10
        self.turns.pop(turn_id)
        self.deleted[turn_id] = self.clock

    def walk(self, conversation_id, owner):
        self.walks += 1
        return [dict(turn) for turn in sorted(self.turns.values(), key=lambda turn: -turn['ordinal'])
                if turn['laneId'] == 'main']

    def delta(self, conversation_id, owner, since_ms, known):
        self.deltas.append(dict(known))
        if self.refuse:
            raise self.refuse
        lower = since_ms - 5  # The authority's overlap window.
        return {'turns': [dict(turn) for turn in self.turns.values() if turn['updated'] >= lower
                          and turn['projectionRevision'] > known.get(turn['turnId'], -1)],
                'deletedTurnIds': [turn_id for turn_id, at in self.deleted.items() if at >= lower],
                'serverNowMs': self.clock}


def _facts(lane):
    derived = []

    def derive(turn):
        derived.append(turn['turnId'])
        return {'turnId': turn['turnId'], 'ordinal': turn['ordinal'], 'revision': turn['projectionRevision']}
    facts = LeadTurnFacts(derive, walk=lane.walk, delta=lane.delta, now_ms=lambda: lane.clock)
    return facts, derived


def _ids(facts):
    return [entry['turnId'] for entry in facts]


def test_an_unchanged_conversation_is_read_once_and_then_only_asked_for_changes():
    lane = Lane(('a', 'main'), ('b', 'main'), ('side', 'branch'))
    facts, derived = _facts(lane)
    assert _ids(facts.read(1, 'lead')) == ['b', 'a']
    assert _ids(facts.read(1, 'lead')) == ['b', 'a']
    assert lane.walks == 1 and derived == ['b', 'a']
    assert lane.deltas[-1] == {'a': 1, 'b': 1}


def test_new_changed_deleted_and_moved_turns_are_applied_exactly():
    lane = Lane(('a', 'main'), ('b', 'main'), ('c', 'main'))
    facts, derived = _facts(lane)
    facts.read(1, 'lead')
    derived.clear()
    lane.put('b')                    # edited: a newer projection revision
    lane.put('d')                    # new
    lane.delete('a')                 # deleted: a tombstone
    lane.put('c', lane='branch')     # left the main lane
    assert [(entry['turnId'], entry['revision']) for entry in facts.read(1, 'lead')] == [('d', 1), ('b', 2)]
    assert sorted(derived) == ['b', 'd'] and lane.walks == 1


def test_a_turn_changed_while_the_first_walk_runs_is_in_the_next_delta():
    lane = Lane(('a', 'main'))
    walk = lane.walk

    def racing(conversation_id, owner):
        rows = walk(conversation_id, owner)
        lane.put('a')  # Committed after the walk read it.
        return rows
    lane.walk = racing
    facts, _ = _facts(lane)
    assert facts.read(1, 'lead')[0]['revision'] == 1
    assert facts.read(1, 'lead')[0]['revision'] == 2


def test_a_delta_refused_at_the_authority_bound_rebuilds_from_the_lane_walk():
    lane = Lane(('a', 'main'))
    facts, _ = _facts(lane)
    facts.read(1, 'lead')
    lane.refuse = StorageError('database_resource_exhausted', 'turn delta exceeds 2000 changed rows')
    with pytest.raises(StorageError):
        facts.read(1, 'lead')  # The rebuild's own watermark read is refused too: the error surfaces.
    lane.refuse = None
    lane.put('b')
    assert _ids(facts.read(1, 'lead')) == ['b', 'a']


def test_an_entry_older_than_the_authoritys_tombstone_retention_is_rebuilt(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(ttl_cache, 'time', SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]))
    lane = Lane(('a', 'main'))
    facts, _ = _facts(lane)
    facts.read(1, 'lead')
    now[0] += TURN_TOMBSTONE_RETENTION_SECONDS - 1
    facts.read(1, 'lead')
    assert lane.walks == 1
    now[0] += TURN_TOMBSTONE_RETENTION_SECONDS + 1
    facts.read(1, 'lead')
    assert lane.walks == 2


def test_owners_and_conversations_never_share_an_entry():
    lane = Lane(('a', 'main'))
    facts, _ = _facts(lane)
    facts.read(1, 'lead')
    facts.read(2, 'lead')
    facts.read(1, 'other')
    assert lane.walks == 3


def test_memory_pressure_relief_drops_the_entries():
    lane = Lane(('a', 'main'))
    facts, _ = _facts(lane)
    facts.read(1, 'lead')
    assert ttl_cache.clear_all_caches() >= 1
    facts.read(1, 'lead')
    assert lane.walks == 2
