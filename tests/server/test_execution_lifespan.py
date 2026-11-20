"""Headless execution reuses application boot and proves joined shutdown."""
import asyncio
import logging
from types import SimpleNamespace
import threading

import pytest

pytestmark = pytest.mark.unit


def test_control_shutdown_failure_still_closes_application():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan
    async def exercise():
        stopped = asyncio.Event()
        calls = []
        async def startup():
            pass
        async def close_controls():
            calls.append('controls')
            raise RuntimeError('control-close-failed')
        async def close_application():
            calls.append('application')
        app = SimpleNamespace(startup=startup, shutdown=close_application, extensions={
            'tofu_boot_runner': SimpleNamespace(status='ready', task=None),
            'tofu_shutdown_requested': threading.Event()})
        with pytest.raises(RuntimeError, match='control-close-failed'):
            await run_execution_lifespan(app, stopped, stopped.set,
                                          before_shutdown=close_controls)
        assert calls == ['controls', 'application']
    asyncio.run(exercise())


@pytest.mark.parametrize('boot_fails', [False, True])
def test_execution_host_awaits_common_boot_and_physical_shutdown(boot_fails):
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        calls = []
        ready, stopped, draining, joined = (asyncio.Event() for _ in range(4))
        shutdown = threading.Event()
        runner = SimpleNamespace(status='starting', task=None, snapshot=lambda: {})

        async def bootstrap():
            runner.status = 'failed' if boot_fails else 'ready'

        async def startup():
            calls.append('startup')
            runner.task = asyncio.create_task(bootstrap())

        async def close():
            assert shutdown.is_set()
            calls.append('drain')
            draining.set()
            await joined.wait()
            calls.append('joined')

        app = SimpleNamespace(startup=startup, shutdown=close, extensions={
            'tofu_boot_runner': runner, 'tofu_shutdown_requested': shutdown})
        host = asyncio.create_task(run_execution_lifespan(app, stopped, ready.set))
        if not boot_fails:
            await ready.wait()
            stopped.set()
        await draining.wait()
        assert not host.done()
        joined.set()
        if boot_fails:
            with pytest.raises(RuntimeError, match='bootstrap'):
                await host
            assert not ready.is_set()
        else:
            await host
        assert calls == ['startup', 'drain', 'joined']

    asyncio.run(exercise())


def test_failed_execution_boot_preserves_the_common_phase_error():
    from quart import Quart
    from lib.production_lifecycle import BootPhase, BootRunner
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        app = Quart(__name__)
        shutdown = threading.Event()
        closed = []

        async def refuse_storage():
            raise RuntimeError('storage cutover refused: lib/business.py')

        runner = BootRunner(app, phase_total=1, inline_phases=(),
            background_phases=(BootPhase(1, 'storage_boundary',
                'Exclusive storage boundary', refuse_storage),),
            stop_event=shutdown, logger=logging.getLogger(__name__),
            boot=lambda *args: None)

        async def startup():
            await runner.run_inline()
            runner.start_background()

        async def close():
            assert shutdown.is_set()
            await runner.stop()
            closed.append(True)

        app.extensions.update(tofu_boot_runner=runner, tofu_shutdown_requested=shutdown)
        host = SimpleNamespace(startup=startup, shutdown=close, extensions=app.extensions)
        with pytest.raises(RuntimeError) as failure:
            await run_execution_lifespan(host, asyncio.Event(),
                lambda: pytest.fail('failed bootstrap published readiness'))

        assert runner.status == 'failed'
        assert runner.snapshot()['error'] in str(failure.value)
        assert 'storage cutover refused: lib/business.py' in str(failure.value)
        assert closed == [True]

    asyncio.run(exercise())


def test_shutdown_before_startup_never_constructs_owned_work():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        stop = asyncio.Event()
        stop.set()
        await run_execution_lifespan(None, stop, lambda: pytest.fail('ready'))

    asyncio.run(exercise())


def test_stop_during_boot_is_owned_and_joined_by_application():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        started, stopped = asyncio.Event(), asyncio.Event()
        shutdown = threading.Event()
        runner = SimpleNamespace(status='starting', task=None)

        async def startup():
            runner.task = asyncio.create_task(asyncio.Event().wait())
            started.set()

        async def close():
            assert shutdown.is_set()
            runner.task.cancel()
            await asyncio.gather(runner.task, return_exceptions=True)

        app = SimpleNamespace(startup=startup, shutdown=close, extensions={
            'tofu_boot_runner': runner, 'tofu_shutdown_requested': shutdown})
        host = asyncio.create_task(run_execution_lifespan(
            app, stopped, lambda: pytest.fail('ready while interrupted')))
        await started.wait()
        stopped.set()
        await host
        assert runner.task.done()

    asyncio.run(exercise())


