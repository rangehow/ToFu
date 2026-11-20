"""Durable toy only: SQLite owns all witnesses here, unlike current dispatch authority.

No runtime readiness writer/codec/executor is exported. Ordinary repository validation
seeds synthetic registrations; copying its verdict cannot fence shared owners.
"""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from lib.project_dispatch import continuation
from tests.project_mod.test_coordinator_continuation import (
    run as run, runtime as runtime, base_runtime as base_runtime, context as context,
    activate, controls, delegate, end,
)
from tests.project_mod.test_coordinator_wake_lane_events import poll_reports

pytestmark = pytest.mark.unit


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class _ToyStore:
    """All authority facts are test-owned rows in ONE SQLite transaction domain."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS registrations (
                member TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                witnesses TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wakes (
                id TEXT PRIMARY KEY, recipient TEXT NOT NULL CHECK(recipient='coordinator'),
                members TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consumed (
                identity TEXT PRIMARY KEY, wake TEXT NOT NULL REFERENCES wakes(id));
        ''')

    def close(self):
        self.db.close()

    def seed(self, members):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for member, witnesses in members.items():
                identity = _digest({'toyVersion': 1, 'member': member, 'witnesses': witnesses})
                self.db.execute('INSERT INTO registrations VALUES (?,?,?)',
                                (member, identity, _json(witnesses)))
                for key, value in witnesses.items():
                    self.db.execute('INSERT OR IGNORE INTO facts VALUES (?,?)', (key, _json(value)))
                    assert self.db.execute('SELECT value FROM facts WHERE key=?', (key,)).fetchone() == (_json(value),)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def receipts(self):
        return dict(self.db.execute('SELECT identity,wake FROM consumed ORDER BY identity'))

    def identities(self):
        return dict(self.db.execute('SELECT member,identity FROM registrations ORDER BY member'))

    def admit(self, members, *, authority='isolated-toy', fault=None):
        if authority != 'isolated-toy':
            raise PermissionError('unsupported shared-authority witnesses: binding/Stop/source/delivery; no admission')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            identities = []
            for member in sorted(set(members)):
                row = self.db.execute('SELECT identity,witnesses FROM registrations WHERE member=?', (member,)).fetchone()
                if row is None:
                    raise PermissionError('not registered')
                identity, encoded = row
                for key, expected in json.loads(encoded).items():
                    actual = self.db.execute('SELECT value FROM facts WHERE key=?', (key,)).fetchone()
                    if actual != (_json(expected),):
                        raise PermissionError('witness mismatch: ' + key)
                identities.append(identity)
            prior = self.receipts()
            fresh = sorted(set(identities) - prior.keys())
            if fresh:
                wake = 'toy-coordinator:' + _digest(fresh)
                self.db.execute('INSERT INTO wakes VALUES (?,?,?)', (wake, 'coordinator', _json(fresh)))
                if fault == 'before-consumption':
                    raise RuntimeError('simulated precommit failure')
                self.db.executemany('INSERT INTO consumed VALUES (?,?)', [(identity, wake) for identity in fresh])
            answer = {identity: self.receipts()[identity] for identity in identities}
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if fault == 'lost-ack':
            raise ConnectionError('simulated response loss AFTER commit')
        return answer


@pytest.fixture
def proof(run, monkeypatch, tmp_path):
    r = run
    monkeypatch.setattr('lib.conversations.coordinator_continuation._questions', lambda *a: [])
    monkeypatch.setattr('lib.integration_control.release_publication_hold', lambda *a, **kw: None)
    continuation._LANE_HOLDS.clear()
    continuation._LANE_OBSERVATIONS.clear()
    path = tmp_path / 'readiness-toy.sqlite'
    store = _ToyStore(path)
    try:
        first = delegate(r)
        end(r, first)
        sha = 'a' * 40
        workspace = r.workspaces[first['execution']['taskId']]
        workspace['checkpoint_sha'] = sha
        workspace['origin']['delivery']['checkpointSha'] = sha
        assert r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                      decision='accept', note='Synthetic proof prerequisite', checkpoint_sha=sha)['decision'] == 'accept'
        first = r.c.repo.read(first['id'])
        members = {}
        candidates = {}
        for member in ('A', 'B', 'C'):
            request = deepcopy(first['request'])
            request['dependencies'] = [{'assignmentId': first['id'], 'grantRevision': 1,
                                        'resultRevision': 1, 'checkpointSha': sha}]
            stage = r.c.repo.authorize_from_coordinator(request, task=r.task, command_id='toy-stage-' + member)
            assert r.c.repo.validate(r.c.repo.admission(stage['id']))['id'] == stage['id']
            assert r.c.repo.dependency_states(stage)[0]['status'] == 'ready'
            assert stage['execution'] is None
            execution = first['execution']
            attempt = r.attempts[execution['attemptId']]
            assert attempt['attemptId'] == execution['attemptId']
            assert attempt['turnId'] == execution['turnId']
            assert workspace['task_id'] == execution['taskId']
            members[member] = {
                'source': {'human': deepcopy(r.c.turn), 'disposition': 'open', 'stopped': False,
                           'originalRequest': deepcopy(stage['request']['delegation'])},
                'binding': deepcopy(r.c.binding.value),
                'candidate:' + member: stage,
                'prerequisite': first,
                'prerequisite-attempt': deepcopy(attempt),
                'prerequisite-turn': deepcopy(r.turns[execution['turnId']]),
                'workspace-delivery': deepcopy(workspace),
            }
            candidates[member] = stage
        store.seed(members)
        yield r, store, path, candidates
    finally:
        store.close()
        continuation._LANE_HOLDS.clear()
        continuation._LANE_OBSERVATIONS.clear()


def test_aggregate_overlap_consumes_members_not_changing_sets(proof):
    r, store, path, candidates = proof
    births, calls = deepcopy(r.created), list(r.calls)
    ids = store.identities()
    ab = store.admit(['B', 'A'])
    assert set(ab) == {ids['A'], ids['B']} and len(set(ab.values())) == 1
    bc = store.admit(['B', 'C'])
    assert bc[ids['B']] == ab[ids['B']]
    assert bc[ids['C']] != ab[ids['A']]
    receipts = store.receipts()
    assert len(receipts) == 3
    with sqlite3.connect(path) as reopened:
        assert dict(reopened.execute('SELECT identity,wake FROM consumed')) == receipts
        assert set(reopened.execute('SELECT recipient,members FROM wakes')) == {
            ('coordinator', _json(sorted([ids['A'], ids['B']]))),
            ('coordinator', _json([ids['C']])),
        }
    assert store.admit(['B']) == {ids['B']: ab[ids['B']]}
    assert store.admit(['C', 'A', 'B']) == receipts
    assert r.created == births and r.calls == calls
    assert all(r.c.repo.read(stage['id'])['execution'] is None for stage in candidates.values())
    assert controls(r) == [], 'toy receipts are NOT real dispatch grants'


def test_lost_response_then_close_reopen_preserves_exact_receipt(proof):
    r, store, path, candidates = proof
    ids = store.identities()
    with pytest.raises(ConnectionError, match='AFTER commit'):
        store.admit(['A', 'B'], fault='lost-ack')
    committed = store.receipts()
    assert set(committed) == {ids['A'], ids['B']}
    store.close()
    store.db = sqlite3.connect(path, isolation_level=None)
    store.db.execute('PRAGMA foreign_keys=ON')
    assert store.identities() == ids
    assert store.admit(['B', 'A']) == committed
    assert store.receipts() == committed
    assert store.db.execute('SELECT count(*) FROM wakes').fetchone() == (1,)
    assert all(r.c.repo.read(stage['id'])['execution'] is None for stage in candidates.values())


def test_closed_request_preserves_historical_consumption_but_admits_nothing(proof):
    _, store, path, _ = proof
    original = store.admit(['A'])
    source = json.loads(store.db.execute("SELECT value FROM facts WHERE key='source'").fetchone()[0])
    source['disposition'] = 'closed'
    store.db.execute("UPDATE facts SET value=? WHERE key='source'", (_json(source),))
    with pytest.raises(PermissionError, match='witness mismatch: source'):
        store.admit(['A', 'B'])
    with sqlite3.connect(path) as reopened:
        assert dict(reopened.execute('SELECT identity,wake FROM consumed')) == original
        assert reopened.execute('SELECT count(*) FROM wakes').fetchone() == (1,)
    assert store.receipts() == original


def test_unregistered_member_does_not_partially_consume_registered_member(proof):
    _, store, _, _ = proof
    with pytest.raises(PermissionError, match='not registered'):
        store.admit(['A', 'unknown'])
    assert store.receipts() == {}
    assert store.db.execute('SELECT count(*) FROM wakes').fetchone() == (0,)


def test_precommit_failure_rolls_back_wake_and_every_consumed_member(proof):
    _, store, path, _ = proof
    with pytest.raises(RuntimeError, match='precommit'):
        store.admit(['A', 'B'], fault='before-consumption')
    with sqlite3.connect(path) as reopened:
        assert reopened.execute('SELECT count(*) FROM wakes').fetchone() == (0,)
        assert reopened.execute('SELECT count(*) FROM consumed').fetchone() == (0,)
    assert len(store.admit(['A', 'B'])) == 2


@pytest.mark.parametrize('key', [
    'source', 'binding', 'candidate:A', 'prerequisite', 'prerequisite-attempt',
    'prerequisite-turn', 'workspace-delivery',
])
def test_changed_or_missing_exact_witness_fails_closed(proof, key):
    _, store, _, _ = proof
    store.db.execute('UPDATE facts SET value=? WHERE key=?', (_json({'changed': True}), key))
    with pytest.raises(PermissionError, match='witness mismatch: ' + key):
        store.admit(['A', 'B'])
    assert store.receipts() == {}
    assert store.db.execute('SELECT count(*) FROM wakes').fetchone() == (0,)
    store.db.execute('DELETE FROM facts WHERE key=?', (key,))
    with pytest.raises(PermissionError, match='witness mismatch'):
        store.admit(['A'])


@pytest.mark.parametrize('key,path,value', [
    ('source', ('disposition',), 'closed'),
    ('source', ('stopped',), True),
    ('source', ('human', 'actor'), 'virtual_user'),
    ('binding', ('revision',), 2),
    ('candidate:A', ('grant', 'revision'), 2),
    ('candidate:A', ('request', 'dependencies', 0, 'resultRevision'), 2),
    ('prerequisite-attempt', ('status',), 'running'),
    ('workspace-delivery', ('origin', 'delivery', 'state'), 'pending'),
    ('workspace-delivery', ('origin', 'delivery', 'checkpointSha'), 'b' * 40),
])
def test_named_authority_and_delivery_mismatches_do_not_consume(proof, key, path, value):
    _, store, _, _ = proof
    fact = json.loads(store.db.execute('SELECT value FROM facts WHERE key=?', (key,)).fetchone()[0])
    target = fact
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    store.db.execute('UPDATE facts SET value=? WHERE key=?', (_json(fact), key))
    with pytest.raises(PermissionError, match='witness mismatch'):
        store.admit(['A', 'B'])
    assert store.receipts() == {}
    assert store.db.execute('SELECT count(*) FROM wakes').fetchone() == (0,)


def test_toy_cannot_promote_snapshots_to_shared_authority(proof):
    r, store, _, _ = proof
    calls = list(r.calls)
    with pytest.raises(PermissionError, match='unsupported shared-authority witnesses'):
        store.admit(['A'], authority='current-shared')
    assert store.receipts() == {} and r.calls == calls and not controls(r)


def test_unrelated_observed_result_parks_existing_executor_without_consuming_readiness(proof):
    r, store, _, candidates = proof
    unrelated = delegate(r, 'unrelated-result')
    still_running = delegate(r, 'owns-next-event')
    end(r, unrelated)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    control = activate(r)
    reported = poll_reports(r, unrelated)
    end(r, control, delivered=False)
    continuation.observe_turn_end(7, 'coordinator', control['execution']['attemptId'], reported)
    births, calls = deepcopy(r.created), list(r.calls)
    ids = store.identities()
    assert store.admit(['A', 'B'])
    consumed = store.receipts()
    for _ in range(3):
        assert r.wake() == []
        assert store.admit(['B']) == {ids['B']: consumed[ids['B']]}
    assert r.attempts[still_running['execution']['attemptId']]['status'] == 'running'
    assert len(controls(r)) == 1 and r.calls == calls and r.created == births
    assert store.receipts() == consumed
    assert all(r.c.repo.read(stage['id'])['execution'] is None for stage in candidates.values())
    end(r, still_running)
    assert r.wake() == ['accepted'], 'a genuinely new independent result remains owed'
    assert len(controls(r)) == 2 and r.calls == calls + ['coordinator']
    assert r.created == births and store.receipts() == consumed
