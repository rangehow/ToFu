"""Offline regression for the startup aggregate writer and the issued-route fence."""

import asyncio
from functools import partial
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conversation.test_restart_resume import RESTART, chats as chats, storage as storage

pytestmark = pytest.mark.unit
MODEL_REF = {'creator_id': 'creator', 'model_id': 'alpha'}


def _composition(monkeypatch):
    import server
    from lib import production_lifecycle as lifecycle
    from lib.app.app_assembly import create_base_app

    register = lifecycle.register_production_lifecycle
    monkeypatch.setattr(lifecycle._boot_profile, 'record_boot_phases', lambda *a, **kw: None)
    monkeypatch.setattr(lifecycle, 'register_production_lifecycle', partial(
        register, optional_services=lambda **_: ({}, False), shutdown_runtime=lambda *a, **kw: None))
    for name in ('_prepare_startup_artifacts', '_validate_storage_cutover_boundary',
                 '_start_storage_authority_sql', '_validate_imports'):
        monkeypatch.setattr(server, name, lambda: None)

    def compose(role, *, recovery=lambda: None, workers=lambda *a, **kw: None,
                post_ready=lambda: None):
        monkeypatch.setattr(server, 'DEPLOYMENT_CONFIGURATION',
                            SimpleNamespace(mode='personal', process_role=role))
        monkeypatch.setattr(server, '_init_database', recovery)
        monkeypatch.setattr(server, '_start_background_workers', workers)
        monkeypatch.setattr(server, '_run_post_ready_deferred_with_continuity', post_ready)
        app = create_base_app('offline-routing-' + role, {'TESTING': True})
        app.extensions['tofu_production_startup_gate'] = asyncio.Event()
        server.register_server_production_lifecycle(app)
        return app

    return compose


def _routing_fixture(monkeypatch):
    from lib import model_routing
    from lib.model_routing import repository as repos
    from tests.model_routing.test_native_route import Repository
    from tests.support.model_registration import complete_registration

    boundary = model_routing.OwnerBoundary.create(1)
    repo = repos.InMemoryModelRoutingRepository()
    monkeypatch.setattr(repos, 'notify_model_routing_changed', lambda **_: None)
    doc = complete_registration(Repository().document)
    repo.put_secret(boundary, 'synthetic-secret', secret_reference='secret-a')
    repo.compare_and_swap(boundary, doc, expected_revision=0)
    monkeypatch.setattr(model_routing, 'ModelRoutingRepository', lambda: repo)
    bootstrap = Mock(return_value={'status': 'already_active', 'revision': 1})
    monkeypatch.setattr(model_routing, 'bootstrap_owner_model_routing', bootstrap)
    monkeypatch.setattr('lib.oauth.token_store.load_stored_token', lambda *a, **kw: None)
    no_remote_login = Mock(side_effect=AssertionError('local convergence cannot discover remote login'))
    monkeypatch.setattr('lib.oauth.codex_shared_login.reconcile_shared_login', no_remote_login)
    monkeypatch.setattr('lib.model_routing.dispatch_adapter.prepare_ephemeral_slot',
                        lambda **kw: SimpleNamespace(slot=SimpleNamespace(oauth=kw.get('oauth'))))
    monkeypatch.setattr('lib.model_routing.dispatch_adapter.publish_ephemeral_slots', lambda _: None)
    secrets = Mock(wraps=repo.resolve_secret)
    monkeypatch.setattr(repo, 'resolve_secret', secrets)
    return repo, boundary, bootstrap, secrets, no_remote_login