def test_stop_before_startup_closes_a_real_application():
    """A stop that reaches the host before startup closes the real application.

    Quart creates ``shutdown_event`` only in ``startup()``, so shutting down an
    application constructed but never started raised AttributeError and the
    resident host reported an unclean close instead of running its owners'
    cleanup (seen when a slow boot outlived the readiness window).
    """
    from lib.app.app_factory import create_base_app
    from lib.app.app_lifecycle import add_shutdown_handler
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        app = create_base_app(__name__)
        app.extensions['tofu_shutdown_requested'] = threading.Event()
        closed = []
        add_shutdown_handler(app, lambda: closed.append(True), name='constructed-owner')
        stopped = asyncio.Event()
        stopped.set()
        await run_execution_lifespan(app, stopped, lambda: pytest.fail('ready'))
        assert app.extensions['tofu_shutdown_requested'].is_set()
        assert closed == [True]
        assert app.extensions['tofu_lifecycle']['status'] == 'stopped'

    asyncio.run(exercise())


def test_stop_during_construction_closes_the_constructed_application():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        stopped = asyncio.Event()
        stopped.set()
        shutdown = threading.Event()
        closed = []

        async def startup():
            pytest.fail('startup after owner exit')

        async def close():
            assert shutdown.is_set()
            closed.append(True)

        app = SimpleNamespace(startup=startup, shutdown=close,
            extensions={'tofu_shutdown_requested': shutdown})
        await run_execution_lifespan(app, stopped, lambda: pytest.fail('ready'))
        assert closed == [True]

    asyncio.run(exercise())


@pytest.mark.parametrize('before_boot', [True, False])
def test_source_recovery_witness_cannot_be_created_after_application_start(
        tmp_path, monkeypatch, before_boot):
    import os
    from serverctl_pkg import execution_host
    from serverctl_pkg.execution_plan import ExecutionSourceChanged

    async def exercise():
        reader, writer = os.pipe()
        shutdown = threading.Event()
        closed = []
        record = {'plan': {}, 'serviceId': 'owned-execution'}
        args = SimpleNamespace(manager_fd=reader, manager_pid=os.getpid(),
            manager_start=1, state_file=str(tmp_path / 'execution.json'))
        monkeypatch.setattr(execution_host, 'process_matches', lambda *args: True)

        async def startup():
            raise ExecutionSourceChanged('source changed after startup entered')

        async def close():
            closed.append(True)

        def construct(stop, plan):
            if before_boot:
                raise ExecutionSourceChanged('source changed before startup')
            return SimpleNamespace(startup=startup, shutdown=close,
                extensions={'tofu_shutdown_requested': shutdown})

        try:
            with pytest.raises(ExecutionSourceChanged):
                await execution_host._run_application(
                    args, record, asyncio.Event(), shutdown, construct)
            assert record.get('failureKind') == ('source_changed_before_boot' if before_boot else None)
            assert closed == ([] if before_boot else [True])
        finally:
            os.close(reader)
            os.close(writer)

    asyncio.run(exercise())


