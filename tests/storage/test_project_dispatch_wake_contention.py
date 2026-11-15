"""Recovery passes on each real authority while concurrent writers keep committing.

Production 11-10: a settlement drain read the whole segmented dispatch snapshot
(534 grants) under one head witness while ~20 active Workers committed ledger
writes to the same head; 13 of 23 Worker drains lost all eight back-to-back
attempts (DispatchContention) and dropped their Coordinator wake. Recovery only
discovers lanes and requests there; every decision re-reads its own snapshot.
"""
import itertools
from types import SimpleNamespace

import pytest

from lib.project_dispatch import pending_wakes, recovery
from lib.project_dispatch.repository import DispatchContention, ProjectDispatchRepository
from lib.project_dispatch.segmented import SegmentedDispatch
from tests.project_mod.test_project_dispatch_repository import context as context, model_attempt
from tests.storage.test_project_dispatch_repository_segments import authorize, owned as owned

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def advance(repository):
    """Commit the project head unchanged under its CAS: the head-moving part of any creation."""
    segmented = SegmentedDispatch(repository)
    head = segmented.head()
    segmented.command(head['version'], head['value']['state'], [])


def racing(c, rows):
    """A writer that commits to the project head during every document read of a whole-project scan.

    A scan starts with the assignment order index and lasts until the next head
    read, as in production, where scans take seconds and point reads milliseconds.
    Workers' ledger occurrences are document commits that leave the head alone,
    so the writer also advances the head, as a concurrent creation (a grant, a
    settlement, a scope change) does; only that tears a witnessed walk.
    """
    writer = ProjectDispatchRepository(c.repo.owner, '/project', client=c.factory,
        conversations=c.repo.conversations, turn_get=c.repo.turn_get,
        binding_repository=c.repo.binding_repository, clock=c.repo.clock)
    real, counter, scanning = c.repo.client, itertools.count(), [False]

    class Racing:
        def query(self, operation, payload, **kwargs):
            if operation == 'project.dispatch.list' and payload.get('prefix') == 'assignments:order:':
                scanning[0] = True
            elif operation == 'project.dispatch.get' and payload.get('collection') == 'head':
                scanning[0] = False
            result = real().query(operation, payload, **kwargs)
            if (scanning[0] and operation == 'project.dispatch.get'
                    and payload.get('collection') == 'assignments'):
                ordinal = next(counter)
                # One replaced ledger entry: every read sees a new commit, and the
                # grant's model-attempt capacity is never the limit.
                writer.mutate(rows[ordinal % len(rows)]['id'], lambda row: row.update(
                    modelAttempts=[model_attempt(f'race-{ordinal}')]))
                advance(writer)
            return result

        def command(self, *args, **kwargs):
            return real(write=True).command(*args, **kwargs)
    return lambda **kwargs: Racing()


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_a_settlement_drain_completes_while_writers_commit_during_its_read(owned):
    c = owned
    rows = [authorize(c, str(index)) for index in range(6)]
    c.repo.client = racing(c, rows)
    # The witnessed full snapshot can never finish under this writer.
    with pytest.raises(DispatchContention):
        c.repo.list()
    dispatched = []
    service = SimpleNamespace(dispatch_authorized=lambda binding: (
        dispatched.append(binding['assignmentId']) or {'dispatch': 'accepted'}))
    stats = recovery.recover_project_dispatch(
        c.repo.owner, '/project', recipient_id='recipient', repository=c.repo, service=service,
        was_aborted_after=lambda *args: False, clock=c.repo.clock)
    # Every unstarted grant of the settled lane was retried exactly once.
    assert stats['failed'] == 0 and stats['scanned'] == len(rows)
    assert sorted(dispatched) == sorted(row['id'] for row in rows)


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_discovery_returns_every_document_checked_and_never_writes(owned):
    c = owned
    rows = [authorize(c, str(index)) for index in range(3)]
    c.repo.client = racing(c, rows)
    found, settled = c.repo.discover()
    assert [row['id'] for row in found] == [row['id'] for row in rows] and settled == set()
    # Each document is the committed row of its moment: the racing writer's
    # ledger entries may or may not be included, never anything else.
    for before, after in zip(rows, found):
        assert {key: value for key, value in after.items() if key != 'modelAttempts'} == \
            {key: value for key, value in before.items() if key != 'modelAttempts'}


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_pending_wakes_are_one_durable_entry_per_episode(semantic_authority, monkeypatch):
    factory, owner, _engine = semantic_authority
    reports = []
    monkeypatch.setattr(pending_wakes, '_report', lambda *args: reports.append(args) or {'signalId': 'signal-1'})
    started = []
    monkeypatch.setattr(pending_wakes, '_start', lambda key, due=None: started.append(key))
    monkeypatch.setattr(pending_wakes, '_PENDING', {})
    entry = pending_wakes.source_entry('attempt-1')
    assert pending_wakes.retain(owner, '/project', entry, 'source', RuntimeError('lost'), client=factory,
                                sourceAttemptId='attempt-1', coordinatorId='coordinator')
    # The same episode records nothing new, whatever the next failure is.
    assert pending_wakes.retain(owner, '/project', entry, 'source', OSError('again'), client=factory,
                                sourceAttemptId='attempt-1', coordinatorId='coordinator')
    assert len(reports) == 1
    stored = pending_wakes.PendingWakes(owner, '/project', client=factory).entries()
    assert stored[entry]['signalId'] == 'signal-1' and stored[entry]['error'] == 'RuntimeError: lost'
    # The maintenance tick and startup find it without knowing the project.
    assert pending_wakes.process_all(client=factory) == 1 and started == [(owner, '/project')]
    resolved = []
    monkeypatch.setattr('lib.router.watchdog.signals.FaultSignals',
                        lambda owner, client=None: SimpleNamespace(
                            resolve=lambda ids, text: resolved.append((ids, text))))
    pending_wakes.complete(owner, '/project', entry, client=factory)
    assert pending_wakes.PendingWakes(owner, '/project', client=factory).entries() == {}
    assert resolved and resolved[0][0] == ['signal-1']
    # A failure after completion is a new episode with its own fault.
    assert pending_wakes.retain(owner, '/project', entry, 'source', RuntimeError('later'), client=factory,
                                sourceAttemptId='attempt-1')
    assert len(reports) == 2 and reports[0][-1] != reports[1][-1]


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_a_document_commit_runs_the_pending_wakes_whose_pass_reads_it(owned, monkeypatch):
    """A Worker occurrence leaves the head alone, yet it is the next commit of the wakes that read its row."""
    c = owned
    row = authorize(c)
    project = (c.repo.owner, '/project')
    mine = pending_wakes.recipient_entry(row['request']['recipientId'])
    monkeypatch.setattr(pending_wakes, '_PENDING', {project: frozenset({mine, 'source:other-request'})})
    started = []
    monkeypatch.setattr(pending_wakes, '_start', lambda key, due=None: started.append((key, due)))
    head = SegmentedDispatch(c.repo).head()['version']
    c.repo.mutate(row['id'], lambda value: value.update(modelAttempts=[model_attempt('occurrence')]))
    assert SegmentedDispatch(c.repo).head()['version'] == head
    assert started == [(project, frozenset({mine}))]


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_a_wake_failing_on_unchanged_evidence_runs_no_pass_per_unrelated_commit(owned, monkeypatch):
    """Production 2026-11-12: every project held source entries failing for hours with the same
    WakeEvidenceChanged, and each of the execution host's 2.46 project.dispatch.commit/s (almost all
    Worker occurrences of other requests) re-ran a full recovery pass over them: 11% of its GIL."""
    c = owned
    row = authorize(c)
    project = (c.repo.owner, '/project')
    monkeypatch.setattr(pending_wakes, '_PENDING', {})
    monkeypatch.setattr(pending_wakes, '_DUE', {})
    monkeypatch.setattr(pending_wakes, '_RUNNING', set())
    monkeypatch.setattr(pending_wakes, '_report', lambda *args: {'signalId': 'signal-1'})
    monkeypatch.setattr(pending_wakes, '_spawn', lambda target, key: target(key))
    passes = []

    def failing_pass(owner, project, *, due=None, **kwargs):
        passes.append(due)
        return True  # the stuck entry stays: its evidence did not change
    monkeypatch.setattr(pending_wakes, 'process', failing_pass)
    stuck = pending_wakes.source_entry('stuck-request')
    assert pending_wakes.retain(c.repo.owner, '/project', stuck, 'source', RuntimeError('deterministic'),
                                client=c.factory, sourceAttemptId='stuck-request')
    for ordinal in range(20):
        c.repo.mutate(row['id'], lambda value, ordinal=ordinal: value.update(
            modelAttempts=[model_attempt(f'unrelated-{ordinal}')]))
    assert passes == []
    # A commit that rewrites the head (a creation, here unchanged) concerns every entry.
    advance(c.repo)
    assert passes == [None]
    assert stuck in pending_wakes.PendingWakes(c.repo.owner, '/project', client=c.factory).entries()


def test_written_entries_name_the_passes_that_read_each_document():
    request = {'recipientId': 'worker', 'coordinatorId': 'coordinator',
               'delegation': {'attemptId': 'source-attempt'}, 'dependencies': [{'assignmentId': 'first'}]}
    assert pending_wakes.written_entries([
        {'collection': 'assignments', 'value': {'record': {'id': 'second', 'request': request}}},
        {'collection': 'settlements', 'value': {'record': {'sourceAttemptId': 'settled'}}},
        {'collection': 'requests', 'value': {'record': {'accounts': [{'attemptId': 'budget-source'}]}}},
    ]) == {'source:source-attempt', 'recipient:worker', 'recipient:coordinator', 'dependents:second',
           'dependents:first', 'source:settled', 'source:budget-source'}
    assert pending_wakes.written_entries([{'collection': 'scope_changes', 'value': {'record': {}}}]) is None
