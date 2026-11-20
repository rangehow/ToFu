"""HTTP wiring contract for the deterministic integration control plane."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

pytestmark = pytest.mark.api


def test_status_route_preserves_explicit_project_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    seen = []
    # The route lazy-loads the control plane via _integration_api() on first
    # use (boot-path deferral), so patch the authority module attribute.
    monkeypatch.setattr(
        'lib.integration_control.integration_status',
        lambda path, *, user_id: (
            seen.append((path, user_id))
            or {'ok': True, 'repo': {'root': path}, 'counts': {}}),
    )
    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().get(
            '/api/v1/project/integration/status?path=%2Ftmp%2Fexplicit-repo')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    assert body['ok'] is True
    assert body['repo']['root'] == '/tmp/explicit-repo'
    assert seen == [('/tmp/explicit-repo', 1)]


def _synthetic_workspace_rows(repo, count: int) -> list[dict]:
    return [
        {
            'id': index, 'user_id': 1, 'project_root': str(repo),
            'task_id': f'task-{index}', 'title': f'task-{index}',
            'workspace_path': '', 'managed': 0, 'state': 'merged',
            'base_sha': '', 'checkpoint_sha': '', 'candidate_sha': '',
            'error': '', 'origin': {}, 'created_at': 1.0,
            'updated_at': float(index),
        }
        for index in range(1, count + 1)
    ]


def _status_over_workspace_rows(tmp_path, monkeypatch, count: int):
    from lib import integration_control as control
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
    rows = _synthetic_workspace_rows(repo, count)
    monkeypatch.setattr(
        control._state, 'status_rows',
        lambda project_root, *, user_id: (rows, []))
    control._STATUS_CACHE.clear()

    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().get(
            '/api/v1/project/integration/status',
            query_string={'path': str(repo)})
        return response.status_code, await response.get_json()

    return asyncio.run(_run())


def test_status_endpoint_answers_more_workspaces_than_the_former_bound(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accepted workspace history above the old fixed row cap is a status, not a 500."""
    status, body = _status_over_workspace_rows(tmp_path, monkeypatch, 1001)

    assert status == 200
    assert [row['workId'] for row in body['workspaces']] == [
        f'task-{index}' for index in range(1, 1002)]
    assert body['counts'] == {'merged': 1001}
    assert all(row['state'] == 'merged' for row in body['workspaces'])


def test_status_endpoint_single_workspace_is_unchanged(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    status, body = _status_over_workspace_rows(tmp_path, monkeypatch, 1)

    assert status == 200
    assert [row['workId'] for row in body['workspaces']] == ['task-1']
    assert body['counts'] == {'merged': 1}


def test_status_of_a_missing_project_path_is_a_client_error(tmp_path) -> None:
    """A deleted or stale project selection is the caller's to correct, never a 500."""
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)
    missing = tmp_path / 'deleted-project'

    async def _run():
        response = await app.test_client().get(
            '/api/v1/project/integration/status', query_string={'path': str(missing)})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 400
    assert body['ok'] is False
    assert f'Project path does not exist: {missing}' in str(body)


def test_reconcile_route_preserves_explicit_user_and_project_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    seen = []
    monkeypatch.setattr(
        'lib.integration_control.reconcile_candidate_with_head',
        lambda path, *, user_id: (
            seen.append((path, user_id))
            or {'ok': True, 'changed': False, 'candidateSha': 'abc'}),
    )
    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().post(
            '/api/v1/project/integration/reconcile-head',
            json={'path': '/tmp/explicit-repo'},
        )
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    assert body['ok'] is True
    assert body['changed'] is False
    assert seen == [('/tmp/explicit-repo', 1)]


def test_all_control_actions_are_registered() -> None:
    from quart import Quart
    from routes.api_v1.integration import api_v1_integration_bp

    app = Quart(__name__)
    app.register_blueprint(api_v1_integration_bp)
    paths = {rule.rule: set(rule.methods or ()) for rule in app.url_map.iter_rules()}
    base = '/api/v1/project/integration/'
    assert 'GET' in paths[base + 'status']
    for action in ('create', 'register', 'checkpoint', 'submit', 'retry',
                   'discard', 'promote', 'reconcile-head', 'prune'):
        assert 'POST' in paths[base + action]


def test_unexpected_control_plane_failure_is_redacted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    def _crash(*_args, **_kwargs):
        raise RuntimeError('/secret/repo?credential=do-not-expose')

    monkeypatch.setattr(
        'lib.integration_control.integration_status', _crash)
    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().get(
            '/api/v1/project/integration/status?path=%2Ftmp%2Frepo')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 500
    assert body['ok'] is False
    assert '/secret/repo' not in str(body)
    assert 'do-not-expose' not in str(body)


def test_repeated_mutations_preserve_auth_and_domain_errors_without_peer_quota(monkeypatch):
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route
    import lib.rate_limit.rate_limit_store as store
    monkeypatch.setenv('TOFU_RATE_LIMIT', 'on')
    monkeypatch.setattr(store, 'get_store', lambda: pytest.fail('endpoint counter entered'))
    state = {'authenticated': True, 'failure': False}
    seen = []
    class IntegrationStateError(Exception):
        pass
    def reconcile(path, *, user_id):
        seen.append((path, user_id))
        if state['failure']:
            raise IntegrationStateError('candidate changed')
        return {'ok': True, 'changed': False}
    monkeypatch.setattr(route, '_integration_api', lambda name: reconcile)
    app = Quart(__name__)
    @app.before_request
    async def authenticate():
        g.auth_ctx = local_admin_context() if state['authenticated'] else None
    app.register_blueprint(route.api_v1_integration_bp)
    async def run():
        client = app.test_client()
        for _ in range(129):
            response = await client.post('/api/v1/project/integration/reconcile-head',
                                         json={'path': '/tmp/synthetic-project'})
            assert response.status_code == 200
            assert 'Retry-After' not in response.headers
        state['failure'] = True
        response = await client.post('/api/v1/project/integration/reconcile-head',
                                     json={'path': '/tmp/synthetic-project'})
        assert response.status_code == 400
        state['authenticated'] = False
        response = await client.post('/api/v1/project/integration/reconcile-head',
                                     json={'path': '/tmp/synthetic-project'})
        assert response.status_code == 401
    asyncio.run(run())
    assert len(seen) == 130
    assert all(item == ('/tmp/synthetic-project', 1) for item in seen)