@pytest.mark.parametrize('has_pins', [True, False])
def test_execution_control_reports_frozen_source_only(tmp_path, monkeypatch, has_pins):
    import os
    from lib.runtime.pinned_import_source import PinnedImportSource
    from lib.tasks_pkg import command_control_transport
    from serverctl_pkg import execution_host, execution_lifespan

    source = tmp_path / 'app.py'
    source.write_text('VERSION = 1\n')
    fingerprint = PinnedImportSource([str(tmp_path)]).freeze()
    source.write_text('VERSION = 2\n')
    disk = PinnedImportSource([str(tmp_path)]).freeze()
    assert disk != fingerprint

    def forward(method, task_id, **kwargs):
        assert method == 'interrupt'
        return {'interrupted': False}

    gate = SimpleNamespace(apply_command_control=forward, begin_stop=lambda: None, join=lambda: None)
    monkeypatch.setattr(execution_host, '_claim_gate', lambda *a: gate)
    monkeypatch.setattr(execution_host, 'process_matches', lambda *a: True)
    monkeypatch.setattr(execution_host, 'write_record', lambda *a: None)

    class Listener:
        def __init__(self, service_id, apply):
            assert service_id == 'primary'
            self.apply = apply

        async def start(self):
            assert self.apply('interrupt', 'task', user_id=41) == {'interrupted': False}
            if has_pins:
                result = self.apply('retained_source_fingerprint', '', user_id=41)
                assert result == fingerprint
                result.clear()
                assert self.apply('retained_source_fingerprint', '', user_id=41) == fingerprint
            else:
                with pytest.raises(RuntimeError, match='unavailable'):
                    self.apply('retained_source_fingerprint', '', user_id=41)
            return {}

        async def close(self):
            pass

    async def lifespan(app, stopped, ready, *, before_shutdown):
        await ready()
        await before_shutdown()

    monkeypatch.setattr(command_control_transport, 'CommandControlServer', Listener)
    monkeypatch.setattr(execution_lifespan, 'run_execution_lifespan', lifespan)
    extensions = {'tofu_execution_retained_source_fingerprint': fingerprint} if has_pins else {}
    app = SimpleNamespace(extensions=extensions)
    reader, writer = os.pipe()
    try:
        args = SimpleNamespace(manager_fd=reader, manager_pid=os.getpid(), manager_start=1,
                               state_file=str(tmp_path / 'unused.json'))
        record = {'plan': disk, 'serviceId': 'primary'}

        async def exercise():
            await execution_host._run_application(
                args, record, asyncio.Event(), threading.Event(), lambda *a: app)

        asyncio.run(exercise())
        assert record['state'] == 'ready'
    finally:
        os.close(reader)
        os.close(writer)


def test_retained_fingerprint_is_not_a_witness_of_pre_pin_imports(tmp_path, monkeypatch):
    import importlib
    import linecache
    import sys

    import runtime_guards
    from lib.runtime.pinned_import_source import source_identity
    from serverctl_pkg import execution_host, execution_plan

    early_name = 'synthetic_execution_early'
    lazy_name = 'synthetic_execution_lazy'
    early_path = tmp_path / f'{early_name}.py'
    lazy_path = tmp_path / f'{lazy_name}.py'
    early_bytes = b'VERSION = "already imported"\n'
    retained_bytes = b'VERSION = "retained"\n'
    later_bytes = b'VERSION = "new checkout after admission"\n'
    early_path.write_bytes(early_bytes)
    lazy_path.write_bytes(retained_bytes)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    monkeypatch.setattr(runtime_guards, 'load_deployment_configuration',
                        lambda: SimpleNamespace(process_role='execution'))
    app = SimpleNamespace(extensions={})
    monkeypatch.setitem(sys.modules, 'server', SimpleNamespace(
        app=app, _APPLICATION_IMPORT_ROOTS=[str(tmp_path)],
        register_server_runtime_lifecycle=lambda *_args, **_kwargs: None))

    def identity(early, lazy):
        return source_identity([(str(early_path), early), (str(lazy_path), lazy)])

    plan = identity(retained_bytes, retained_bytes)
    monkeypatch.setattr(execution_plan, 'prepare_plan', lambda *_: plan)
    monkeypatch.setattr(execution_plan, 'prepare_launch_policy', lambda *_: {})

    try:
        early = importlib.import_module(early_name)
        assert early.VERSION == 'already imported'
        early_path.write_bytes(retained_bytes)
        assert execution_host.create_application(threading.Event(), plan) is app

        lazy_path.write_bytes(later_bytes)
        assert importlib.import_module(lazy_name).VERSION == 'retained'
        assert importlib.import_module(early_name) is early
        assert early.VERSION == 'already imported'

        dispatch = execution_host._with_source_diagnostic(
            app, lambda *_args, **_kwargs: pytest.fail('forwarded to a shard'))
        result = dispatch('retained_source_fingerprint', '', user_id=41)
        assert result == plan
        assert result != identity(early_bytes, retained_bytes)  # Actually imported bytes.
        assert result != identity(retained_bytes, later_bytes)  # Current checkout bytes.
    finally:
        pins = app.extensions.get('tofu_execution_import_source')
        if pins is not None:
            pins.uninstall()
        sys.modules.pop(early_name, None)
        sys.modules.pop(lazy_name, None)
        linecache.cache.pop(str(lazy_path), None)
