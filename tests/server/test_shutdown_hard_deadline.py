"""Signal shutdown must not hold the listener/instance lock for 300 seconds."""

from __future__ import annotations

import asyncio
import threading

import pytest

pytestmark = pytest.mark.unit


def test_server_reexports_shutdown_policy_from_lifecycle_module():
    from lib.server import server_shutdown
    import server

    assert server.graceful_shutdown_signals is server_shutdown.graceful_shutdown_signals
    assert server.shutdown_hard_deadline_seconds is (
        server_shutdown.shutdown_hard_deadline_seconds
    )
    assert server.http_keep_alive_timeout_seconds is (
        server_shutdown.http_keep_alive_timeout_seconds
    )
    assert server._start_shutdown_hard_deadline is (
        server_shutdown.start_shutdown_hard_deadline
    )
    assert server._request_graceful_shutdown is (
        server_shutdown.request_graceful_shutdown
    )


def test_shutdown_deadline_default_invalid_and_bounds(monkeypatch):
    import server

    monkeypatch.delenv('TOFU_SHUTDOWN_HARD_SECS', raising=False)
    assert server.shutdown_hard_deadline_seconds() == 30.0
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', 'invalid')
    assert server.shutdown_hard_deadline_seconds() == 30.0
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', '1')
    assert server.shutdown_hard_deadline_seconds() == 5.0
    monkeypatch.setenv('TOFU_SHUTDOWN_HARD_SECS', '999')
    assert server.shutdown_hard_deadline_seconds() == 300.0


def test_shutdown_deadline_forces_only_while_shutdown_is_requested():
    import server

    callbacks = []
    exits = []

    class FakeTimer:
        daemon = False

        def __init__(self, delay, callback):
            assert delay == 7.0
            callbacks.append(callback)

        def start(self):
            pass

    requested = threading.Event()
    timer = server._start_shutdown_hard_deadline(
        requested, timeout=7, exit_fn=exits.append,
        timer_factory=FakeTimer)
    assert timer.daemon is True

    callbacks[0]()
    assert exits == []
    requested.set()
    callbacks[0]()
    assert exits == [1]


def test_shutdown_trigger_closes_push_before_transport_drain():
    from lib.server.server_shutdown import ShutdownRequest, wait_for_shutdown_trigger

    requested = ShutdownRequest()
    closed = []

    async def scenario():
        trigger = asyncio.create_task(wait_for_shutdown_trigger(
            requested, close_push_clients=lambda: closed.append(True) or 1))
        await asyncio.sleep(0.06)
        assert closed == []
        requested.set()
        await asyncio.wait_for(trigger, timeout=1)

    asyncio.run(scenario())
    assert closed == [True]


def test_shutdown_trigger_waits_without_timers_and_wakes_on_a_foreign_thread_set():
    """An idle serving loop schedules no polling timer for shutdown (idle wakeups)."""
    from lib.server.server_shutdown import ShutdownRequest, wait_for_shutdown_trigger

    requested = ShutdownRequest()

    async def scenario():
        loop = asyncio.get_running_loop()
        trigger = asyncio.create_task(wait_for_shutdown_trigger(
            requested, close_push_clients=lambda: 0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not trigger.done()
        assert not [handle for handle in loop._scheduled if not handle.cancelled()]
        setter = threading.Thread(target=requested.set)
        started = loop.time()
        setter.start()
        await asyncio.wait_for(trigger, timeout=5)
        setter.join()
        return loop.time() - started

    assert asyncio.run(scenario()) < 1
    # A loop awaiting an already-set request returns without registering.
    asyncio.run(wait_for_shutdown_trigger(requested, close_push_clients=lambda: 0))
    assert requested._waiters == []


def test_shutdown_arms_deadline_before_clean_marker_io():
    import server

    requested = threading.Event()
    calls = []
    timer = object()

    def start_timer(event, *, logger):
        assert event.is_set()
        calls.append('timer')
        return timer

    def mark_clean(reason):
        assert requested.is_set()
        calls.append(('marker', reason))

    result = server._request_graceful_shutdown(
        requested,
        timer_starter=start_timer,
        mark_clean_fn=mark_clean,
    )

    assert result is timer
    assert calls == ['timer', ('marker', 'signal')]


def test_restart_request_is_recorded_as_controlled_shutdown():
    import server

    requested = threading.Event()
    reasons = []
    server._request_graceful_shutdown(
        requested,
        timer_starter=lambda _event, logger: object(),
        mark_clean_fn=reasons.append,
        reason='restart',
    )

    assert requested.is_set()
    assert reasons == ['restart']


def test_http_keep_alive_is_short_configurable_and_bounded(monkeypatch):
    import server

    monkeypatch.delenv('TOFU_HTTP_KEEP_ALIVE_SECS', raising=False)
    assert server.http_keep_alive_timeout_seconds() == 15.0
    monkeypatch.setenv('TOFU_HTTP_KEEP_ALIVE_SECS', 'invalid')
    assert server.http_keep_alive_timeout_seconds() == 15.0
    monkeypatch.setenv('TOFU_HTTP_KEEP_ALIVE_SECS', '0')
    assert server.http_keep_alive_timeout_seconds() == 1.0
    monkeypatch.setenv('TOFU_HTTP_KEEP_ALIVE_SECS', '999')
    assert server.http_keep_alive_timeout_seconds() == 120.0


@pytest.mark.parametrize('lifecycle, exits', [
    ({'status': 'stopped'}, True),
    ({'status': 'stopped', 'shutdown_errors': (('tofu.production.shutdown', 'X'),)}, False),
    ({'status': 'stopping'}, False),
    ({}, False),
])
def test_process_exits_promptly_only_after_a_clean_lifespan(lifecycle, exits):
    from types import SimpleNamespace
    from lib.server.server_shutdown import exit_after_completed_lifespan

    calls = []
    app = SimpleNamespace(extensions={'tofu_lifecycle': lifecycle})
    exit_after_completed_lifespan(
        app, run_exit_handlers=lambda: calls.append('atexit'),
        exit_fn=lambda status: calls.append(('exit', status)))
    assert calls == (['atexit', ('exit', 0)] if exits else [])