@pytest.mark.parametrize('role', ['all', 'execution'])
@pytest.mark.parametrize('storage', ['sqlite'], indirect=True)
def test_local_commit_precedes_claim_and_restart_model_ref(monkeypatch, chats, role):
    from lib import model_routing, turn_lifecycle as turns
    from lib.conversations import restart_resume
    from lib.conversation_sync.command_service import restore_accepted_attempt_config
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.conversation_sync.worker_dispatch import dispatch_accepted_attempt
    from lib.durable_worker import StorageWorkerJobStore
    from lib.model_routing.native_route import NativeRouteError
    from lib.tasks_pkg.native_execution import compile_native_application_route
    from tests.model_routing.test_native_route import principal

    repo, boundary, bootstrap, secrets, no_remote_login = _routing_fixture(monkeypatch)
    compose = _composition(monkeypatch)
    held, release = threading.Event(), threading.Event()
    committed, recovered, dispatched = [], [], []
    resumed = threading.Event()
    cas = repo.compare_and_swap
    config = {'model': 'alpha', 'modelRef': MODEL_REF, 'preferredProviderId': 'provider-a'}
    owner = principal(1)

    def pause_before_cas(*args, **kwargs):
        if not committed:
            held.set()
            assert release.wait(15), 'test did not release local convergence'
        row = cas(*args, **kwargs)
        committed.append(row.revision)
        return row

    monkeypatch.setattr(repo, 'compare_and_swap', pause_before_cas)
    old = chats.running('restart', **config)
    restart_resume.record_quiesced_conversations([old])
    chats.settle(old, 'server_shutdown')
    chats.next_generation()

    def submit():
        pair = turns.create_turn_pair('queued', command_id='queued:generate',
            input_projection={'content': 'queued before convergence'},
            config=capture_execution_identity(config, 1, owner), user_id=1,
            conversation_defaults={'allowCreate': True, 'title': 'queued', 'settings': {}})
        row = dispatch_accepted_attempt(pair['attempt']['attemptId'], user_id=1)
        assert row['job']['status'] == 'queued'
        return pair['attempt']['attemptId']

    def enter_native(accepted_config):
        assert accepted_config['modelRef'] == MODEL_REF
        selection = model_routing.parse_native_model_selection({'model': accepted_config['modelRef']})
        group = model_routing.mint_routed_slot_group(repo, boundary, selection)
        route = compile_native_application_route(repo, owner, selection, issued_group=group)
        public, _private = route.consume(owner)  # fake transport; no provider
        assert public['revision'] == group.route_identity['revision']
        dispatched.append((public['revision'], public['logical_model'], group, selection))

    def claim_worker(_app, **_kwargs):
        job = StorageWorkerJobStore().claim_next(worker_id='fixture', task_kinds=('conversation-attempt',),
                                                   now_ms=int(time.time() * 1000), lease_ms=60_000)
        attempt = turns.get_attempt(job['payload']['attemptId'], user_id=1, include_execution_config=True)
        turn = turns.get_turn('queued', attempt['turnId'], user_id=1)
        enter_native(restore_accepted_attempt_config(turn, attempt, user_id=1,
                                                       accepted_config=attempt['config']))

    def post_ready():
        try:
            [result] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
            assert result['outcome'] == 'resumed'
            [(conversation, accepted_config)] = chats.started
            assert conversation == 'restart' and accepted_config['_turnOperation'] == 'continue'
            enter_native(accepted_config)
        finally:
            resumed.set()

    api = compose('api') if role == 'execution' else None
    app = compose(role, recovery=lambda: recovered.extend(committed),
                  workers=claim_worker, post_ready=post_ready)
    shard = compose('execution_shard') if api is not None else None

    async def exercise():
        try:
            async with app.test_app():
                assert await asyncio.to_thread(held.wait, 10)
                assert not app.extensions['tofu_production_startup_gate'].is_set()
                if api is not None:
                    async with api.test_app():
                        assert await api.extensions['tofu_boot_runner'].wait_done() == 'ready'
                        attempt_id = await asyncio.to_thread(submit)
                else:
                    attempt_id = await asyncio.to_thread(submit)
                queued = await asyncio.to_thread(turns.get_attempt, attempt_id, user_id=1)
                assert queued['status'] == 'pending'
                assert recovered == committed == dispatched == []
                secrets.assert_not_called()
                release.set()
                assert await app.extensions['tofu_boot_runner'].wait_done() == 'ready'
                assert await asyncio.to_thread(resumed.wait, 10)
                if shard is not None:
                    async with shard.test_app():
                        assert await shard.extensions['tofu_boot_runner'].wait_done() == 'ready'
        finally:
            release.set()

    asyncio.run(exercise())
    bootstrap.assert_called_once_with(boundary, repository=repo)
    no_remote_login.assert_not_called()
    assert committed == recovered == [2]
    assert [(rev, model) for rev, model, _, _ in dispatched] == [(2, 'alpha'), (2, 'alpha')]

    # A legitimate later authority edit must still retire the original witness,
    # even though it doesn't break the selected route's identity or eligibility.
    current = repo.get(boundary)
    current.document['creators'][0]['name'] = 'Updated by owner'
    repo.compare_and_swap(boundary, current.document, expected_revision=current.revision)
    _, _, issued, selection = dispatched[0]
    secrets.reset_mock()
    with pytest.raises(NativeRouteError) as error:
        compile_native_application_route(repo, owner, selection, issued_group=issued)
    assert error.value.kind == 'native_route_changed'
    secrets.assert_not_called()


