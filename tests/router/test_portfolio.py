"""Specs for the opportunity registry, ranking, review, kill conditions and ledger.

docs/ROUTER.md R10 (portfolio). Deterministic unit specs over a recording record
store; the real-storage restart specs are test_portfolio_authority.py.
"""
from copy import deepcopy
from datetime import datetime
import threading

import pytest

pytestmark = pytest.mark.unit


class Store:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.get':
            return deepcopy(self.rows.get((payload['namespace'], payload['key'])))
        if operation == 'record.list':
            prefix, after = payload['prefix'], payload.get('after_key', '')
            limit = payload.get('limit', 1000)
            matching = sorted(key for (ns, key) in self.rows
                              if ns == payload['namespace'] and key.startswith(prefix) and key > after)
            return [{'key': key, **deepcopy(self.rows[(payload['namespace'], key)])}
                    for key in matching[:limit]]
        raise ValueError(f'unknown operation {operation}')

    def command(self, operation, payload, command_id):
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            expected = payload.get('expected_version', 0)
            if (old['version'] if old else 0) != expected:
                from lib.storage.errors import StorageError
                raise StorageError('database_conflict', 'CAS conflict')
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': expected + 1}
            return {'version': expected + 1}


def arguments(**overrides):
    base = {
        'idempotency_key': 'candidate-1', 'project_path': '/project', 'title': 'Cut Router cost',
        'problem': 'Every Router turn pays for the same context twice.',
        'kind': 'cross_project', 'source': 'repeated_failure', 'evidence': ['logs/app.log'],
        'value': {'amount': 30.0, 'unit': 'cny_per_day', 'probability': 0.5, 'basis': 'measured $3.14/turn'},
        'validation': {'step': 'Measure cache read share on 10 turns.', 'gpu_hours': 0.0,
                       'model_cny': 10.0, 'wall_hours': 1.0},
        'approval_required': False, 'approval_reason': 'Reversible measurement inside the standing grant.',
        'dependencies': [], 'kill_condition': {'metric': 'cache_read_share', 'comparator': '>=',
                                              'threshold': 0.5, 'within_hours': 24.0},
        'instruction_id': 'ri-1',
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def project_directory(tmp_path, monkeypatch):
    # Project paths must be existing absolute directories; use this test's own tmp root.
    root = tmp_path / 'project'
    root.mkdir()
    from lib.router import forwarding
    monkeypatch.setattr(forwarding, '_target_project', lambda scope, raw: str(root))
    return str(root)


@pytest.fixture
def store():
    return Store()


# What a start records as its reason (review.start_opportunity builds it).
ADMISSION = {'rank': 1, 'ranked': 1, 'score': 0.5, 'valueShare': 1.0, 'dominantResourceShare': 1.0,
             'authority': {'kind': 'instruction', 'id': 'ri-1'},
             'resources': {'model': 'kimi-k3', 'observedAt': 0}}


def propose(store, **overrides):
    from lib.router.portfolio.registry import RouterPortfolio
    clock = overrides.pop('clock', None)
    return RouterPortfolio(7, client=store, clock=clock).propose(arguments(**overrides))


def test_registry_records_every_field_and_replays_by_idempotency_key(store):
    from lib.router.portfolio.registry import RouterPortfolio
    registry = RouterPortfolio(7, client=store)
    first = registry.propose(arguments())
    again = registry.propose(arguments())
    assert first == again
    assert first['opportunityId'].startswith('po-')
    assert first['state'] == 'proposed'
    assert first['value']['amount'] == 30.0
    assert first['validation']['cost'] == {'gpuHours': 0.0, 'modelCny': 10.0, 'wallHours': 1.0}
    assert first['killCondition']['withinHours'] == 24.0
    assert first['approval'] == {'required': False, 'reason': 'Reversible measurement inside the standing grant.',
                                 'grantedTurnId': None, 'grantedAt': None}
    assert first['evidence'] == ['logs/app.log']
    assert first['outcome'] is None and first['initiative'] is None
    assert first['deferral'] is None and first['startAttempt'] == 0
    assert first['validation']['gpuQueue'] is None
    # The same key with a different problem is refused, never a second record.
    with pytest.raises(ValueError, match='idempotency_key'):
        registry.propose(arguments(problem='Something else'))
    # Another owner cannot read the record.
    with pytest.raises(ValueError, match='Unknown opportunity'):
        RouterPortfolio(8, client=store).read(first['opportunityId'])


def test_ranking_is_expected_value_per_unit_of_resource_not_novelty(store):
    from lib.router.portfolio.ranking import rank
    cheap = propose(store, idempotency_key='cheap', value={'amount': 30.0, 'unit': 'cny_per_day',
                                                          'probability': 0.5, 'basis': 'x'})
    expensive = propose(store, idempotency_key='expensive',
                        value={'amount': 30.0, 'unit': 'cny_per_day', 'probability': 0.5, 'basis': 'x'},
                        validation={'step': 'A whole evaluation grid.', 'gpu_hours': 100.0,
                                    'model_cny': 5000.0, 'wall_hours': 100.0,
                                    'gpu_queue': {'connection': 'hope', 'queue': 'root.gpu.eval',
                                                  'gpu_count': 8}})
    rows = rank([expensive, cheap])
    assert [row['opportunity']['opportunityId'] for row in rows] == [
        cheap['opportunityId'], expensive['opportunityId']]
    assert rows[0]['score'] > rows[1]['score']
    # A candidate whose validation costs nothing measured ranks ahead of all priced ones.
    free = propose(store, idempotency_key='free',
                   validation={'step': 'Read the existing artifact.', 'gpu_hours': 0.0,
                               'model_cny': 0.0, 'wall_hours': 0.0})
    rows = rank([cheap, free, expensive])
    assert rows[0]['opportunity']['opportunityId'] == free['opportunityId']
    assert rows[0]['score'] is None and rows[0]['dominantResourceShare'] == 0.0


def test_kill_condition_is_falsifiable(store):
    from lib.router.portfolio.registry import kill_triggered
    condition = {'metric': 'cache_read_share', 'comparator': '>=', 'threshold': 0.5}
    assert kill_triggered(condition, 0.5) is True
    assert kill_triggered(condition, 0.9) is True
    assert kill_triggered(condition, 0.2) is False
    assert kill_triggered(condition, None) is False
    assert kill_triggered({'comparator': '<', 'threshold': 3}, 2) is True


def test_ledger_freezes_the_promise_at_start_and_compares_with_the_measured_value(store):
    from lib.router.portfolio.registry import RouterPortfolio
    from lib.router.portfolio.view import _ledger
    registry = RouterPortfolio(7, client=store, clock=lambda: datetime(2026, 11, 9, 10, 0))
    candidate = registry.propose(arguments())
    started = registry.begin(candidate['opportunityId'], started_by='review', review_id='task-1',
                             admission=ADMISSION)
    promise = started['initiative']['promise']
    assert promise['amount'] == 30.0 and promise['unit'] == 'cny_per_day'
    assert promise['dueAt'] > started['initiative']['startedAt']
    # The promise is frozen before any dispatch; the start has not been confirmed yet.
    assert started['initiative']['disposition'] is None
    settled = registry.settle(candidate['opportunityId'], {
        'outcome': {'amount': 12.0, 'metric_value': 0.2, 'evidence': ['runs/1.json'], 'note': 'measured'}})
    assert settled['state'] == 'delivered'
    assert settled['outcome']['verdict'] == 'delivered'
    [row] = _ledger([settled])
    assert row['unit'] == 'cny_per_day'
    assert row['initiatives'] == 1 and row['delivered'] == 1 and row['negative'] == 0
    assert row['promised'] == pytest.approx(15.0)
    assert row['measured'] == pytest.approx(12.0)
    # A closed initiative never restarts.
    with pytest.raises(ValueError, match='never restarted'):
        registry.begin(candidate['opportunityId'], started_by='review', review_id='task-2',
                       admission=ADMISSION)


def test_a_measurement_meeting_the_kill_condition_is_an_explicit_negative_result(store):
    from lib.router.portfolio.registry import RouterPortfolio
    registry = RouterPortfolio(7, client=store)
    candidate = registry.propose(arguments())
    registry.begin(candidate['opportunityId'], started_by='review', review_id='task-1',
                             admission=ADMISSION)
    settled = registry.settle(candidate['opportunityId'], {
        'outcome': {'amount': 0.0, 'metric_value': 0.9, 'evidence': ['runs/2.json']}})
    assert settled['state'] == 'negative'
    assert settled['outcome']['reason'] == 'kill_condition'
    assert settled['outcome']['amount'] == 0.0


def test_no_outcome_by_the_promised_due_time_closes_the_initiative(store):
    from lib.router.portfolio.registry import RouterPortfolio
    clock = [datetime(2026, 11, 9, 10, 0)]
    registry = RouterPortfolio(7, client=store, clock=lambda: clock[0])
    candidate = registry.propose(arguments())
    registry.begin(candidate['opportunityId'], started_by='review', review_id='task-1',
                             admission=ADMISSION)
    clock[0] = datetime(2026, 11, 10, 11, 0)
    closed = registry.close_expired()
    assert len(closed) == 1
    assert closed[0]['state'] == 'negative'
    assert closed[0]['outcome']['reason'] == 'no_outcome_by_due'
    # Idempotent: the next review closes nothing.
    assert registry.close_expired() == []


def test_a_proposal_past_its_value_deadline_is_dropped(store):
    from lib.router.portfolio.registry import RouterPortfolio
    clock = [datetime(2026, 11, 9, 10, 0)]
    registry = RouterPortfolio(7, client=store, clock=lambda: clock[0])
    candidate = registry.propose(arguments(deadline='2026-11-09T12:00:00'))
    assert registry.close_expired() == []
    clock[0] = datetime(2026, 11, 9, 13, 0)
    closed = registry.close_expired()
    assert [value['outcome']['reason'] for value in closed] == ['expired']
    assert closed[0]['state'] == 'dropped'


def test_a_gpu_validation_names_the_queue_whose_real_state_admits_it(store):
    with pytest.raises(ValueError, match='gpu_queue'):
        propose(store, idempotency_key='gpu', validation={'step': 'Train one epoch.', 'gpu_hours': 8.0,
                                                          'model_cny': 0.0, 'wall_hours': 2.0})
    recorded = propose(store, idempotency_key='gpu', validation={
        'step': 'Train one epoch.', 'gpu_hours': 8.0, 'model_cny': 0.0, 'wall_hours': 2.0,
        'gpu_queue': {'connection': 'hope', 'queue': 'root.gpu.train', 'gpu_count': 8}})
    assert recorded['validation']['gpuQueue'] == {'connection': 'hope', 'queue': 'root.gpu.train',
                                                  'gpuType': None, 'gpuCount': 8}


def test_an_unaccepted_start_returns_to_proposed_and_the_next_start_is_a_new_attempt(store):
    from lib.router.portfolio.registry import RouterPortfolio
    registry = RouterPortfolio(7, client=store, clock=lambda: datetime(2026, 11, 9, 10, 0))
    candidate = registry.propose(arguments())
    first = registry.begin(candidate['opportunityId'], started_by='review', review_id='r1', admission=ADMISSION)
    assert first['initiative']['trigger'] == f'portfolio:{candidate["opportunityId"]}:0'
    assert first['initiative']['admission'] == ADMISSION
    deferral = {'reason': 'start_failed', 'detail': 'executor did not start', 'observedAt': 1,
                'retryAt': None, 'observed': {'disposition': 'start_failed'}}
    released = registry.release(candidate['opportunityId'], deferral=deferral)
    assert released['state'] == 'proposed' and released['initiative'] is None
    assert released['deferral'] == deferral and released['startAttempt'] == 1
    second = registry.begin(candidate['opportunityId'], started_by='review', review_id='r2', admission=ADMISSION)
    assert second['initiative']['trigger'].endswith(':1') and second['deferral'] is None
    registry.dispatched(candidate['opportunityId'], command_id='router-forward:x', disposition='queued',
                        request=None)
    # An accepted start is an initiative: it is measured, never released.
    with pytest.raises(ValueError, match='unaccepted'):
        registry.release(candidate['opportunityId'], deferral=deferral)


def test_a_deferral_is_recorded_on_a_proposal_and_a_revision_keeps_the_attempt_counter(store):
    from lib.router.portfolio.registry import RouterPortfolio
    registry = RouterPortfolio(7, client=store)
    candidate = registry.propose(arguments())
    deferral = {'reason': 'provider_rate_limited', 'detail': 'held', 'observedAt': 5, 'retryAt': 9,
                'observed': {'model': 'kimi-k3', 'holds': []}}
    assert registry.defer(candidate['opportunityId'], deferral=deferral)['deferral'] == deferral
    registry.begin(candidate['opportunityId'], started_by='router', review_id=None, admission=ADMISSION)
    registry.release(candidate['opportunityId'], deferral=dict(deferral, reason='start_failed', retryAt=None))
    revised = registry.revise({**arguments(problem='A sharper problem statement.'),
                               'opportunity_id': candidate['opportunityId']})
    # New facts clear the old measurement; the attempt counter never goes back.
    assert revised['deferral'] is None and revised['startAttempt'] == 1
