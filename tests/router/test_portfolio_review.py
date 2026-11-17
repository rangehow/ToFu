"""The scheduled portfolio review and its start guard (docs/ROUTER.md R10).

Admission spec: in rank order (expected value per unit of cost) the review
starts every candidate that passes the authority gate, is neither running nor a
duplicate of open work, and whose resources are measured available: there is no
count of starts and no per-project initiative serialization. A candidate that needs owner approval never starts automatically; a
rate-limited provider defers with the provider's own retry time; a start the
Project Router does not accept (its executor could not launch) leaves the
candidate pending with the recorded reason. Starts go through the same
owner-checked forwarding path as route_to_project, as one request that queues
behind a busy Project Router.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from lib.router import forwarding
from lib.router.portfolio.capacity import CapacityPorts
from lib.router.portfolio.registry import RouterPortfolio
from lib.router.portfolio.review import run_portfolio_review, start_opportunity
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch

pytestmark = pytest.mark.unit

OWNER = 7
OWNER_TEXT = '我们要让主router能发挥主人公意识，有办法去迭代我的多个项目，能想到甚至我想不到的地方，要有价值'
MORNING = datetime(2026, 11, 9, 10, 0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from tests.router.test_standing_instructions import Records
    records = Records()
    monkeypatch.setattr('lib.storage.get_storage_client', records)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    project = tmp_path / 'project'
    project.mkdir()
    turns = {'owner-turn': {'conversationId': 'router', 'turnId': 'owner-turn', 'actor': 'human',
                            'projectionRevision': 1, 'projection': {'content': OWNER_TEXT}},
             'approval-turn': {'conversationId': 'router', 'turnId': 'approval-turn', 'actor': 'human',
                               'projectionRevision': 1, 'projection': {'content': '批准，开始吧'}}}
    state = SimpleNamespace(records=records, project=str(project), calls=[], turns=turns, tmp=tmp_path,
                            clock=[MORNING], route={'admissible': True, 'holds': []}, route_reads=[],
                            queue={}, outcomes=[])

    class Bindings:
        def read(self, path):
            return {'projectPath': path, 'conversationId': 'coordinator:' + path, 'revision': 1,
                    'availability': 'available'}

    from lib.conversations import project_coordination
    monkeypatch.setattr(forwarding, '_router_turn', lambda owner, conversation, turn_id: deepcopy(turns[turn_id]))
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: Bindings())
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, **kwargs: {
        'availability': 'available', 'conversationId': 'router', 'projectPath': str(tmp_path / 'router' / '1'),
        'revision': 1, 'capabilities': {}})

    def dispatch(conversation, message, tools, **kwargs):
        state.calls.append({'conversation': conversation, 'message': deepcopy(message), **kwargs})
        outcome = state.outcomes.pop(0) if state.outcomes else 'started'
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == 'start_failed':
            return ScheduledTurnDispatch('start_failed')
        return ScheduledTurnDispatch(outcome, 'task', input_turn_id=f'input-{len(state.calls)}')
    state.dispatch = dispatch

    def model_route(owner, project):
        state.route_reads.append(project)
        return {'model': 'kimi-k3', **state.route}

    def queue_rows(owner, gpu_queue):
        rows = state.queue[gpu_queue['queue']]
        if isinstance(rows, BaseException):
            raise rows
        return rows
    state.capacity = CapacityPorts(model_route=model_route, queue_rows=queue_rows,
                                   now_ms=lambda: int(state.clock[0].timestamp() * 1000))
    return state


def registry(world):
    return RouterPortfolio(OWNER, client=world.records, clock=lambda: world.clock[0])


def instruction(world):
    from lib.router.instructions import RouterInstructions
    return RouterInstructions(OWNER).create(
        router_conversation_id='router', source_turn=world.turns['owner-turn'], label='Own the portfolio',
        project_path=None, thread=None, idempotency_key='portfolio')


def candidate(world, *, project=None, **overrides):
    from tests.router.test_portfolio import arguments
    if project is not None:
        (world.tmp / project).mkdir(exist_ok=True)
    base = arguments(idempotency_key=overrides.pop('idempotency_key', 'one'),
                     instruction_id=instruction(world)['instructionId'],
                     project_path=str(world.tmp / project) if project else world.project)
    base.update(overrides)
    return registry(world).propose(base)


def review(world, task_id='system-7-router-portfolio-review'):
    ok, result = run_portfolio_review({'user_id': OWNER, 'id': task_id}, client=world.records,
                                      clock=lambda: world.clock[0], dispatch=world.dispatch,
                                      capacity=world.capacity)
    assert ok, result
    return result if result.startswith('Quiet hours') else json.loads(result)


def forwards(world):
    return [call for call in world.calls if 'forwardedFrom' in call['message'].get('origin', {})]


def test_a_candidate_needing_owner_approval_never_starts_automatically(world):
    candidate(world, approval_required=True, approval_reason='A production restart is the owner\'s decision only.')
    summary = review(world)
    assert world.calls == [], 'an approval-requiring candidate forwarded nothing'
    assert summary['started'] == []
    assert summary['waiting'][0]['blockers'] == ['owner_approval']
    # Not even capacity is measured for it: the authority gate comes first.
    assert world.route_reads == []


def test_a_candidate_without_approval_starts_as_one_queued_request_after_its_promise(world):
    opportunity = candidate(world)
    summary = review(world)
    # One Coordinator request: the owner text is the authority, the opportunity
    # rides inside it as labelled nonhuman evidence (R2 "Evidence rides the same
    # request"), and it queues behind a busy Project Router instead of steering.
    [human] = world.calls
    assert human['message']['originalContent'] == OWNER_TEXT
    assert human['message']['content'].startswith(OWNER_TEXT + '\n\n[Global Router context: nonhuman')
    assert human['message']['origin']['forwardedFrom']['instructionId'] == opportunity['instructionId']
    assert human['inject_mode'] == 'queue'
    body = human['message']['content']
    assert opportunity['opportunityId'] in body and 'killCondition' in body and 'expectedOutcome' in body
    stored = registry(world).read(opportunity['opportunityId'])
    assert stored['state'] == 'started'
    initiative = stored['initiative']
    assert initiative['disposition'] == 'started'
    assert initiative['promise']['amount'] == 30.0 and initiative['promise']['unit'] == 'cny_per_day'
    assert initiative['admission']['rank'] == 1 and initiative['admission']['ranked'] == 1
    assert initiative['admission']['authority'] == {'kind': 'instruction', 'id': opportunity['instructionId']}
    assert initiative['admission']['resources']['model'] == 'kimi-k3'
    assert initiative['request'] == {'conversationId': 'coordinator:' + world.project, 'bindingRevision': 1,
                                     'inputTurnId': 'input-1'}
    assert summary['started'][0]['admission'] == initiative['admission']
    # Re-running the same review never restarts it (closed or started work is never restarted).
    review(world)
    assert len(world.calls) == 1


def test_quiet_hours_start_nothing(world):
    candidate(world)
    world.clock[0] = datetime(2026, 11, 9, 2, 0)
    assert 'Quiet hours' in review(world) and world.calls == []


def test_a_proposal_without_authority_waits_instead_of_starting(world):
    candidate(world, instruction_id=None)
    summary = review(world)
    assert world.calls == []
    assert summary['waiting'][0]['blockers'] == ['no_authority']


def test_an_owner_approval_in_his_own_message_starts_the_candidate(world):
    opportunity = candidate(world, approval_required=True, approval_reason='Signs a new APK identity.')
    registry(world).approve(opportunity['opportunityId'], source_turn=world.turns['approval-turn'])
    result = start_opportunity(OWNER, opportunity['opportunityId'], started_by='router', review_id=None,
                               client=world.records, clock=lambda: world.clock[0], dispatch=world.dispatch,
                               capacity=world.capacity)
    [human] = world.calls
    assert human['message']['originalContent'] == '批准，开始吧'
    assert human['message']['origin']['forwardedFrom']['turnId'] == 'approval-turn'
    assert result['opportunity']['state'] == 'started'
    assert result['opportunity']['initiative']['admission']['authority'] == {'kind': 'approval',
                                                                             'id': 'approval-turn'}


def test_one_owner_message_approving_two_opportunities_forwards_each_of_them(world):
    first = candidate(world, idempotency_key='first', approval_required=True, approval_reason='Owner trade-off.')
    second = candidate(world, idempotency_key='second', problem='A different problem in the same project.',
                       approval_required=True, approval_reason='Owner trade-off.')
    for value in (first, second):
        registry(world).approve(value['opportunityId'], source_turn=world.turns['approval-turn'])
    options = dict(started_by='router', review_id=None, client=world.records, clock=lambda: world.clock[0],
                   dispatch=world.dispatch, capacity=world.capacity)
    start_opportunity(OWNER, first['opportunityId'], **options)
    registry(world).settle(first['opportunityId'], {'outcome': {'amount': 5.0, 'evidence': ['runs/1.json']}})
    start_opportunity(OWNER, second['opportunityId'], **options)
    first_forward, second_forward = forwards(world)
    # Both cite the same owner Turn, yet each is its own forward: a shared command
    # id would let the Project Router drop the second as a replay of the first.
    assert first_forward['command_id'] != second_forward['command_id']
    assert second['opportunityId'] in second_forward['message']['content']


def test_a_lost_acknowledgement_is_retried_with_the_same_command_id(world):
    opportunity = candidate(world)
    world.outcomes = [ConnectionError('acknowledgement lost')]
    summary = review(world)
    assert summary['waiting'][0]['blockers'] == ['start_unconfirmed:ConnectionError']
    stored = registry(world).read(opportunity['opportunityId'])
    # Whether it was delivered is unknown, so it stays an unconfirmed start.
    assert stored['state'] == 'started' and stored['initiative']['disposition'] is None
    summary = review(world)
    assert summary['retried'] == [opportunity['opportunityId']]
    command_ids = [call['command_id'] for call in forwards(world)]
    assert len(command_ids) == 2 and len(set(command_ids)) == 1, 'the retry reuses the original command id'
    assert registry(world).read(opportunity['opportunityId'])['initiative']['disposition'] == 'started'


def test_a_review_starts_every_admitted_candidate_in_value_order(world):
    """More than three eligible candidates: all of them start in one review."""
    names = ('alpha', 'beta', 'gamma', 'delta', 'epsilon')
    for index, name in enumerate(names):
        candidate(world, idempotency_key=name, project=name, problem=f'Problem {name}.',
                  validation={'step': 'Measure it.', 'gpu_hours': 0.0, 'model_cny': float(index + 1),
                              'wall_hours': 1.0})
    from lib.router.portfolio.ranking import rank
    order = [entry['opportunity']['opportunityId'] for entry in rank(registry(world).open())]
    summary = review(world)
    assert [row['opportunityId'] for row in summary['started']] == order
    assert len(forwards(world)) == len(names) and summary['waiting'] == [] and summary['deferred'] == []
    assert [row['admission']['rank'] for row in summary['started']] == [1, 2, 3, 4, 5]
    assert all(row['admission']['ranked'] == 5 for row in summary['started'])


def test_independent_same_project_initiatives_start_but_duplicates_wait(world):
    cheap = candidate(world, idempotency_key='cheap', problem='Router turns pay twice for one context.')
    costly = candidate(world, idempotency_key='costly', problem='A slower alternative in the same project.',
                       validation={'step': 'A whole evaluation grid.', 'gpu_hours': 0.0, 'model_cny': 500.0,
                                   'wall_hours': 10.0})
    # Same work as one that waits for the owner's approval: never a bypass.
    gated = candidate(world, idempotency_key='gated', project='other', problem='Sign the release.',
                      approval_required=True, approval_reason='A new external identity.',
                      validation={'step': 'Sign it.', 'gpu_hours': 0.0, 'model_cny': 0.0, 'wall_hours': 0.0})
    twin = candidate(world, idempotency_key='twin', project='other', problem='  sign the RELEASE. ')
    elsewhere = candidate(world, idempotency_key='elsewhere', project='elsewhere', problem='Unrelated work.')
    summary = review(world)
    started = {row['opportunityId'] for row in summary['started']}
    assert started == {cheap['opportunityId'], costly['opportunityId'], elsewhere['opportunityId']}
    waiting = {row['opportunityId']: row['blockers'] for row in summary['waiting']}
    assert waiting[gated['opportunityId']] == ['owner_approval']
    assert waiting[twin['opportunityId']] == [f'duplicate:{gated["opportunityId"]}']
    # Same-project starts remain independent queued requests and usage accounts.
    requests = [call for call in forwards(world) if call['message']['origin']['forwardedFrom']]
    assert all(call['inject_mode'] == 'queue' for call in requests)
    input_turns = [registry(world).read(identity)['initiative']['request']['inputTurnId']
                   for identity in (cheap['opportunityId'], costly['opportunityId'])]
    assert len(set(input_turns)) == 2
    # Settling one does not restart or duplicate the other.
    registry(world).settle(cheap['opportunityId'], {'outcome': {'amount': 12.0, 'evidence': ['runs/1.json']}})
    assert review(world)['started'] == []


def test_a_dependency_blocks_until_it_is_delivered(world):
    dependency = candidate(world, idempotency_key='dependency', project='first')
    dependent = candidate(world, idempotency_key='dependent', project='second', problem='Builds on it.',
                          dependencies=[dependency['opportunityId']])
    summary = review(world)
    assert [row['opportunityId'] for row in summary['started']] == [dependency['opportunityId']]
    assert summary['waiting'][0]['blockers'] == [f'dependency:{dependency["opportunityId"]}']
    # A delivered dependency is closed, so it is read by id, not from the open set.
    registry(world).settle(dependency['opportunityId'], {'outcome': {'amount': 30.0, 'evidence': ['a.json']}})
    assert [row['opportunityId'] for row in review(world)['started']] == [dependent['opportunityId']]


def test_a_rate_limited_provider_defers_with_the_providers_own_retry_time(world):
    opportunity = candidate(world)
    reset = MORNING + timedelta(hours=3)
    world.route = {'admissible': False, 'holds': [
        {'key': 'k1', 'model': 'kimi-k3', 'reason': 'quota', 'until': reset.timestamp(), 'providerNamed': True}]}
    summary = review(world)
    assert world.calls == []
    retry_at = int(reset.timestamp() * 1000)
    assert summary['deferred'] == [{'opportunityId': opportunity['opportunityId'], 'title': opportunity['title'],
                                    'reason': 'provider_rate_limited', 'retryAt': retry_at}]
    stored = registry(world).read(opportunity['opportunityId'])
    assert stored['state'] == 'proposed' and stored['initiative'] is None
    assert stored['deferral']['reason'] == 'provider_rate_limited' and stored['deferral']['retryAt'] == retry_at
    assert stored['deferral']['observed']['holds'][0]['providerNamed'] is True
    # Before the provider's own time nothing is asked again.
    reads = len(world.route_reads)
    world.clock[0] = MORNING + timedelta(hours=1)
    assert review(world)['deferred'][0]['retryAt'] == retry_at
    assert len(world.route_reads) == reads and world.calls == []
    # After it, capacity is measured again and the candidate starts.
    world.clock[0] = MORNING + timedelta(hours=4)
    world.route = {'admissible': True, 'holds': []}
    assert [row['opportunityId'] for row in review(world)['started']] == [opportunity['opportunityId']]
    assert registry(world).read(opportunity['opportunityId'])['deferral'] is None


def test_our_own_backoff_is_never_a_retry_time(world):
    opportunity = candidate(world)
    world.route = {'admissible': False, 'holds': [
        {'key': 'k1', 'model': 'kimi-k3', 'reason': 'rate_limit', 'until': MORNING.timestamp() + 0.5,
         'providerNamed': False}]}
    assert review(world)['deferred'][0]['retryAt'] is None
    world.route = {'admissible': True, 'holds': []}
    assert [row['opportunityId'] for row in review(world)['started']] == [opportunity['opportunityId']]


def test_no_route_for_the_model_is_recorded_not_guessed(world):
    opportunity = candidate(world)
    world.route = {'admissible': False, 'holds': []}
    review(world)
    deferral = registry(world).read(opportunity['opportunityId'])['deferral']
    assert deferral['reason'] == 'model_route_unavailable' and deferral['retryAt'] is None


def test_an_os_level_start_failure_leaves_the_candidate_pending(world):
    opportunity = candidate(world)
    world.outcomes = ['start_failed']
    summary = review(world)
    assert summary['started'] == []
    assert summary['deferred'][0]['reason'] == 'start_failed'
    stored = registry(world).read(opportunity['opportunityId'])
    # Nothing runs for it, so it is not an initiative and promises nothing.
    assert stored['state'] == 'proposed' and stored['initiative'] is None
    assert stored['deferral']['reason'] == 'start_failed'
    assert stored['deferral']['observed']['disposition'] == 'start_failed'
    assert stored['startAttempt'] == 1
    from lib.router.portfolio.view import _ledger
    assert _ledger([stored]) == []
    # The next start is a new forward, not a replay of the one that never ran.
    assert [row['opportunityId'] for row in review(world)['started']] == [opportunity['opportunityId']]
    failed, started = forwards(world)
    assert failed['command_id'] != started['command_id']
    assert registry(world).read(opportunity['opportunityId'])['initiative']['trigger'].endswith(':1')


def test_a_gpu_candidate_starts_only_when_its_queue_shows_the_gpus(world):
    gpu = {'connection': 'hope', 'queue': 'root.gpu.eval', 'gpu_type': 'H800', 'gpu_count': 4}
    opportunity = candidate(world, validation={'step': 'One evaluation shard.', 'gpu_hours': 2.0,
                                               'model_cny': 5.0, 'wall_hours': 1.0, 'gpu_queue': gpu})
    world.queue['root.gpu.eval'] = [{'kind': 'gpu', 'name': 'H800', 'unit': 'count', 'total': 64,
                                     'allocated': 62, 'available': 2, 'pending': 3},
                                    {'kind': 'gpu', 'name': 'A100', 'unit': 'count', 'total': 8,
                                     'allocated': 0, 'available': 8}]
    review(world)
    deferral = registry(world).read(opportunity['opportunityId'])['deferral']
    assert deferral['reason'] == 'gpu_queue_full' and world.calls == []
    assert deferral['observed']['rows'] == [{'name': 'H800', 'total': 64, 'allocated': 62, 'available': 2,
                                             'pending': 3}]
    world.queue['root.gpu.eval'] = PermissionError('Read-only compute catalog unavailable')
    review(world)
    deferral = registry(world).read(opportunity['opportunityId'])['deferral']
    assert deferral['reason'] == 'gpu_queue_unreadable' and 'PermissionError' in deferral['detail']
    world.queue['root.gpu.eval'] = [{'kind': 'gpu', 'name': 'H800', 'unit': 'count', 'total': 64,
                                     'allocated': 56, 'available': 8}]
    [started] = review(world)['started']
    assert started['admission']['resources']['gpuQueue'] == 'root.gpu.eval'


def test_the_digest_lists_each_start_with_its_reason_cost_and_expected_outcome(world, monkeypatch):
    from lib.router.portfolio import view
    monkeypatch.setattr('lib.router.portfolio.review.review_state', lambda owner: {
        'enabled': True, 'schedule': '30 9 * * *', 'taskId': None, 'lastRun': None, 'lastResult': None})
    started = candidate(world, idempotency_key='started', project='alpha')
    deferred = candidate(world, idempotency_key='deferred', project='beta', problem='Another problem.',
                         validation={'step': 'One shard.', 'gpu_hours': 1.0, 'model_cny': 1.0, 'wall_hours': 1.0,
                                     'gpu_queue': {'connection': 'hope', 'queue': 'q', 'gpu_count': 1}})
    world.queue['q'] = [{'kind': 'gpu', 'name': 'H800', 'unit': 'count', 'total': 8, 'allocated': 8,
                         'available': 0}]
    review(world)
    usage = {'tokens': 123456, 'modelRequests': 7, 'toolCalls': 3, 'usageComplete': True, 'activeWorkers': 1}
    seen = []

    def request_usage(owner, opportunity):
        seen.append(opportunity['initiative']['request'])
        return usage
    text, counts = view.portfolio_digest(OWNER, client=world.records, usage=request_usage)
    assert counts['started'] == 1 and counts['deferred'] == 1
    assert started['title'] in text and 'Expected: 30 cny_per_day (p=0.5)' in text
    assert 'Why: rank 1/2 by expected value per unit cost' in text
    assert 'authorized by your standing instruction ' + started['instructionId'] in text
    assert 'Cost so far: 123,456 tokens over 7 model requests' in text
    assert 'estimated ¥10 model, 0 GPU h, 1 h' in text
    assert seen == [{'conversationId': 'coordinator:' + str(world.tmp / 'alpha'), 'bindingRevision': 1,
                     'inputTurnId': 'input-1'}]
    assert '**Authorized but not started (measured):**' in text
    assert deferred['title'] + ': gpu_queue_full' in text
    # Reported once: a later digest (since this one) repeats neither.
    later = (MORNING + timedelta(hours=1)).isoformat()
    assert view.portfolio_digest(OWNER, client=world.records, since=later, usage=request_usage) == (None, {})