@pytest.mark.parametrize('role', ['all', 'execution'])
def test_startup_convergence_failure_keeps_claims_and_recovery_closed(monkeypatch, role):
    _routing_fixture(monkeypatch)
    compose = _composition(monkeypatch)
    recovery, workers, post_ready = Mock(), Mock(), Mock()
    rejected = Mock(side_effect=RuntimeError('fixture local catalogue commit failed'))
    monkeypatch.setattr('lib.model_routing.world_catalog.sync_world_model_catalog', rejected)
    app = compose(role, recovery=recovery, workers=workers, post_ready=post_ready)

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'failed'
            assert runner.snapshot()['failedPhase']['key'] == 'model_routing'
            assert not app.extensions['tofu_production_startup_gate'].is_set()

    asyncio.run(exercise())
    rejected.assert_called_once()
    recovery.assert_not_called()
    workers.assert_not_called()
    post_ready.assert_not_called()


def test_old_mint_then_world_sync_order_rejects_before_secret(monkeypatch):
    from lib import model_routing
    from lib.model_routing.native_route import NativeRouteError
    from lib.model_routing.world_catalog import sync_world_model_catalog
    from lib.tasks_pkg.native_execution import compile_native_application_route
    from tests.model_routing.test_native_route import principal, selection

    repo, boundary, _, secrets, _ = _routing_fixture(monkeypatch)
    selected = selection()
    issued = model_routing.mint_routed_slot_group(repo, boundary, selected)
    assert issued.route_identity['revision'] == 1
    assert sync_world_model_catalog(boundary=boundary, repository=repo)['revision'] == 2
    secrets.reset_mock()
    with pytest.raises(NativeRouteError) as error:
        compile_native_application_route(repo, principal(1), selected, issued_group=issued)
    assert error.value.kind == 'native_route_changed'
    secrets.assert_not_called()


@pytest.mark.parametrize('stop_before_ready', [False, True])
def test_codex_feed_waits_for_readiness_without_a_second_local_writer(monkeypatch, stop_before_ready):
    from lib.server import server_background_services as background

    feed = Mock(return_value=True)
    local = Mock(side_effect=AssertionError('duplicate post-ready convergence'))
    monkeypatch.setattr('lib.oauth.codex_catalog.start_codex_catalog_refresher', feed)
    monkeypatch.setattr('lib.oauth.outbound.reconcile_oauth_providers', local)
    monkeypatch.setattr('lib.model_routing.world_catalog.sync_world_model_catalog', local)
    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(mode='personal'))
    monkeypatch.setattr('lib.browser.bridge_relay.configure_bridge_owner', lambda **_: None)
    monkeypatch.setattr('lib.desktop.bridge_relay.configure_bridge_owner', lambda **_: None)
    monkeypatch.setattr('lib.mcp.bridge_relay.configure_mcp_owner', lambda **_: None)

    async def exercise():
        gate, stop = asyncio.Event(), asyncio.Event()
        tasks = []
        def spawn(coro, **kwargs):
            tasks.append(asyncio.create_task(coro, **kwargs))
        app = SimpleNamespace(extensions={
            'tofu_production_startup_gate': gate, 'tofu_shutdown_requested': stop,
            'tofu_serving_loop_lifecycle': {'runtime': SimpleNamespace(create_task=spawn)},
        })
        assert background.start_registered_background_services(app, process_role='api') == 0
        assert background.start_registered_background_services(app, process_role='api') == 0
        feed.assert_not_called()
        if stop_before_ready:
            stop.set()
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(exercise())
    local.assert_not_called()
    if stop_before_ready:
        feed.assert_not_called()
    else:
        feed.assert_called_once_with(discover_shared_login=True)


