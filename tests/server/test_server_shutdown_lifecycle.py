"""Contracts for bounded cleanup inside Quart's shutdown lifespan."""

from __future__ import annotations

import asyncio
import importlib
import threading

import pytest

from tests.support.dx_overlay import requires_dx

from lib.server.server_shutdown import (
    _stop_storage_boundary_for_shutdown,
    _shutdown_loaded_module,
    shutdown_production_runtime,
)


pytestmark = pytest.mark.unit


def test_optional_shutdown_joins_partial_import_and_preserves_unused_laziness(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    import sys
    from types import SimpleNamespace
    entered, release = threading.Event(), threading.Event()
    events = []
    monkeypatch.setitem(sys.modules, 'shutdown_import_state',
        SimpleNamespace(entered=entered, release=release, events=events))
    module_name = 'shutdown_partial_import'
    (tmp_path / (module_name + '.py')).write_text(
        'from shutdown_import_state import entered, release, events\n'
        'entered.set()\nassert release.wait(5)\n'
        'def close(): events.append("closed")\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            importing = workers.submit(importlib.import_module, module_name)
            try:
                assert entered.wait(5)
                closing = workers.submit(_shutdown_loaded_module, module_name, 'close')
                with pytest.raises(TimeoutError): closing.result(timeout=.03)
                assert events == []
            finally:
                release.set()
            importing.result(timeout=5); closing.result(timeout=5)
        assert events == ['closed']
    finally:
        sys.modules.pop(module_name, None)
    _shutdown_loaded_module('unused_shutdown_dependency', 'close')
    assert 'unused_shutdown_dependency' not in sys.modules


@pytest.fixture(autouse=True)
def isolate_incremental_shutdown(monkeypatch):
    from lib.translate import incremental
    monkeypatch.setattr(incremental, 'shutdown_incremental_translations', lambda: None)


def _run_async(awaitable):
    """Run an awaitable despite a loop marker left by a browser fixture."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result = []
    failure = []

    def runner():
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=runner, name='shutdown-test-loop')
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), 'isolated shutdown test loop did not finish'
    if failure:
        raise failure[0]
    return result[0] if result else None


def test_parallel_producer_shutdown_joins_workers_before_propagating_cancellation(monkeypatch):
    from lib.server import server_shutdown
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def close(module, function, **options):
        if function == 'stop_prober':
            entered.set()
            assert release.wait(5)
            exited.set()
        elif function == 'stop_fs_keepalive':
            raise RuntimeError('independent cleanup fault')

    monkeypatch.setattr(server_shutdown, '_shutdown_loaded_module', close)

    async def run():
        cleanup = asyncio.create_task(server_shutdown._stop_independent_producers(
            server_shutdown._MODULE_LOG))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(2):
                cleanup.cancel()
                await asyncio.sleep(.01)
                assert not cleanup.done(), 'cancellation abandoned a running cleanup worker'
        finally:
            release.set()
            result = (await asyncio.gather(cleanup, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert exited.is_set()

    _run_async(run())


@pytest.mark.parametrize(('automation_stop_fails', 'failing_flush', 'route_stop_fails'), [
    (False, None, False), (True, None, False), (False, 'key-stats', False),
    (False, 'usage', False), (False, None, True),
])
@pytest.mark.parametrize('parallel_producers', [False, True], ids=['normal', 'parallel'])
def test_shutdown_closes_all_runtime_owners_and_offloads_sync_joins(
        monkeypatch, automation_stop_fails, failing_flush, parallel_producers, route_stop_fails):
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', '30')
    import lib.agent_core.admission as admission
    import lib.agent_core.push as push
    import lib.cgroup_guard as cgroup_guard
    import lib.fs_keepalive as fs_keepalive
    import lib.http_transport.client as http_client
    import lib.key_stats as key_stats
    import lib.search.search_runtime as search_runtime
    import lib.scheduler.timer as timer_runtime
    import lib.integration_control as integration_control
    import lib.llm_dispatch.health_local as health_local
    import lib.mcp.client as mcp_client
    import lib.mcp.startup as mcp_startup
    import lib.netpath as netpath
    import lib.presence as presence
    import lib.pricing as pricing
    import lib.runtime.runtime_state_store as runtime_state_store
    import lib.server.server_background_services as background_services
    import lib.storage as storage
    import lib.storage.warmup as warmup
    import lib.swarm.integration as swarm_integration
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.event_delivery as event_delivery
    import lib.tasks_pkg.event_log as event_log
    import lib.usage_tracker as usage_tracker
    import routes
    from lib.durable_worker import runtime as automation_runtime

    calls = []
    serving_loop_threads = []
    producers_entered = threading.Barrier(2, timeout=5)
    producers_joined = []

    def sync_call(name):
        def run(*args, **kwargs):
            calls.append((name, threading.get_ident(), kwargs))
            if parallel_producers and name in ('netpath', 'fs-keepalive'):
                producers_entered.wait()
                producers_joined.append(name)
        return run

    def flush_call(name):
        def run(*args, **kwargs):
            calls.append((name, threading.get_ident(), kwargs))
            if name == failing_flush:
                raise OSError(f'{name} store unavailable')
        return run

    def stop_automation(*args, **kwargs):
        sync_call('automation')(*args, **kwargs)
        if automation_stop_fails:
            raise RuntimeError('Owned work has not quiesced')

    monkeypatch.setattr(automation_runtime, 'stop_workers', stop_automation)

    async def close_http_clients():
        calls.append(('http', threading.get_ident(), {}))

    class Store:
        close = sync_call('runtime')

    class Bridge:
        disconnect_all = sync_call('mcp')

    quiesce_reasons = []

    def quiesce_tasks(*, reason):
        quiesce_reasons.append(reason)
        return 0

    monkeypatch.setattr(task_manager, 'quiesce_running_tasks', quiesce_tasks)
    monkeypatch.setattr(
        swarm_integration, 'stop_swarm_cleanup_timer',
        sync_call('swarm-cleanup'))
    monkeypatch.setattr(
        swarm_integration, 'stop_swarm_output_cleanup',
        sync_call('swarm-output-cleanup'))
    monkeypatch.setattr(netpath, 'stop_prober', sync_call('netpath'))
    monkeypatch.setattr(
        background_services, 'stop_lan_discovery_responder',
        sync_call('lan-discovery'))
    monkeypatch.setattr(cgroup_guard, 'stop_monitor', sync_call('cgroup'))
    monkeypatch.setattr(
        health_local, 'stop_local_health_checker', sync_call('local-health'))
    monkeypatch.setattr(
        fs_keepalive, 'stop_fs_keepalive', sync_call('fs-keepalive'))
    monkeypatch.setattr(presence, 'stop_sweeper', sync_call('presence'))
    monkeypatch.setattr(
        integration_control, 'stop_worker', sync_call('integration'))
    monkeypatch.setattr(
        pricing, 'stop_pricing_refresh', sync_call('pricing-refresh'))
    def stop_routes(app, **options):
        sync_call('route-services')(**options)
        app.extensions['tofu_registered_background_services'] = route_stop_fails
        app.extensions[background_services.LATE_OWNERS_KEY] = (
            (background_services.LateBackgroundOwner(
                'proactive scheduler', writes_storage=True, reason='still running'),)
            if route_stop_fails else ())

    monkeypatch.setattr(background_services, 'stop_registered_background_services', stop_routes)
    monkeypatch.setattr(mcp_client, 'disconnect_all_bridges', Bridge().disconnect_all)
    monkeypatch.setattr(
        mcp_startup, 'stop_mcp_auto_connect', sync_call('mcp-startup'))
    monkeypatch.setattr(admission.controller, 'shutdown', sync_call('admission'))
    def stop_event_delivery(**kwargs):
        sync_call('event-delivery')(**kwargs)
        return True

    monkeypatch.setattr(event_delivery, 'shutdown_event_delivery', stop_event_delivery)
    monkeypatch.setattr(push.hub, 'stop', sync_call('push'))
    monkeypatch.setattr(http_client, 'close_http_clients', close_http_clients)
    monkeypatch.setattr(timer_runtime, 'shutdown_timer_watchers', sync_call('timers'))
    monkeypatch.setattr(search_runtime, 'shutdown_search_browser_runtime', sync_call('search-browser'))
    from lib.translate import incremental
    monkeypatch.setattr(incremental, 'shutdown_incremental_translations', sync_call('incremental-translation'))
    monkeypatch.setattr(runtime_state_store, 'get_store', lambda: Store())
    monkeypatch.setattr(
        event_log, 'stop_storage_maintenance', sync_call('event-maintenance'))
    monkeypatch.setattr(event_log, 'stop_sidecar_batcher',
                        sync_call('event-batcher'))
    monkeypatch.setattr(storage, 'stop_storage', sync_call('storage'))
    monkeypatch.setattr(warmup, 'stop_keep_warm', sync_call('storage-warmup'))
    monkeypatch.setattr(key_stats, 'shutdown_key_stats', flush_call('key-stats'))
    monkeypatch.setattr(usage_tracker, 'flush', flush_call('usage'))

    stopped = threading.Event()
    stopped.set()
    from types import SimpleNamespace
    async def close_native():
        # Native close can settle a task immediately. Its cause must already
        # identify service recovery rather than fabricate a human stop.
        assert quiesce_reasons == ['server_shutdown']
        calls.append(('native-host',threading.get_ident(),{}))
    app = SimpleNamespace(extensions={'tofu_native_host':SimpleNamespace(close=close_native)})

    async def exercise():
        serving_loop_threads.append(threading.get_ident())
        await shutdown_production_runtime(stopped, app=app)
        assert calls[0][0]=='native-host'

    if automation_stop_fails:
        with pytest.raises(RuntimeError, match='Owned work has not quiesced'):
            _run_async(exercise())
        assert 'automation' in [name for name, _, _ in calls]
        assert 'storage' not in [name for name, _, _ in calls]
        return
    if route_stop_fails:
        # The raised cause names the owner and why: shutdown's logging is
        # already draining, so it is the only record that reaches the manager.
        with pytest.raises(background_services.BackgroundOwnersStillLive,
                           match=r'have not quiesced: proactive scheduler \(still running\) .*storage stays open'):
            _run_async(exercise())
        assert 'route-services' in [name for name, _, _ in calls]
        assert {'event-delivery', 'key-stats', 'usage', 'event-batcher'} <= {
            name for name, _, _ in calls}
        assert 'storage' not in [name for name, _, _ in calls]
        return
    _run_async(exercise())
    loop_thread = serving_loop_threads[0]
    assert calls[0]==('native-host',loop_thread,{})
    calls=calls[1:]
    if parallel_producers:
        assert set(producers_joined) == {'netpath', 'fs-keepalive'}

    # Key-health and per-key usage counters are separate in-memory owners: a
    # failed flush of either must neither skip the other nor later teardown.
    names = [name for name, _, _ in calls]
    assert names[:3] == ['timers', 'incremental-translation', 'search-browser']
    assert set(names[3:13]) == {
        'swarm-cleanup', 'swarm-output-cleanup', 'netpath', 'lan-discovery',
        'cgroup', 'local-health', 'fs-keepalive', 'presence', 'integration',
        'automation',
    }
    # Automation and every independent producer finish before consumers,
    # final evidence flushes, and the storage boundary retire.
    assert names[12:] == [
        'automation', 'route-services', 'storage-warmup', 'pricing-refresh',
        'mcp-startup', 'mcp', 'admission', 'event-delivery', 'push', 'http', 'runtime',
        'key-stats', 'usage', 'event-maintenance', 'event-batcher', 'storage',
    ]
    assert next(thread for name, thread, _ in calls if name == 'http') == loop_thread
    for name, thread, _ in calls:
        if name != 'http':
            assert thread != loop_thread
    assert calls[-3][2] == {'timeout': 2.0}
    assert calls[-1][2] == {'timeout': 15.0}
    assert calls[-2][2] == {'timeout': 3.0}
    assert calls[0][2] == {}
    assert calls[1][2] == {}
    by_owner = {name: kwargs for name, _, kwargs in calls}
    assert by_owner['search-browser'] == {}
    assert by_owner['swarm-cleanup'] == {'timeout': 2.0}
    assert by_owner['swarm-output-cleanup'] == {'timeout': 2.0}
    assert by_owner['netpath'] == {}
    assert by_owner['key-stats'] == {}
    assert by_owner['usage'] == {}
    assert by_owner['event-delivery'] == {'timeout': 10.0}
    assert all(by_owner[name] == {'timeout': 2.0}
               for name in ('lan-discovery', 'cgroup', 'local-health', 'fs-keepalive', 'presence'))
    assert all(kwargs == {'timeout': 2.0} for name, _, kwargs in calls
               if name in {'integration', 'route-services', 'storage-warmup', 'pricing-refresh', 'mcp-startup'})


@pytest.mark.parametrize(('hard_deadline', 'storage_timeout'), [
    ('10', 5.0), ('30', 15.0), ('60', 30.0), ('300', 150.0),
])
def test_storage_shutdown_certifies_reexec_only_after_release(
    monkeypatch, hard_deadline, storage_timeout,
):
    import lib.server.server_reexec as server_reexec
    import lib.storage as storage

    calls = []
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', hard_deadline)

    def stop_storage(*, timeout):
        calls.append(('stop', timeout))

    def certify():
        calls.append(('certify', None))
        return True

    class Log:
        def info(self, *_args):
            calls.append(('log', None))

        def warning(self, *_args):
            raise AssertionError('successful release must not warn')

    monkeypatch.setattr(storage, 'stop_storage', stop_storage)
    monkeypatch.setattr(
        server_reexec,
        'confirm_server_reexec_storage_boundary_released',
        certify,
    )

    assert _run_async(_stop_storage_boundary_for_shutdown(Log())) is True
    kinds = [name for name, _ in calls]
    assert kinds[0] == 'stop' and calls[0][1] == storage_timeout
    # Durable boundary: certification happens only after the release call
    # returned; incidental logging between the two is not part of the contract.
    assert 'certify' in kinds
    assert kinds.index('certify') > kinds.index('stop')


def test_storage_shutdown_failure_cannot_certify_reexec(monkeypatch):
    import lib.server.server_reexec as server_reexec
    import lib.storage as storage

    certified = []
    warnings = []

    def fail_stop(*, timeout):
        raise RuntimeError(f'still alive after {timeout}s')

    class Log:
        def info(self, *_args):
            raise AssertionError('failed release must not report success')

        def warning(self, *args):
            warnings.append(args)

    monkeypatch.setattr(storage, 'stop_storage', fail_stop)
    monkeypatch.setattr(
        server_reexec,
        'confirm_server_reexec_storage_boundary_released',
        lambda: certified.append(True) or True,
    )

    assert _run_async(_stop_storage_boundary_for_shutdown(Log())) is False
    assert certified == []
    assert warnings


def test_legacy_billing_janitor_facade_is_thread_free():
    import lib.billing.janitor as janitor

    assert janitor.start_janitor() is False
    assert janitor.stop_janitor(timeout=0.25) is True


def test_legacy_daily_report_scheduler_facade_is_thread_free(monkeypatch):
    import lib.daily_report.scheduler as scheduler
    import lib.scheduler.manager as manager
    from lib.identity import PrincipalContext

    monkeypatch.setattr(
        manager, 'ensure_daily_report_schedule', lambda **_kwargs: False)
    principal = PrincipalContext.system(
        subject_id='daily-report-test',
        owner_user_id=1,
        scopes={'reports:maintain'},
    )
    assert scheduler.start_report_scheduler(principal=principal) is False
    assert scheduler.stop_report_scheduler(timeout=0.25) is True
    assert not hasattr(scheduler, '_scheduler_thread')


@pytest.mark.parametrize(('module_name', 'thread_name', 'event_names', 'stop_name'), [
    ('lib.cgroup_guard', '_monitor_thread', ('_monitor_stop',), 'stop_monitor'),
    ('lib.database_panel.maintenance', '_thread', ('_stop',), 'stop_maintenance'),
    ('lib.fs_keepalive', '_thread', ('_stop_event',), 'stop_fs_keepalive'),
    ('lib.llm_dispatch.health_local', '_thread', ('_stop_event', '_wake_event'),
     'stop_local_health_checker'),
    ('lib.oauth.codex_catalog', '_worker_thread',
     ('_worker_stop', '_refresh_wake'), 'stop_codex_catalog_refresher'),
    ('lib.presence.registry', '_sweeper_thread', ('_sweeper_stop',),
     'stop_sweeper'),
])
def test_worker_stop_contract_releases_only_a_stopped_owner(
        monkeypatch, module_name, thread_name, event_names, stop_name):
    module = importlib.import_module(module_name)

    class Thread:
        alive = True
        joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    thread = Thread()
    monkeypatch.setattr(module, thread_name, thread)
    events = []
    for event_name in event_names:
        event = threading.Event()
        events.append(event)
        monkeypatch.setattr(module, event_name, event)
    if module_name == 'lib.fs_keepalive':
        monkeypatch.setattr(module, '_running', True)
        monkeypatch.setattr(module, '_probe_runtime', None)
    if module_name == 'lib.database_panel.maintenance':
        monkeypatch.setattr(module, '_started', True)
    if module_name == 'lib.oauth.codex_catalog':
        monkeypatch.setattr(module, '_worker_started', True)
    if module_name == 'lib.presence.registry':
        monkeypatch.setattr(module, '_sweeper_started', True)

    assert getattr(module, stop_name)(timeout=0.125) is True
    assert thread.joined == [0.125]
    assert getattr(module, thread_name) is None
    assert all(event.is_set() for event in events)
    if module_name == 'lib.fs_keepalive':
        assert module._running is False
    if module_name == 'lib.database_panel.maintenance':
        assert module._started is False
    if module_name == 'lib.oauth.codex_catalog':
        assert module._worker_started is False
    if module_name == 'lib.presence.registry':
        assert module._sweeper_started is False


def test_visual_enrichment_stop_contract_releases_each_owner(monkeypatch):
    """The shared lane releases every explicitly owned worker on shutdown."""
    from lib.knowledge import enrichment
    from lib.knowledge.enrichment_lane import OwnerFairEnrichmentLane

    started = {7: threading.Event(), 9: threading.Event()}
    released: set[int] = set()
    released_lock = threading.Lock()

    def process(owner_id, stop_event):
        started[owner_id].set()
        assert stop_event.wait(1.0)
        with released_lock:
            released.add(owner_id)
        return False

    lane = OwnerFairEnrichmentLane(
        max_workers=2,
        owner_capacity=2,
        idle_seconds=1.0,
        processor=process,
    )
    monkeypatch.setattr(enrichment, '_enrichment_lane', lane)

    assert lane.schedule(7) is True
    assert lane.schedule(9) is True
    assert started[7].wait(1.0)
    assert started[9].wait(1.0)
    assert enrichment.stop_visual_enrichment(timeout=0.5) is True
    assert released == {7, 9}
    snapshot = enrichment.knowledge_enrichment_snapshot()
    assert snapshot['residentThreads'] == 0
    assert snapshot['retainedOwners'] == 0


def test_scheduler_manager_stop_interrupts_wait_and_releases_owner():
    from lib.scheduler.manager import ScheduledTaskManager

    class Thread:
        alive = True
        joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    manager = ScheduledTaskManager.__new__(ScheduledTaskManager)
    manager._running = True
    manager._stop_event = threading.Event()
    manager._thread = Thread()

    assert manager.stop(timeout=0.25) is True
    assert manager._running is False
    assert manager._stop_event.is_set()
    assert manager._thread is None


def test_timer_join_failure_keeps_storage_and_clients_live(monkeypatch):
    import lib.scheduler.timer as timer_runtime
    import lib.http_transport.client as http_client
    import lib.storage as storage
    closed = []
    def fail():
        raise RuntimeError('timer ownership not settled')
    async def close_http():
        closed.append('http')
    monkeypatch.setattr(timer_runtime, 'shutdown_timer_watchers', fail)
    monkeypatch.setattr(http_client, 'close_http_clients', close_http)
    monkeypatch.setattr(storage, 'stop_storage', lambda **kwargs: closed.append('storage'))
    with pytest.raises(RuntimeError, match='timer ownership not settled'):
        _run_async(shutdown_production_runtime(threading.Event()))
    assert closed == []


def test_browser_join_failure_keeps_dependencies_live(monkeypatch):
    import lib.search.search_runtime as runtime
    import lib.scheduler.timer as timer
    import lib.http_transport.client as http
    import lib.storage as storage
    import lib.tasks_pkg.manager as manager
    closed=[]
    def fail(): raise RuntimeError('browser ownership not settled')
    async def close_http(): closed.append('http')
    monkeypatch.setattr(timer,'shutdown_timer_watchers',lambda:None)
    monkeypatch.setattr(manager,'quiesce_running_tasks',lambda **kwargs:0)
    monkeypatch.setattr(runtime,'shutdown_search_browser_runtime',fail)
    monkeypatch.setattr(http,'close_http_clients',close_http)
    monkeypatch.setattr(storage,'stop_storage',lambda **kwargs:closed.append('storage'))
    with pytest.raises(RuntimeError,match='browser ownership not settled'):
        _run_async(shutdown_production_runtime(threading.Event()))
    assert closed==[]


def test_incremental_shutdown_failure_preserves_dependencies(monkeypatch):
    from lib.translate import incremental
    import lib.scheduler.timer as timers
    import lib.tasks_pkg.manager as manager
    import lib.search.search_runtime as browser
    import lib.storage as storage
    closed = []
    monkeypatch.setattr(timers, 'shutdown_timer_watchers', lambda: None)
    monkeypatch.setattr(manager, 'quiesce_running_tasks', lambda **kwargs: 0)
    def fail():
        raise RuntimeError('incremental work still owned')
    monkeypatch.setattr(incremental, 'shutdown_incremental_translations', fail)
    monkeypatch.setattr(browser, 'shutdown_search_browser_runtime', lambda: closed.append('browser'))
    monkeypatch.setattr(storage, 'stop_storage', lambda **kwargs: closed.append('storage'))
    with pytest.raises(RuntimeError, match='incremental work still owned'):
        _run_async(shutdown_production_runtime(threading.Event()))
    assert not closed


def test_unused_incremental_shutdown_does_not_import_module(monkeypatch):
    import sys
    import builtins
    import lib.scheduler.timer as timers
    import lib.tasks_pkg.manager as manager
    import lib.search.search_runtime as browser
    monkeypatch.delitem(sys.modules, 'lib.translate.incremental', raising=False)
    monkeypatch.setattr(timers, 'shutdown_timer_watchers', lambda: None)
    monkeypatch.setattr(manager, 'quiesce_running_tasks', lambda **kwargs: 0)
    def stop_here():
        raise RuntimeError('browser boundary sentinel')
    monkeypatch.setattr(browser, 'shutdown_search_browser_runtime', stop_here)
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name != 'lib.translate.incremental'
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    with pytest.raises(RuntimeError, match='browser boundary sentinel'):
        _run_async(shutdown_production_runtime(threading.Event()))
    assert 'lib.translate.incremental' not in sys.modules


def _record_runtime_owners(monkeypatch):
    """Stub every owner shutdown_production_runtime retires; returns their call order."""
    import lib.agent_core.admission as admission
    import lib.agent_core.push as push
    import lib.http_transport.client as http_client
    import lib.runtime.runtime_state_store as runtime_state_store
    from lib.durable_worker import runtime as automation_runtime  # noqa: F401 (loaded: stopped)
    import lib.cgroup_guard  # noqa: F401 (loaded: stopped)
    calls = []

    def record(name, result=None):
        def run(*args, **kwargs):
            calls.append(name)
            return result
        return run

    for module_name, attribute, name, result in (
        ('lib.tasks_pkg.manager', 'quiesce_running_tasks', 'quiesce', 0),
        ('lib.scheduler.timer', 'shutdown_timer_watchers', 'timers', None),
        ('lib.translate.incremental', 'shutdown_incremental_translations', 'incremental-translation', None),
        ('lib.search.search_runtime', 'shutdown_search_browser_runtime', 'search-browser', None),
        ('lib.swarm.integration', 'stop_swarm_cleanup_timer', 'swarm-cleanup', None),
        ('lib.swarm.integration', 'stop_swarm_output_cleanup', 'swarm-output-cleanup', None),
        ('lib.netpath', 'stop_prober', 'netpath', None),
        ('lib.server.server_background_services', 'stop_lan_discovery_responder', 'lan-discovery', None),
        ('lib.llm_dispatch.health_local', 'stop_local_health_checker', 'local-health', None),
        ('lib.fs_keepalive', 'stop_fs_keepalive', 'fs-keepalive', None),
        ('lib.presence', 'stop_sweeper', 'presence', None),
        ('lib.integration_control', 'stop_worker', 'integration', None),
        ('lib.cgroup_guard', 'stop_monitor', 'cgroup', None),
        ('lib.durable_worker.runtime', 'stop_workers', 'automation', None),
        ('lib.storage.warmup', 'stop_keep_warm', 'storage-warmup', None),
        ('lib.pricing', 'stop_pricing_refresh', 'pricing-refresh', None),
        ('lib.mcp.startup', 'stop_mcp_auto_connect', 'mcp-startup', None),
        ('lib.mcp.client', 'disconnect_all_bridges', 'mcp', None),
        ('lib.tasks_pkg.event_delivery', 'shutdown_event_delivery', 'event-delivery', True),
        ('lib.key_stats', 'shutdown_key_stats', 'key-stats', None),
        ('lib.usage_tracker', 'flush', 'usage', None),
        ('lib.tasks_pkg.event_log', 'stop_storage_maintenance', 'event-maintenance', None),
        ('lib.tasks_pkg.event_log', 'stop_sidecar_batcher', 'event-batcher', None),
        ('lib.storage', 'stop_storage', 'storage', None),
    ):
        monkeypatch.setattr(importlib.import_module(module_name), attribute, record(name, result))
    monkeypatch.setattr(admission.controller, 'shutdown', record('admission'))
    monkeypatch.setattr(push.hub, 'stop', record('push'))

    async def close_http_clients():
        calls.append('http')

    class Store:
        close = record('runtime')

    monkeypatch.setattr(http_client, 'close_http_clients', close_http_clients)
    monkeypatch.setattr(runtime_state_store, 'get_store', lambda: Store())
    return calls


@pytest.mark.parametrize(('late', 'storage_closes'), [
    (None, False),  # a latch without a typed record: an unknown owner may still write
    ((('browser bridge relay', False),), True),
    ((('bot channel sessions', True),), False),
], ids=['unnamed', 'non-storage', 'storage-writer'])
def test_a_late_background_owner_never_skips_the_drain_and_flushes(monkeypatch, late, storage_closes):
    """Recorded 2026-11-10 04:35 (and 11-09 11:06): a late "bot channel sessions" stop
    raised past the event-delivery drain, the key-stats/usage flushes, the event
    batcher and the storage stop, so the in-memory counters since the last flush
    were lost and the manager retired the host as unclean. A late owner is now a
    typed, logged record; only one whose threads may still write storage keeps
    storage open (and uncertified)."""
    from types import SimpleNamespace
    from lib.server import server_background_services as services
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', '30')
    calls = _record_runtime_owners(monkeypatch)

    def stop_late(app, **options):
        calls.append('route-services')
        if late is not None:
            app.extensions[services.LATE_OWNERS_KEY] = tuple(
                services.LateBackgroundOwner(label, writes_storage=writes) for label, writes in late)
        app.extensions['tofu_registered_background_services'] = True

    monkeypatch.setattr(services, 'stop_registered_background_services', stop_late)
    app = SimpleNamespace(extensions={})
    stopped = threading.Event()
    stopped.set()
    if storage_closes:
        _run_async(shutdown_production_runtime(stopped, app=app))
    else:
        with pytest.raises(RuntimeError) as raised:
            _run_async(shutdown_production_runtime(stopped, app=app))
        assert isinstance(raised.value, services.BackgroundOwnersStillLive)
        assert raised.value.fences_storage
        assert (late[0][0] if late else 'an unnamed owner') in str(raised.value)
    assert calls.index('route-services') < calls.index('event-delivery')
    assert calls[calls.index('event-delivery'):] == [
        'event-delivery', 'push', 'http', 'runtime', 'key-stats', 'usage',
        'event-maintenance', 'event-batcher', *(['storage'] if storage_closes else [])]


@requires_dx
def test_a_late_owner_is_recorded_with_its_storage_ownership(monkeypatch):
    """stop_registered_background_services names each owner left live, typed."""
    import sys
    from types import SimpleNamespace
    from lib.server import server_background_services as services
    import lib.browser.bridge_relay as bridge_relay
    import lib.desktop.bridge_relay as desktop_bridge_relay
    stops = {}

    def owner_stop(name, stopped):
        def stop(timeout):
            stops[name] = timeout
            return stopped
        return stop

    for module_name, stop_name in (
            ('lib.knowledge.enrichment', 'stop_visual_enrichment'),
            ('lib.knowledge.image_ocr_jobs', 'stop_attachment_image_ocr'),
            ('lib.oauth.codex_catalog', 'stop_codex_catalog_refresher'),
            ('lib.scheduler.manager', 'stop_scheduler_worker'),
            ('tofu_meituan.dx.supervisor', 'stop_channel_bridges'),
            ('lib.channels.github.supervisor', 'stop_github_sessions'),
            ('lib.channels.bots.supervisor', 'stop_bot_sessions'),
            ('lib.database_panel.maintenance', 'stop_maintenance')):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, stop_name, owner_stop(stop_name, stop_name != 'stop_bot_sessions'))
    monkeypatch.setattr(bridge_relay, 'stop_bridge_owner', owner_stop('relay', False))
    monkeypatch.setattr(desktop_bridge_relay, 'stop_bridge_owner',
                        owner_stop('desktop relay', False))
    monkeypatch.setitem(sys.modules, 'lib.app.plugin_registry',
                        SimpleNamespace(run_shutdown_hooks=lambda app: 0))
    app = SimpleNamespace(extensions={'tofu_registered_background_services': True,
                                      'tofu_registered_background_services_role': 'all'})
    services.stop_registered_background_services(app, timeout=2.0)
    assert app.extensions[services.LATE_OWNERS_KEY] == (
        services.LateBackgroundOwner('browser bridge relay', writes_storage=False),
        services.LateBackgroundOwner('desktop bridge relay', writes_storage=False),
        services.LateBackgroundOwner('bot channel sessions', writes_storage=True))
    assert app.extensions['tofu_registered_background_services'] is True  # the latch stays
    assert set(stops.values()) == {2.0}


class _HeldPoll:
    """A bot transport whose run thread sits in a wait close() does not end: iLink's getupdates
    hold before 214650e681, or a send in flight, which is never interrupted (CHANNELS.md C12)."""

    def __init__(self):
        self.closed = threading.Event()
        self.answered = threading.Event()

    def run(self, sink):
        assert self.answered.wait(10)

    def send(self, *args, **kwargs):
        return {'ok': True, 'externalId': ''}

    def close(self):
        self.closed.set()


@requires_dx
def test_bot_sessions_are_asked_to_stop_when_the_drain_begins(monkeypatch):
    """Recorded 2026-11-12 15:51: the execution host drained for 58 s before its background
    services stopped. Only then was the 微信 session asked to stop, and its 2 s join met a run
    thread in a wait close() could not end, so the host closed unclean and its successor recovered
    as after a crash. Channel owners are asked to stop when the drain begins and are joined at
    their place, within the same contract."""
    import sys
    from types import SimpleNamespace
    import lib.browser.bridge_relay as bridge_relay
    from lib.channels.bots import supervisor as bots
    from lib.channels.bots.session import BotSession
    from lib.durable_worker import runtime as durable_workers
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', '30')
    calls = _record_runtime_owners(monkeypatch)
    for module_name, stop_name in (
            ('lib.knowledge.enrichment', 'stop_visual_enrichment'),
            ('lib.knowledge.image_ocr_jobs', 'stop_attachment_image_ocr'),
            ('lib.oauth.codex_catalog', 'stop_codex_catalog_refresher'),
            ('lib.scheduler.manager', 'stop_scheduler_worker'),
            ('tofu_meituan.dx.supervisor', 'stop_channel_bridges'),
            ('lib.channels.github.supervisor', 'stop_github_sessions'),
            ('lib.database_panel.maintenance', 'stop_maintenance')):
        monkeypatch.setattr(importlib.import_module(module_name), stop_name, lambda timeout: True)
    monkeypatch.setattr(bridge_relay, 'stop_bridge_owner', lambda timeout: True)
    monkeypatch.setitem(sys.modules, 'lib.app.plugin_registry',
                        SimpleNamespace(run_shutdown_hooks=lambda app: 0))
    for module_name, registry in (('tofu_meituan.dx.supervisor', '_ADAPTERS'),
                                  ('lib.channels.github.supervisor', '_ADAPTERS'),
                                  ('lib.channels.bots.supervisor', '_SESSIONS')):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, registry, {})
        monkeypatch.setattr(module, '_HOSTING', threading.Event())
        monkeypatch.setattr(module, '_STOPPING', threading.Event(), raising=False)
    transport = _HeldPoll()
    session = BotSession(7, 'wechat', store=SimpleNamespace(owner=7, account=lambda channel: {}),
                         transport=transport)
    bots._SESSIONS[(7, 'wechat')] = session

    def stop_durable_workers(*args, **kwargs):
        calls.append('automation')
        # iLink answers its hold while the drain runs; only a session that was already asked to
        # stop leaves the wait then instead of polling again.
        if transport.closed.wait(5):
            transport.answered.set()

    monkeypatch.setattr(durable_workers, 'stop_workers', stop_durable_workers)
    app = SimpleNamespace(extensions={'tofu_registered_background_services': True,
                                      'tofu_registered_background_services_role': 'execution'})
    stopped = threading.Event()
    stopped.set()
    session.start()
    try:
        _run_async(shutdown_production_runtime(stopped, app=app))
    finally:
        transport.answered.set()
        session.stop(2.0)
    assert calls[-1] == 'storage'  # released, so the re-exec gate certifies a clean close
    assert bots._SESSIONS == {}
