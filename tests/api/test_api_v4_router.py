"""/api/v4/router: owner-scoped Global Router read, ensure, worker model and instructions."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def flask_app(chat_sidecar):
    import server
    server.app.config.update(TESTING=True)
    return server.app


@pytest.fixture
def home_root(tmp_path, monkeypatch):
    root = tmp_path / 'router-homes'
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(root))
    return root


@pytest.fixture
def no_review_chosen(chat_sidecar):
    """The periodic review row is per owner; this module's storage outlives each test's router home."""
    from lib.router.watchdog.review import review_task_id
    from lib.scheduler.manager import get_scheduler
    get_scheduler().delete_task(review_task_id(1), user_id=1)
    yield
    get_scheduler().delete_task(review_task_id(1), user_id=1)


def test_read_is_passive_and_ensure_is_idempotent(flask_client, chat_sidecar, home_root, no_review_chosen):
    unbound = flask_client.get('/api/v4/router')
    assert unbound.status_code == 200, unbound.get_json()
    assert unbound.get_json()['availability'] == 'unbound' and unbound.get_json()['workerModel'] == ''
    assert not home_root.exists()
    refused = flask_client.post('/api/v4/router', json={})
    assert refused.status_code == 400 and not home_root.exists()
    first = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure-1'})
    assert first.status_code == 200, first.get_json()
    router = first.get_json()
    assert router['availability'] == 'available' and router['revision'] == 1
    assert os.path.isdir(os.path.join(router['projectPath'], '.git'))
    # Health checks come with the main coordinator, on by default (owner decision 2026-11-17).
    health = flask_client.get('/api/v4/router/watchdog').get_json()
    assert health['availability'] == 'available' and health['projectPath'].startswith(router['projectPath'] + '/')
    assert health['review']['enabled'] is True and health['review']['schedule'] == '7,37 * * * *'
    assert health['review']['taskId']
    again = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure-2'})
    assert again.get_json()['conversationId'] == router['conversationId']
    assert flask_client.get('/api/v4/router').get_json() == router
    assert flask_client.post('/api/v4/router', json={'unexpected': 1},
                             headers={'Idempotency-Key': 'ensure-3'}).status_code == 400


def test_health_checks_that_cannot_be_ensured_never_keep_the_owner_out_of_the_main_coordinator(
        flask_client, chat_sidecar, home_root, monkeypatch):
    from lib.storage import StorageError

    def unavailable(owner, *, tenant_id=None):
        raise StorageError('database_unavailable', 'scheduler store is restarting')
    monkeypatch.setattr('lib.router.watchdog.review.ensure_health_checks', unavailable)
    ensured = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure-degraded'})
    assert ensured.status_code == 200, ensured.get_json()
    assert ensured.get_json()['availability'] == 'available'
    assert flask_client.get('/api/v4/router/watchdog').get_json()['availability'] == 'unbound'
    # The Watchdog's own ensure reports the failure instead.
    refused = flask_client.post('/api/v4/router/watchdog', json={}, headers={'Idempotency-Key': 'watchdog'})
    assert refused.status_code == 503, refused.get_json()


def test_git_unavailability_is_a_safe_retryable_503_not_an_uncaught_500(
        flask_client, chat_sidecar, home_root, monkeypatch):
    from lib.git_runtime import GitExecutableUnavailable
    monkeypatch.setattr(
        'lib.git_runtime.git_executable',
        lambda: (_ for _ in ()).throw(GitExecutableUnavailable('private loader detail')))
    home_root.joinpath('1', '.git').mkdir(parents=True)
    response = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure-git'})
    assert response.status_code == 503, response.get_json()
    body = response.get_json()
    error = body['error']
    assert error['kind'] == 'router_git_unavailable'
    assert error['code'] == 'router_git_unavailable'
    assert error['message'] == 'Git or its filesystem is temporarily unavailable. Retry opening the main coordinator.'
    assert body['problem']['status'] == 503
    assert body['problem']['requestId']
    assert response.headers['Content-Type'].startswith('application/problem+json')
    assert 'private loader detail' not in response.get_data(as_text=True)


def test_legacy_git_unavailability_retains_retryable_envelope():
    import asyncio
    from quart import Quart
    from lib.router.home import RouterGitUnavailable
    from routes.api_v1.router import _typed_error

    async def scenario():
        app = Quart(__name__)
        async with app.test_request_context('/api/v1/router', method='POST'):
            result, status = _typed_error(RouterGitUnavailable('private loader detail'))
            assert status == 503
            body = await result.get_json()
            assert body['error']['kind'] == 'storage_unavailable'
            assert body['error']['retryable'] is True
            assert body['error_code'] == 'router_git_unavailable'
            assert 'private loader detail' not in await result.get_data(as_text=True)

    asyncio.run(scenario())


def test_worker_model_is_route_admitted_and_clearable(flask_client, chat_sidecar, home_root, monkeypatch):
    from lib.project_dispatch import delegation

    def admit(owner, model, *, tenant_id=None):
        if model != 'strong-model':
            raise ValueError('workerModel is not routable for this owner')
        return model
    monkeypatch.setattr(delegation, 'admit_worker_model', admit)
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    endpoint, headers = '/api/v4/router/worker-model', {'Idempotency-Key': 'model'}
    rejected = flask_client.post(endpoint, json={'path': router['projectPath'], 'workerModel': 'made-up'},
                                 headers=headers)
    assert rejected.status_code == 400
    chosen = flask_client.post(endpoint, json={'path': router['projectPath'], 'workerModel': 'strong-model'},
                               headers=headers)
    assert chosen.status_code == 200, chosen.get_json()
    assert chosen.get_json()['workerModel'] == 'strong-model'
    assert flask_client.get('/api/v4/router').get_json()['workerModel'] == 'strong-model'
    cleared = flask_client.post(endpoint, json={'path': router['projectPath'], 'workerModel': None},
                                headers=headers)
    assert cleared.get_json()['workerModel'] == ''
    unbound = flask_client.post(endpoint, json={'path': '/no/such/coordinator', 'workerModel': None},
                                headers=headers)
    assert unbound.status_code == 403


def test_worker_model_reads_any_bound_router_without_changing_it(flask_client, chat_sidecar, home_root,
                                                                  monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.project_dispatch import delegation
    monkeypatch.setattr(delegation, 'admit_worker_model', lambda owner, model, *, tenant_id=None: model)
    project = tmp_path / 'project'
    project.mkdir()
    binding = ProjectCoordinationRepository(1).provision(
        str(project), title='Project coordinator', settings={}, command_id='route-to-project')
    endpoint = '/api/v4/router/worker-model'
    read = flask_client.get(endpoint, query_string={'path': str(project)})
    assert read.status_code == 200, read.get_json()
    assert {key: value for key, value in read.get_json().items() if key != 'ok'} == {**binding, 'workerModel': ''}
    flask_client.post(endpoint, json={'path': str(project), 'workerModel': 'fast-model'},
                      headers={'Idempotency-Key': 'project-model'})
    assert flask_client.get(endpoint, query_string={'path': str(project)}).get_json()['workerModel'] == 'fast-model'
    unbound = flask_client.get(endpoint, query_string={'path': str(tmp_path / 'unbound')}).get_json()
    assert unbound['availability'] == 'unbound' and unbound['workerModel'] == ''
    assert flask_client.get(endpoint).status_code == 400
    assert flask_client.get(endpoint, query_string={'path': str(project), 'extra': '1'}).status_code == 400


def test_owner_lists_and_cancels_standing_instructions(flask_client, chat_sidecar, home_root):
    from lib.router.instructions import RouterInstructions
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    endpoint = '/api/v4/router/instructions'
    empty = flask_client.get(endpoint).get_json()
    assert empty['instructions'] == [] and empty['nextCursor'] is None
    instruction = RouterInstructions(1).create(
        router_conversation_id=router['conversationId'],
        source_turn={'turnId': 'owner-turn', 'projectionRevision': 1,
                     'projection': {'content': 'Reproduce reported bugs in the parser project.'}},
        label='Bug reports', project_path=None, thread='dx:gid:7', idempotency_key='bugs')
    listed = flask_client.get(endpoint).get_json()['instructions']
    assert [item['instructionId'] for item in listed] == [instruction['instructionId']]
    detail = flask_client.get(endpoint, query_string={'instructionId': instruction['instructionId']})
    assert detail.get_json()['applications'] == []
    assert flask_client.post(endpoint + '/cancel', json={'instructionId': instruction['instructionId']}
                             ).status_code == 400
    cancelled = flask_client.post(endpoint + '/cancel', json={'instructionId': instruction['instructionId']},
                                  headers={'Idempotency-Key': 'cancel'})
    assert cancelled.status_code == 200 and cancelled.get_json()['state'] == 'cancelled'
    assert flask_client.get(endpoint).get_json()['instructions'] == []
    everything = flask_client.get(endpoint, query_string={'includeCancelled': 'true'}).get_json()
    assert [item['state'] for item in everything['instructions']] == ['cancelled']
    assert flask_client.get(endpoint, query_string={'owner': '2'}).status_code == 400
    unknown = flask_client.post(endpoint + '/cancel', json={'instructionId': 'ri-unknown'},
                                headers={'Idempotency-Key': 'cancel-unknown'})
    assert unknown.status_code == 400


def test_owner_pauses_and_resumes_standing_instructions(flask_client, chat_sidecar, home_root):
    from lib.router.instructions import RouterInstructions
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    endpoint = '/api/v4/router/instructions'
    instruction = RouterInstructions(1).create(
        router_conversation_id=router['conversationId'],
        source_turn={'turnId': 'owner-turn', 'projectionRevision': 1,
                     'projection': {'content': 'Reproduce reported bugs in the parser project.'}},
        label='Bug reports', project_path=None, thread=None, idempotency_key='bugs')
    body = {'instructionId': instruction['instructionId']}
    assert flask_client.post(endpoint + '/pause', json=body).status_code == 400
    assert flask_client.post(endpoint + '/pause', json={**body, 'state': 'paused'},
                             headers={'Idempotency-Key': 'pause-extra'}).status_code == 400
    paused = flask_client.post(endpoint + '/pause', json=body, headers={'Idempotency-Key': 'pause'})
    assert paused.status_code == 200, paused.get_json()
    assert paused.get_json()['state'] == 'paused' and isinstance(paused.get_json()['pausedAt'], int)
    [listed] = flask_client.get(endpoint).get_json()['instructions']
    assert listed['state'] == 'paused'
    again = flask_client.post(endpoint + '/pause', json=body, headers={'Idempotency-Key': 'pause-again'})
    assert again.get_json() == paused.get_json()
    resumed = flask_client.post(endpoint + '/resume', json=body, headers={'Idempotency-Key': 'resume'})
    assert resumed.status_code == 200 and resumed.get_json()['state'] == 'active'
    assert resumed.get_json()['pausedAt'] is None
    flask_client.post(endpoint + '/cancel', json=body, headers={'Idempotency-Key': 'cancel'})
    refused = flask_client.post(endpoint + '/resume', json=body, headers={'Idempotency-Key': 'resume-cancelled'})
    assert refused.status_code == 400 and 'cannot be resumed' in str(refused.get_json())
    unknown = flask_client.post(endpoint + '/pause', json={'instructionId': 'ri-unknown'},
                                headers={'Idempotency-Key': 'pause-unknown'})
    assert unknown.status_code == 400


def test_watchdog_read_is_passive_ensure_is_idempotent_and_review_is_owner_controlled(
        flask_client, chat_sidecar, home_root, no_review_chosen):
    endpoint = '/api/v4/router/watchdog'
    unbound = flask_client.get(endpoint)
    assert unbound.status_code == 200, unbound.get_json()
    assert unbound.get_json()['availability'] == 'unbound'
    assert unbound.get_json()['review'] == {'enabled': False, 'schedule': '7,37 * * * *', 'taskId': None}
    assert not home_root.exists()
    assert flask_client.post(endpoint, json={}).status_code == 400
    early = flask_client.post(endpoint + '/review', json={'enabled': True}, headers={'Idempotency-Key': 'early'})
    assert early.status_code == 403
    first = flask_client.post(endpoint, json={}, headers={'Idempotency-Key': 'watchdog-1'})
    assert first.status_code == 200, first.get_json()
    watchdog = first.get_json()
    assert watchdog['availability'] == 'available' and watchdog['projectPath'].endswith('/.tofu-watchdog')
    assert os.path.isdir(watchdog['projectPath'])
    # Ensuring health checks turns the periodic review on; nobody has to configure it.
    state = watchdog['review']
    assert state['enabled'] is True and state['schedule'] == '7,37 * * * *' and state['taskId']
    again = flask_client.post(endpoint, json={}, headers={'Idempotency-Key': 'watchdog-2'}).get_json()
    assert again == watchdog
    review = endpoint + '/review'
    assert flask_client.post(review, json={'enabled': True}).status_code == 400
    custom = flask_client.post(review, json={'enabled': True, 'schedule': '*/20 * * * *'},
                               headers={'Idempotency-Key': 'review-2'}).get_json()['review']
    assert custom == {**state, 'schedule': '*/20 * * * *'}
    assert flask_client.post(review, json={'enabled': True, 'schedule': 'whenever'},
                             headers={'Idempotency-Key': 'review-3'}).status_code == 400
    off = flask_client.post(review, json={'enabled': False}, headers={'Idempotency-Key': 'review-4'})
    assert off.get_json()['review'] == {**custom, 'enabled': False}
    assert flask_client.get(endpoint).get_json() == off.get_json()
    # The owner's choice survives every later ensure, the main coordinator's included.
    assert flask_client.post(endpoint, json={}, headers={'Idempotency-Key': 'watchdog-3'}).get_json() == off.get_json()
    flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'router'})
    assert flask_client.get(endpoint).get_json() == off.get_json()


def test_portfolio_read_reports_actual_counts_and_review_pause_is_owner_controlled(
        flask_client, chat_sidecar, home_root, tmp_path):
    from lib.router.portfolio.registry import RouterPortfolio
    from lib.router.portfolio.review import ensure_review
    from tests.router.test_portfolio import ADMISSION, arguments
    endpoint = '/api/v4/router/portfolio'
    registry = RouterPortfolio(1)
    project = tmp_path / 'portfolio-project'
    project.mkdir()
    candidate = registry.propose(arguments(project_path=str(project), approval_required=True,
                                           approval_reason='Only the owner can make this trade-off.'))
    registry.begin(candidate['opportunityId'], started_by='router', review_id=None, admission=ADMISSION)
    empty = flask_client.get(endpoint).get_json()
    assert empty['counts'] == {'proposed': 0, 'started': 1, 'delivered': 0, 'negative': 0,
                               'dropped': 0, 'awaitingOwner': 0}
    dropped = registry.propose(arguments(project_path=str(project), idempotency_key='dropped'))
    registry.close(dropped['opportunityId'], verdict='dropped', reason='dropped')
    before = flask_client.get(endpoint)
    assert before.status_code == 200, before.get_json()
    assert before.get_json()['counts'] == {'proposed': 0, 'started': 1, 'delivered': 0, 'negative': 0,
                                           'dropped': 1, 'awaitingOwner': 0}
    # On by default: the owner asked for proactive iteration; only his pause turns it off.
    assert before.get_json()['review'] == {'enabled': True, 'schedule': '30 9 * * *', 'taskId': None,
                                           'lastRun': None, 'lastResult': None}
    assert flask_client.post(endpoint + '/review', json={'enabled': False}).status_code == 400
    paused = flask_client.post(endpoint + '/review', json={'enabled': False},
                               headers={'Idempotency-Key': 'pause-portfolio'})
    assert paused.status_code == 200, paused.get_json()
    state = paused.get_json()['review']
    assert state['enabled'] is False and state['taskId']
    assert ensure_review(1)['enabled'] is False
    resumed = flask_client.post(endpoint + '/review', json={'enabled': True},
                                headers={'Idempotency-Key': 'resume-portfolio'})
    assert resumed.get_json()['review'] == {**state, 'enabled': True}
    assert flask_client.get(endpoint).get_json() == resumed.get_json()


def test_projects_read_pages_the_owners_bound_projects_in_wire_form(
        flask_client, chat_sidecar, home_root, tmp_path, monkeypatch):
    """GET /api/v4/router/projects is one get_router_projects page in camelCase: owner-scoped,
    without the router home and the Watchdog, with exactly the facts the tool produced."""
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.settings_store import set_conversation_settings
    from lib.router.contract import validate
    from lib.router.projects import router_projects, router_projects_wire
    endpoint = '/api/v4/router/projects'

    def read(**query):
        response = flask_client.get(endpoint, query_string=query)
        assert response.status_code == 200, response.get_json()
        body = json.loads(response.get_data(as_text=True))
        assert set(body) == {'data', 'meta'}
        return validate(body['data'], 'routerProjectsPage')

    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    watchdog = flask_client.post('/api/v4/router/watchdog', json={},
                                 headers={'Idempotency-Key': 'watchdog'}).get_json()
    bindings = {}
    for owner, name in ((1, 'alpha'), (1, 'beta'), (1, 'archived'), (2, 'foreign')):
        path = tmp_path / name
        path.mkdir()
        bindings[name] = ProjectCoordinationRepository(owner).provision(
            str(path), title='Project coordinator', settings={}, command_id='route-to-project')
    alpha, beta, archived, foreign = (bindings[name] for name in ('alpha', 'beta', 'archived', 'foreign'))
    set_conversation_settings(archived['conversationId'], {'archived': True}, user_id=1)
    CoordinatorReports(1, alpha['projectPath'], relay=lambda *args, **kwargs: None).publish(
        {'owner': 1, 'projectPath': alpha['projectPath'], 'coordinatorId': alpha['conversationId'],
         'bindingRevision': alpha['revision']},
        {'title': 'Shipped', 'message': 'Delivered and verified.', 'kind': 'delivery',
         'idempotency_key': 'shipped'})
    latest = CoordinatorReports(1, alpha['projectPath']).read()['latest']

    page = read()
    assert page['nextCursor'] is None
    assert page == router_projects_wire(router_projects(1, {}))
    listed = {entry['projectPath']: entry for entry in page['projects']}
    assert router['projectPath'] not in listed and watchdog['projectPath'] not in listed
    assert foreign['projectPath'] not in listed
    assert foreign['projectPath'] in [entry['project_path'] for entry in router_projects(2, {})['projects']]
    assert listed[alpha['projectPath']] == {
        'projectPath': alpha['projectPath'], 'coordinatorConversationId': alpha['conversationId'],
        'bindingRevision': alpha['revision'], 'availability': 'available', 'activeWorkers': 0,
        'activeRequests': 0, 'workersCoverage': 'complete',
        'lastReport': {'turnId': latest['turnId'], 'kind': 'delivery', 'title': 'Shipped',
                       'createdAt': latest['createdAt'], 'unread': True}}
    assert listed[beta['projectPath']] == {
        'projectPath': beta['projectPath'], 'coordinatorConversationId': beta['conversationId'],
        'bindingRevision': beta['revision'], 'availability': 'available', 'activeWorkers': 0,
        'activeRequests': 0, 'workersCoverage': 'complete', 'lastReport': None}
    # An unavailable lead has no activity facts at all, not zeros.
    assert listed[archived['projectPath']] == {
        'projectPath': archived['projectPath'], 'coordinatorConversationId': archived['conversationId'],
        'bindingRevision': archived['revision'], 'availability': 'unavailable'}

    monkeypatch.setattr('lib.project_dispatch.history.PAGE_ROWS', 1)
    seen, cursor = [], None
    for _ in range(len(listed) + 3):  # one binding row per page, the router home and Watchdog included
        paged = read(**({'cursor': cursor} if cursor else {}))
        assert len(paged['projects']) <= 1
        seen += [entry['projectPath'] for entry in paged['projects']]
        cursor = paged['nextCursor']
        if cursor is None:
            break
    assert cursor is None and seen == list(listed)
    for query in ({'cursor': ''}, {'owner': '2'}):
        assert flask_client.get(endpoint, query_string=query).status_code == 400
    assert flask_client.get(endpoint + '?cursor=a&cursor=b').status_code == 400


def test_projects_wire_renames_every_fact_without_inventing_any():
    from lib.router.contract import CONTRACT, validate
    from lib.router.projects import router_projects_wire
    page = {'projects': [
        {'project_path': '/work/alpha', 'coordinator_conversation_id': 'coordinator-a', 'binding_revision': 3,
         'availability': 'available', 'active_workers': 2, 'active_requests': 1,
         'workers_coverage': 'newest_page',
         'last_report': {'turn_id': 'turn-9', 'kind': 'blocked', 'title': 'Waiting on keys',
                         'created_at': 1700, 'unread': False},
         'board_summary': 'Ship the parser fix', 'board_revision': 4, 'open_commitments': 2,
         'overdue_commitments': 1, 'overdue_titles': ['Regression tests'], 'open_questions': 2,
         'oldest_question_age_hours': 5, 'blocking_questions': 1},
        {'project_path': '/work/beta', 'coordinator_conversation_id': 'coordinator-b', 'binding_revision': 1,
         'availability': 'available', 'active_workers': 0, 'active_requests': 0,
         'workers_coverage': 'complete', 'activity_error': 'Invalid coordination binding'},
        {'project_path': '/work/gamma', 'coordinator_conversation_id': 'coordinator-c', 'binding_revision': 2,
         'availability': 'unavailable'}],
        'next_cursor': 'a1b2'}
    original = json.dumps(page, sort_keys=True)
    wire = validate(router_projects_wire(page), 'routerProjectsPage')
    assert json.dumps(page, sort_keys=True) == original
    assert wire == {'projects': [
        {'projectPath': '/work/alpha', 'coordinatorConversationId': 'coordinator-a', 'bindingRevision': 3,
         'availability': 'available', 'activeWorkers': 2, 'activeRequests': 1,
         'workersCoverage': 'newest_page',
         'lastReport': {'turnId': 'turn-9', 'kind': 'blocked', 'title': 'Waiting on keys',
                        'createdAt': 1700, 'unread': False},
         'boardSummary': 'Ship the parser fix', 'boardRevision': 4, 'openCommitments': 2,
         'overdueCommitments': 1, 'overdueTitles': ['Regression tests'], 'openQuestions': 2,
         'oldestQuestionAgeHours': 5, 'blockingQuestions': 1},
        {'projectPath': '/work/beta', 'coordinatorConversationId': 'coordinator-b', 'bindingRevision': 1,
         'availability': 'available', 'activeWorkers': 0, 'activeRequests': 0,
         'workersCoverage': 'complete', 'activityError': 'Invalid coordination binding'},
        {'projectPath': '/work/gamma', 'coordinatorConversationId': 'coordinator-c', 'bindingRevision': 2,
         'availability': 'unavailable'}],
        'nextCursor': 'a1b2'}
    summary = CONTRACT['$defs']['routerProjectSummary']['properties']
    assert set().union(*wire['projects']) == set(summary)
    with pytest.raises(KeyError):
        router_projects_wire({'projects': [{**page['projects'][2], 'undeclared_fact': 1}], 'next_cursor': None})
    coordination = json.loads((Path(__file__).resolve().parents[2] / 'contracts'
                               / 'project_coordination_v1.schema.json').read_text(encoding='utf-8'))
    assert (summary['lastReport']['anyOf'][0]['properties']['kind']
            == coordination['$defs']['reportPointer']['properties']['kind'])


def test_owner_delegation_is_granted_only_from_the_owners_browser_and_revoked_at_once(
        flask_client, chat_sidecar, home_root, tmp_path, monkeypatch):
    """docs/ROUTER.md R9: a script, API key or agent cannot grant the main coordinator
    the owner's delegation; the owner's same-origin UI request can; any owner request
    revokes it. Open mode has no authentication, so its grant is recorded as advisory."""
    from lib import lifecycle_approval as gate
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    endpoint = '/api/v4/router/owner-delegation'
    before = flask_client.get(endpoint)
    assert before.status_code == 200, before.get_json()
    assert before.get_json()['grant']['state'] == 'revoked' and before.get_json()['active'] is False
    assert before.get_json()['actions'] == []
    unbound = flask_client.post(endpoint, json={'granted': True},
                                headers={'Idempotency-Key': 'g0', 'Sec-Fetch-Site': 'same-origin'})
    assert unbound.status_code == 403
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    for headers in ({'Idempotency-Key': 'g1'},
                    {'Idempotency-Key': 'g2', 'Sec-Fetch-Site': 'cross-site'},
                    {'Idempotency-Key': 'g3', 'Origin': 'http://evil.example'}):
        refused = flask_client.post(endpoint, json={'granted': True}, headers=headers)
        assert refused.status_code in (401, 403), (headers, refused.get_json())
    assert flask_client.get(endpoint).get_json()['grant']['state'] == 'revoked'
    granted = flask_client.post(endpoint, json={'granted': True},
                                headers={'Idempotency-Key': 'g4', 'Sec-Fetch-Site': 'same-origin',
                                         'User-Agent': 'Mozilla/5.0 (owner)'})
    assert granted.status_code == 200, granted.get_json()
    state = granted.get_json()
    assert state['active'] is True and state['grant']['state'] == 'granted'
    assert state['grant']['grantee']['conversationId'] == router['conversationId']
    assert state['grant']['assurance'] == 'advisory'
    assert state['grant']['grantedBy']['kind'] == 'human-ui'
    assert state['grant']['grantedBy']['userAgent'] == 'Mozilla/5.0 (owner)'
    revoked = flask_client.post(endpoint, json={'granted': False}, headers={'Idempotency-Key': 'r1'})
    assert revoked.status_code == 200, revoked.get_json()
    assert revoked.get_json()['grant']['state'] == 'revoked' and revoked.get_json()['active'] is False
    assert revoked.get_json()['grant']['revokedBy']['kind'] == 'authenticated-request'
    assert flask_client.post(endpoint, json={'granted': 'yes'},
                             headers={'Idempotency-Key': 'bad'}).status_code == 400


def test_office_reads_the_authenticated_owners_snapshot(flask_client, monkeypatch):
    from lib.router import office_facts
    calls = []

    def snapshot(owner):
        calls.append(owner)
        return {'contract': 'tofu.office-facts/v1', 'streamId': 'boot:1', 'seq': 3, 'facts': [
            {'fact': 'busy', 'key': 'busy:w', 'conversationId': 'w', 'source': 'attempt'}],
            'coverage': dict.fromkeys(office_facts.SOURCES, 'unsupported') | {'busy': 'complete'}}
    monkeypatch.setattr(office_facts, 'snapshot', snapshot)
    response = flask_client.get('/api/v4/router/office')
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['seq'] == 3
    assert calls == [1]


@pytest.mark.auth_mode('private')
def test_office_refuses_an_unauthenticated_reader_before_any_read(flask_client, monkeypatch):
    from lib.router import office_facts
    calls = []
    monkeypatch.setattr(office_facts, 'snapshot', calls.append)
    assert flask_client.get('/api/v4/router/office').status_code == 401
    assert calls == []