def test_split_api_fences_status_and_startup_feed_until_execution_ready(monkeypatch):
    from quart import Quart, g
    from lib.http_transport.request_lifecycle import enforce_boot_gate
    from lib.oauth import codex_catalog, codex_shared_login, outbound, token_store
    from lib.server import server_background_services as background
    from lib.tasks_pkg.command_control_runtime import CommandControls
    from routes.api_v1 import oauth
    from routes.api_v4 import API_V4_PRODUCT_BLUEPRINTS

    _, _, _, _, discover = _routing_fixture(monkeypatch)
    monkeypatch.setattr(discover, 'side_effect', None)
    monkeypatch.setattr(token_store, 'load_token', lambda _: None)
    monkeypatch.setattr('lib.desktop.egress.egress_status', lambda *a, **kw: {})
    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(mode='personal'))
    monkeypatch.setattr('lib.browser.bridge_relay.configure_bridge_owner', lambda **_: None)
    monkeypatch.setattr('lib.desktop.bridge_relay.configure_bridge_owner', lambda **_: None)
    monkeypatch.setattr('lib.mcp.bridge_relay.configure_mcp_owner', lambda **_: None)
    monkeypatch.setitem(oauth._oauth_reconcile_state, 'at', float('-inf'))
    reconcile = Mock(wraps=outbound.reconcile_oauth_providers)
    monkeypatch.setattr(outbound, 'reconcile_oauth_providers', reconcile)
    feed_started, checked_from_feed = threading.Event(), threading.Event()
    feed_refresh = Mock(side_effect=lambda **_: (
        feed_started.set() or {'skipped': 'not_authenticated'}))
    monkeypatch.setattr(codex_catalog, 'refresh_codex_model_catalog', feed_refresh)
    service_id = 'offline-primary'
    record = {'state': 'starting', 'serviceId': service_id}

    def service_record():
        if threading.current_thread().name != 'MainThread':
            checked_from_feed.set()
        return dict(record)

    controls = CommandControls(service=SimpleNamespace(
        record=service_record, host_matches=lambda _: True))

    async def exercise():
        app = Quart(__name__)
        app.before_request(enforce_boot_gate)

        @app.before_request
        async def authorize():
            from lib.api_keys._context import local_admin_context
            from routes.api_v1.auth import _stamp_principal
            g.auth_ctx = local_admin_context()
            _stamp_principal()

        app.register_blueprint(oauth.api_v1_oauth_bp)
        app.register_blueprint(next(bp for bp in API_V4_PRODUCT_BLUEPRINTS
                                    if bp.name == oauth.api_v1_oauth_bp.name + '_v4'))
        app.add_url_rule('/api/ordinary-read', 'ordinary_read', lambda: {'ok': True})
        app.add_url_rule('/api/generate-fixture', 'generate_fixture', lambda: {'ok': True}, methods=['POST'])
        gate = asyncio.Event()
        tasks = []
        def spawn(coro, **kwargs):
            tasks.append(asyncio.create_task(coro, **kwargs))
        app.extensions.update({
            'tofu_production_startup_gate': gate,
            'tofu_serving_loop_lifecycle': {'runtime': SimpleNamespace(create_task=spawn)},
            'tofu_boot_runner': SimpleNamespace(status='ready', snapshot=lambda: {'status': 'ready'}),
            'tofu_command_controls': controls,
        })
        assert background.start_registered_background_services(app, process_role='api') == 0
        gate.set()
        assert await asyncio.to_thread(checked_from_feed.wait, 5)
        async with app.test_client() as client:
            try:
                for path in ('/api/v1/oauth/status', '/api/v4/oauth/status'):
                    assert (await client.get(path)).status_code == 503
                assert (await client.post('/api/generate-fixture')).status_code == 503
                assert (await client.get('/api/ordinary-read')).status_code == 200
                record['state'] = 'failed'
                failed = await client.get('/api/v1/oauth/status')
                assert failed.status_code == 503
                assert (await failed.get_json())['boot']['failedPhase']['key'] == 'execution_service'
                reconcile.assert_not_called()
                discover.assert_not_called()
                feed_refresh.assert_not_called()
                record.update(state='ready', commandControl={'serviceId': service_id})
                assert await asyncio.to_thread(feed_started.wait, 5)
                await asyncio.gather(*tasks)
                response = await client.get('/api/v4/oauth/status')
                assert response.status_code == 200, await response.get_json()
                reconcile.assert_called_once_with(owner_user_id=1)
                feed_refresh.assert_called_once()
                assert discover.call_count == 2  # feed discovery and status reconciliation
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                assert await asyncio.to_thread(codex_catalog.stop_codex_catalog_refresher)

    asyncio.run(exercise())


