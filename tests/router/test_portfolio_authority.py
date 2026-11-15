"""Portfolio over a real storage authority (docs/ROUTER.md R10).

The registry and the promised -> delivered ledger survive a process restart:
records are written, the authority is stopped and reopened on the same data
(SQLite sidecar: a new StorageRuntime; native TofuDB: restart_cycle), and the
opportunity, its frozen promise and admission, its measured outcome and a
proposal's measured deferral read back unchanged. The review switch is one
owner-controlled scheduler row that is on unless the owner paused it. No model
runs.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]


@pytest.fixture
def world(semantic_authority, monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.binding import global_router
    client, owner, engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    router = global_router(owner, create=True)
    project = tmp_path / 'alpha'
    project.mkdir()
    coordinator = ProjectCoordinationRepository(owner).create(
        str(project), folder_id='folder', expected_revision=0, command_id='alpha')
    return SimpleNamespace(client=client, owner=owner, engine=engine, router=router,
                           project=str(project), coordinator=coordinator, tmp=tmp_path)


def _arguments(world, instruction, **overrides):
    return {
        'idempotency_key': 'survives-restart', 'project_path': world.project,
        'title': 'Prove artifact delivery', 'problem': 'A reviewed artifact lives only in a worker worktree.',
        'kind': 'cross_project', 'source': 'report_contradiction',
        'evidence': ['work/hardening-review/candidate-reviewed-unsigned.apk'],
        'value': {'amount': 2.0, 'unit': 'hours_per_week', 'probability': 0.8, 'basis': 'one re-review avoided'},
        'validation': {'step': 'Re-publish the reviewed checkpoint.', 'gpu_hours': 0.0,
                       'model_cny': 5.0, 'wall_hours': 0.5},
        'approval_required': False, 'approval_reason': 'Existing release path, reversible.',
        'dependencies': [], 'kill_condition': {'metric': 'artifacts_published', 'comparator': '<',
                                              'threshold': 1, 'within_hours': 4.0},
        'instruction_id': instruction['instructionId'], **overrides}


def test_registry_and_ledger_survive_a_storage_restart(world, monkeypatch):
    from lib.router.instructions import RouterInstructions
    from lib.router.portfolio.registry import RouterPortfolio
    owner = world.owner
    turn = {'conversationId': world.router['conversationId'], 'turnId': 'owner-turn', 'actor': 'human',
            'projectionRevision': 1, 'projection': {'content': '我们要让主router能发挥主人公意识'}}
    monkeypatch.setattr('lib.router.forwarding._router_turn', lambda *args: turn)
    instruction = RouterInstructions(owner).create(
        router_conversation_id=world.router['conversationId'], source_turn=turn, label='Own the portfolio',
        project_path=None, thread=None, idempotency_key='portfolio')
    registry = RouterPortfolio(owner, clock=lambda: datetime(2026, 11, 9, 10, 0))
    candidate = registry.propose(_arguments(world, instruction))
    waiting = registry.propose(_arguments(world, instruction, idempotency_key='deferred-across-restart',
                                          problem='A second problem, deferred by its provider.'))
    deferral = {'reason': 'provider_rate_limited',
                'detail': 'Every route of kimi-k3 is held by its provider (quota). The provider named its reset.',
                'observedAt': 1794189600000, 'retryAt': 1794200400000,
                'observed': {'model': 'kimi-k3', 'holds': [{'key': 'k1', 'model': 'kimi-k3', 'reason': 'quota',
                                                             'untilMs': 1794200400000, 'providerNamed': True}]}}
    registry.defer(waiting['opportunityId'], deferral=deferral)
    admission = {'rank': 1, 'ranked': 2, 'score': 0.4, 'valueShare': 0.5, 'dominantResourceShare': 1.0,
                 'authority': {'kind': 'instruction', 'id': instruction['instructionId']},
                 'resources': {'model': 'kimi-k3', 'observedAt': 1794189600000}}
    started = registry.begin(candidate['opportunityId'], started_by='review', review_id='task-1',
                             admission=admission)
    registry.dispatched(candidate['opportunityId'], command_id='portfolio-forward:test', disposition='started',
                        request={'conversationId': world.coordinator['conversationId'], 'bindingRevision': 1,
                                 'inputTurnId': 'turn-1'})
    registry.settle(candidate['opportunityId'], {
        'outcome': {'amount': 2.0, 'metric_value': 1, 'evidence': ['apk/candidate.apk'], 'note': 'published'}})
    before = registry.read(candidate['opportunityId'])
    deferred_before = registry.read(waiting['opportunityId'])
    assert before['state'] == 'delivered' and deferred_before['deferral'] == deferral
    assert started['initiative']['promise']['amount'] == 2.0

    # Reopen the same authority: the durable records must still be there.
    from lib.storage import service as storage_service
    if world.engine is not None:
        world.engine.restart_cycle()
    else:
        storage_service.storage_runtime().stop()
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.resource_control.adapters import bind_storage
        runtime = StorageRuntime(StorageSupervisor(project_root=world.tmp, backend='sqlite'), auto_restart=False)
        storage_service._runtime = runtime
        runtime.start()

        def client(*, write=False):
            bound = runtime.client(write=write)
            bind_storage(bound)
            return bound
        world.client = client
    reopened = RouterPortfolio(owner, client=world.client)
    assert reopened.read(candidate['opportunityId']) == before
    assert before['initiative']['admission'] == admission
    assert before['initiative']['request']['inputTurnId'] == 'turn-1'
    assert before['outcome']['amount'] == 2.0 and before['outcome']['verdict'] == 'delivered'
    # A restart neither forgets the provider's own retry time nor turns the
    # deferred proposal into an initiative.
    assert reopened.read(waiting['opportunityId']) == deferred_before
    from lib.router.portfolio.view import ledger
    [row] = ledger(owner, client=world.client)
    assert row['unit'] == 'hours_per_week' and row['delivered'] == 1 and row['initiatives'] == 1
    assert row['promised'] == pytest.approx(1.6)
    assert row['measured'] == pytest.approx(2.0)


def test_the_review_is_on_by_default_and_a_pause_is_the_owners_lasting_choice(world):
    from lib.router.portfolio.review import configure_review, ensure_review, review_state
    owner = world.owner
    # Never installed and never paused: on (the owner asked for proactive iteration).
    assert review_state(owner) == {'enabled': True, 'schedule': '30 9 * * *', 'taskId': None,
                                   'lastRun': None, 'lastResult': None}
    state = ensure_review(owner)
    assert state['enabled'] is True
    assert state['taskId'] == f'system-{owner}-router-portfolio-review'
    paused = configure_review(owner, {'enabled': False}, principal=_principal(owner))
    assert paused['enabled'] is False, f'pause must persist, got {paused}'
    # The owner's choice survives re-installation (boot never re-enables it).
    assert ensure_review(owner)['enabled'] is False
    resumed = configure_review(owner, {'enabled': True}, principal=_principal(owner))
    assert resumed['enabled'] is True
    from lib.scheduler.manager import get_scheduler
    tasks = [task for task in get_scheduler().list_tasks(user_id=owner, include_disabled=True)
             if task.get('task_type') == 'router_portfolio_review']
    assert len(tasks) == 1


def test_the_first_proposal_installs_the_review_on(world, monkeypatch):
    from lib.router.instructions import RouterInstructions
    from lib.router.portfolio.review import review_state
    from lib.router.portfolio.tools import execute_portfolio_tool
    owner = world.owner
    turn = {'conversationId': world.router['conversationId'], 'turnId': 'owner-turn', 'actor': 'human',
            'projectionRevision': 1, 'projection': {'content': '我们要让主router能发挥主人公意识'}}
    instruction = RouterInstructions(owner).create(
        router_conversation_id=world.router['conversationId'], source_turn=turn, label='Own the portfolio',
        project_path=None, thread=None, idempotency_key='portfolio')
    scope = {'user_id': owner, 'conversation_id': world.router['conversationId'],
             'project_path': world.router['projectPath']}
    proposed = execute_portfolio_tool({}, scope, {'operation': 'propose', **_arguments(world, instruction)})
    assert proposed['state'] == 'proposed'
    state = review_state(owner)
    assert state['enabled'] is True and state['taskId'] == f'system-{owner}-router-portfolio-review'


def _principal(owner):
    from lib.router.portfolio.review import portfolio_principal
    return portfolio_principal(owner)