@pytest.mark.parametrize('role', ['all', 'execution'])
def test_required_worker_start_failure_prevents_readiness(monkeypatch, role):
    from lib.durable_worker import runtime as worker_runtime
    from lib.server import server_background_services as background
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    _routing_fixture(monkeypatch)
    compose = _composition(monkeypatch)
    fail = Mock(side_effect=RuntimeError('durable claim owner failed to arm'))
    monkeypatch.setattr(worker_runtime, 'ensure_workers_started', fail)
    monkeypatch.setattr(background, 'start_registered_background_services', lambda *a, **kw: 0)
    monkeypatch.setattr('lib.tools.environment_capabilities.record_environment_launch_facts', lambda _: None)
    monkeypatch.setattr('lib.tasks_pkg.command_control_runtime.configure', lambda *a, **kw: None)
    monkeypatch.setattr('lib.conversation_sync.delivery_runtime.ensure_started', lambda _: None)
    post_ready, published = Mock(), Mock()
    app = compose(role, post_ready=post_ready, workers=lambda app, **_: background.start_background_services(
        app, process_role=role, load_saved_proxy_config=lambda: None,
        bootstrap_personal_key=lambda: None, environ={}))

    async def exercise():
        if role == 'execution':
            with pytest.raises(RuntimeError, match='durable claim owner failed to arm'):
                await run_execution_lifespan(app, asyncio.Event(), published)
        else:
            async with app.test_app():
                assert await app.extensions['tofu_boot_runner'].wait_done() == 'failed'
        assert app.extensions['tofu_boot_runner'].snapshot()['failedPhase']['key'] == 'background_workers'
        assert not app.extensions['tofu_production_startup_gate'].is_set()

    asyncio.run(exercise())
    fail.assert_called_once()
    published.assert_not_called()
    post_ready.assert_not_called()


@pytest.mark.parametrize('version', ['v1', 'v4'])
def test_oauth_command_gets_require_composite_readiness(monkeypatch, version):
    from quart import Quart, g
    from lib.desktop import adapter as desktop
    from lib.http_transport.request_lifecycle import enforce_boot_gate
    from lib.oauth import outbound
    from lib.oauth.manager import _flow
    from routes.api_v1 import adapter, oauth
    from routes.api_v4 import API_V4_PRODUCT_BLUEPRINTS

    # Real mounted GET handlers and login/sync flows; only external I/O and
    # the writers they would invoke are substituted.
    writes = Mock(return_value=True)
    discover = Mock()
    refresh = Mock()
    monkeypatch.setattr(_flow, '_active_flows', {})
    monkeypatch.setattr('lib.oauth.manager._state._active_flows', _flow._active_flows)
    monkeypatch.setattr('lib.oauth.manager._exchange._update_active_flow', lambda *a, **kw: None)
    monkeypatch.setattr('lib.oauth.manager._exchange.audit_log', lambda *a, **kw: None)
    monkeypatch.setattr('lib.oauth.manager._device.stop_device_flow', lambda _: None)
    monkeypatch.setattr('lib.oauth.codex_shared_login.local_owner', lambda owner: owner)
    monkeypatch.setattr('lib.oauth.codex_shared_login.reconcile_shared_login', discover)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _: {
        'codex_shared_reference': {'kind': 'local'}, 'access_token': 'fixture-only'})
    monkeypatch.setattr(outbound, 'provision_oauth_provider', writes)
    monkeypatch.setattr(outbound, 'managed_oauth_provider_status', lambda *a, **kw: {'provider_ready': True})
    monkeypatch.setattr('lib.oauth.codex_catalog.trigger_codex_catalog_refresh', refresh)
    monkeypatch.setattr(adapter, '_known_agent', lambda *a: {'name': 'Fixture desktop'})
    management = Mock(return_value={'status': 'ok'})
    monkeypatch.setattr(desktop, '_management_request', management)
    monkeypatch.setattr(desktop, 'policy_for', lambda _: {'api_key': 'fixture-only'})
    monkeypatch.setattr(desktop, 'fetch_models', lambda *a, **kw: ['fixture-model'])
    monkeypatch.setattr(desktop, 'provision_provider', writes)
    monkeypatch.setattr(desktop, 'adapter_accounts', lambda *a, **kw: [{'provider': 'codex'}])
    monkeypatch.setattr(desktop, '_adapter_provider_status', lambda *a, **kw: {'provider_ready': False})
    monkeypatch.setattr(desktop, '_status_cache', SimpleNamespace(get=lambda _: None, set=lambda *a: None))
    monkeypatch.setattr('lib.desktop.list_agents', lambda **_: [
        {'agent_id': 'fixture', 'online': True, 'capabilities': {'egress': True}}])
    adapter_status = Mock(return_value=({'running': True}, None))
    monkeypatch.setattr('lib.desktop.send_desktop_command', adapter_status)
    monkeypatch.setattr(desktop, '_invalidate_adapter_caches', lambda *a: None)

    app = Quart(__name__)
    app.before_request(enforce_boot_gate)
    for blueprint in (oauth.api_v1_oauth_bp, adapter.api_v1_adapter_bp):
        app.register_blueprint(blueprint if version == 'v1' else next(
            bp for bp in API_V4_PRODUCT_BLUEPRINTS if bp.name == blueprint.name + '_v4'))
    app.extensions['tofu_boot_runner'] = SimpleNamespace(status='ready', snapshot=lambda: {'status': 'ready'})
    execution = {'state': 'starting', 'error': ''}
    app.extensions['tofu_command_controls'] = SimpleNamespace(readiness=lambda: dict(execution))

    @app.before_request
    async def authorize():
        from lib.api_keys._context import local_admin_context
        from routes.api_v1.auth import _stamp_principal
        g.auth_ctx = local_admin_context()
        _stamp_principal()

    async def exercise():
        client = app.test_client()
        paths = (f'/api/{version}/oauth/device-login?provider=codex',
                 f'/api/{version}/adapter/oauth/status?agent_id=fixture&state=fixture',
                 f'/api/{version}/adapter/status')
        for state in ('starting', 'unavailable'):
            execution.update(state=state, error='Fixture execution not ready')
            for path in paths:
                assert (await client.get(path)).status_code == 503
        for effect in (discover, refresh, writes, management, adapter_status):
            effect.assert_not_called()
        execution['state'] = 'ready'
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200, await response.get_json()
        assert writes.call_count == 3
        discover.assert_called_once_with(owner_user_id=1, explicit=True)
        refresh.assert_called_once()
        management.assert_called_once()
        adapter_status.assert_called_once()

    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['watch_failure', 'executor_failure', 'idle', 'stop'])
def test_headless_boot_waits_for_real_worker_enrollment(monkeypatch, outcome):
    from lib.durable_worker import runtime as worker_runtime
    from lib.server import server_background_services as background
    from lib.storage.errors import StorageError
    from lib.storage.worker_wake import FORMAT
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    _routing_fixture(monkeypatch)
    compose = _composition(monkeypatch)
    monkeypatch.setattr(background, 'start_registered_background_services', lambda *a, **kw: 0)
    monkeypatch.setattr('lib.tools.environment_capabilities.record_environment_launch_facts', lambda _: None)
    monkeypatch.setattr('lib.integration_control.ensure_worker_started', lambda: False)
    monkeypatch.setattr('lib.project_mod.task_spares.warm', lambda: None)
    create = worker_runtime.create_service
    enrolled, release, interrupted, phase_returned = (threading.Event() for _ in range(4))
    initial_queries, failures = [], []
    failure = StorageError('database_protocol_error', 'fixture worker setup rejected')
    services = []
    if outcome == 'executor_failure':
        executor = Mock(side_effect=failure)
        monkeypatch.setattr('lib.durable_worker.service.DemandThreadPoolExecutor', executor)

    class Client:
        def worker_watch(self, *, cursor, **_):
            if cursor is None:
                initial_queries.append(threading.get_ident())
                enrolled.set()
                assert release.wait(10), 'test did not release worker enrollment'
                if outcome == 'watch_failure' or interrupted.is_set():
                    raise failure
            else:
                assert interrupted.wait(10), 'test did not stop the enrolled watch'
            return {'format': FORMAT,
                    'cursor': {'epoch': 'fixture', 'queue': 0, 'cancel': 0}, 'cancelTasks': []}

    class ClosedClaimGate:
        def bind(self, wake):
            pass

        def admit(self, **_):
            return False, None  # shard gate stays closed until publication

    def create_fixture(**kwargs):
        service = create(client_factory=lambda **_: Client(), **kwargs)
        stop_watch = service._wake_source.interrupt

        def interrupt_watch():
            stop_watch()
            interrupted.set()
            release.set()

        monkeypatch.setattr(service._wake_source, 'interrupt', interrupt_watch)
        services.append(service)
        return service

    monkeypatch.setattr(worker_runtime, 'create_service', create_fixture)

    def workers(app, **_):
        try:
            background.start_background_services(app, process_role='execution', environ={},
                load_saved_proxy_config=lambda: None, bootstrap_personal_key=lambda: None)
        except Exception as exc:
            failures.append(exc)
            raise
        finally:
            phase_returned.set()

    app = compose('execution', workers=workers)
    app.extensions[worker_runtime.CLAIM_GATE_EXTENSION] = ClosedClaimGate()

    @app.after_serving
    async def join_workers():
        await asyncio.to_thread(worker_runtime.stop_workers, app)

    async def exercise():
        stop = asyncio.Event()
        published = asyncio.Event()
        boot = asyncio.create_task(run_execution_lifespan(app, stop, published.set))
        try:
            assert await asyncio.to_thread(enrolled.wait, 5)
            assert initial_queries == [services[0]._thread.ident]
            assert services[0].snapshot()['state'] == 'starting'
            assert not published.is_set() and not phase_returned.is_set()
            if outcome != 'stop':
                release.set()
            if outcome == 'idle':
                await asyncio.wait_for(published.wait(), 5)
                assert services[0].snapshot()['state'] == 'running'
                assert not services[0].start()  # healthy idempotent startup
                assert services[0].snapshot()['inflight'] == 0
            if outcome.endswith('_failure'):
                with pytest.raises(RuntimeError, match='fixture worker setup rejected'):
                    await asyncio.wait_for(boot, 5)
                assert failures == [failure]  # same typed authority error
                assert not published.is_set()
            else:
                stop.set()
                await asyncio.wait_for(boot, 5)
                if outcome == 'stop':
                    assert not published.is_set()
                    with pytest.raises(RuntimeError, match='during shutdown'):
                        worker_runtime.ensure_workers_started(app)
            assert await asyncio.to_thread(phase_returned.wait, 5)
            assert not services[0]._thread.is_alive()
            assert services[0].snapshot()['state'] == (
                'failed' if outcome.endswith('_failure') else 'stopped')
        finally:
            stop.set()
            release.set()
            await asyncio.gather(boot, return_exceptions=True)

    asyncio.run(exercise())
