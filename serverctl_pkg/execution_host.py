"""Resident headless application host, bound to its verified lifecycle manager.

This process uses the common application lifespan. It creates no HTTP listener,
task registry or invocation loop. The manager alone holds an inherited pipe's
writer; EOF wakes shutdown on manager exit and manager exec preserves it.

The same module runs the execution shards a primary host starts on demand
(serverctl_pkg/execution_shards.py): ``--report-fd`` selects the shard, which
was bootstrapped from its primary's retained source and is managed by it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import threading

from serverctl_pkg.execution_service import RECORD_FORMAT
from serverctl_pkg.execution_plan import (
    ExecutionSourceChanged, SOURCE_CHANGED_BEFORE_BOOT, SOURCE_FIELDS, require_plan_match,
)
from serverctl_pkg.resident_service import (
    await_published_record, manager_control_matches, process_matches, write_record,
)


def create_application(shutdown_requested, plan):
    from serverctl_pkg.execution_plan import prepare_launch_policy, prepare_plan
    from lib.runtime.pinned_import_source import bootstrapped
    project = Path(os.environ['TOFU_PROJECT_PATH'])
    environment = dict(os.environ)
    retained = bootstrapped()
    if retained is not None:
        return _create_shard_application(shutdown_requested, plan, project, environment, retained)
    require_plan_match(plan, prepare_plan(project, environment), stage='after preparation')
    from runtime_guards import load_deployment_configuration
    if load_deployment_configuration().process_role != 'execution':
        raise RuntimeError('Resident execution requires the execution process role')
    import server
    from lib.runtime.pinned_import_source import PinnedImportSource
    pins = PinnedImportSource(server._APPLICATION_IMPORT_ROOTS)
    pins.install()
    try:
        retained = pins.freeze()
        require_plan_match(plan, {**plan, **retained}, stage='while retaining source')
        # ``import server`` already assembled the module application with the
        # composition server.py serves; a second create_app() would rebuild
        # every blueprint and route rule only to leave the first unused.
        app = server.app
        server.register_server_runtime_lifecycle(app, shutdown_requested=shutdown_requested)
        # Same source owner as server.py/asgi.py. Preserve __file__ paths and
        # lazy optional imports; finish current sweeps before admitting work.
        pins.join()
        # Running Turns is this host's whole work, so its executor is part of
        # the application here, not a lazy import the first Turn after a boot
        # pays for (the API host never imports it).
        import lib.tasks_pkg.orchestrator.api  # noqa: F401
        import lib.llm.stream  # noqa: F401
        # Retained pins equal the plan (checked above), so a checkout that still
        # holds them holds the plan; only a changed checkout is re-hashed.
        stage = 'during application construction'
        if pins.unchanged_since_freeze():
            require_plan_match(plan, {**prepare_launch_policy(project, environment),
                                      **{field: plan.get(field) for field in SOURCE_FIELDS}}, stage=stage)
        else:
            require_plan_match(plan, prepare_plan(project, environment), stage=stage)
        app.extensions['tofu_execution_import_source'] = pins
        return app
    except BaseException:
        pins.join()
        pins.uninstall()
        raise


def _create_shard_application(shutdown_requested, plan, project, environment, retained):
    """A shard runs its primary's generation: the retained bytes and its launch policy."""
    from serverctl_pkg.execution_plan import prepare_launch_policy
    from serverctl_pkg.execution_shards import SHARD_ROLE, image_environment, import_primary_modules
    from runtime_guards import load_deployment_configuration
    environment = image_environment() or environment
    if load_deployment_configuration().process_role != SHARD_ROLE:
        raise RuntimeError('An execution shard requires the execution_shard process role')
    # The role is the only launch difference from the primary's generation.
    generation = {**environment, 'TOFU_PROCESS_ROLE': 'execution'}
    require_plan_match(plan, {**prepare_launch_policy(project, generation), **retained.identity()},
                       stage='shard preparation')
    # A shard forked from the preloaded image already holds both imports.
    import server
    app = server.app
    server.register_server_runtime_lifecycle(app, shutdown_requested=shutdown_requested)
    app.extensions['tofu_execution_import_source'] = retained
    import_primary_modules(retained.loaded_modules)
    return app


def _claim_gate(args, app, record, request_stop, loop):
    """The shard's gate, or the primary's shard pool; None for a test composition."""
    from lib.durable_worker.runtime import CLAIM_GATE_EXTENSION
    from lib.server.server_loop_runtime import _executor_idle_seconds
    if getattr(args, 'report_fd', None) is not None:
        from serverctl_pkg.execution_shards import ShardGate

        def retire():
            try:
                loop.call_soon_threadsafe(request_stop)
            except RuntimeError:
                pass  # The loop already closed: this host is stopping anyway.
        gate = ShardGate(args.report_fd, retire=retire,
                         idle_seconds=_executor_idle_seconds(os.environ, logging.getLogger('server')))
    else:
        source = app.extensions.get('tofu_execution_import_source')
        if source is None:
            return None
        import sys
        from serverctl_pkg.execution_shards import ShardPool
        # The manager's resolved root (ResidentService.project), published in
        # this host's own record: the environment may name it differently or not at all.
        gate = ShardPool(source=source, plan=record['plan'],
                         checkout=Path(record['projectPath']), python=sys.executable,
                         launch_environment=getattr(args, 'launch_environment', None) or dict(os.environ),
                         directory=Path(args.state_file).parent / 'execution-shards',
                         idle_seconds=_executor_idle_seconds(os.environ, logging.getLogger('server')))
    app.extensions[CLAIM_GATE_EXTENSION] = gate
    return gate


async def _run_application(args, record, stopped, shutdown_requested, application_factory):
    from serverctl_pkg.execution_lifespan import run_execution_lifespan
    loop = asyncio.get_running_loop()
    owner_fd = args.manager_fd
    command_controls = None
    gate = None
    shard = getattr(args, 'report_fd', None) is not None

    def request_stop():
        loop.remove_reader(owner_fd)
        if gate is not None and shard:
            gate.close()
        shutdown_requested.set()
        stopped.set()

    async def ready():
        nonlocal command_controls
        from lib.tasks_pkg.command_control_transport import CommandControlServer
        from lib.tasks_pkg.command_controls import apply_command_control
        apply = apply_command_control
        if gate is not None and not shard:
            apply = gate.apply_command_control
        command_controls = CommandControlServer(record['serviceId'], apply)
        record['commandControl'] = await command_controls.start()
        record['state'] = 'ready'
        record['error'] = ''
        write_record(Path(args.state_file), record)
        if gate is not None and shard:
            gate.open()

    async def close_controls():
        if gate is not None and not shard:
            gate.begin_stop()
        if command_controls is not None:
            await command_controls.close()

    try:
        loop.add_reader(owner_fd, request_stop)
        if not process_matches(args.manager_pid, args.manager_start):
            request_stop()
        if stopped.is_set():
            return
        # Imports and app construction may touch the source filesystem. Keep
        # owner-exit notification live; asyncio.run joins this construction.
        try:
            app = await asyncio.to_thread(application_factory, shutdown_requested, record['plan'])
        except ExecutionSourceChanged:
            # This catch ends before startup can admit tasks. A later boot or
            # invocation failure cannot issue the same no-entry witness.
            record['failureKind'] = SOURCE_CHANGED_BEFORE_BOOT
            raise
        # Before startup admits work: the manager that joins this process's
        # exit records it there, so recovery on another host can prove it.
        from lib.process_owner import exit_evidence_dir, process_owner
        record['processOwner'] = process_owner()
        record['exitEvidence'] = exit_evidence_dir()
        write_record(Path(args.state_file), record)
        gate = _claim_gate(args, app, record, request_stop, loop)
        await run_execution_lifespan(app, stopped, ready, before_shutdown=close_controls)
    finally:
        loop.remove_reader(owner_fd)
        if gate is not None and not shard:
            # Shards stop beside this host's own drain; join them before exit.
            await asyncio.to_thread(gate.join)


class SignalStop:
    """Runs ``request_stop`` for SIGTERM/SIGINT on a watcher thread, never in the handler.

    A Python signal handler runs on the main thread between bytecodes, also
    while that thread holds a lock. One that sets a ``threading.Event`` can
    re-enter ``Event.set`` (or interrupt the main thread's own ``wait``) with
    the Event's condition lock already held, and the process deadlocks on
    itself. A stopping shard gets several SIGTERMs in a row
    (``ShardPool.join`` signals twice, then ``ResidentService.stop`` once
    more); one hung that way for good on 2026-11-18, three nested handler
    frames deep in ``Condition.__enter__``, and its primary's drain waited for
    it. The handler only writes a byte to a pipe.
    """

    def __init__(self, request_stop):
        self._request_stop = request_stop
        self._read, self._write = os.pipe()
        os.set_blocking(self._write, False)
        self._thread = threading.Thread(target=self._watch, name='execution-host-signal-stop',
                                        daemon=True)
        self._thread.start()

    def handler(self, *_args):
        try:
            os.write(self._write, b'\0')
        except BlockingIOError:
            pass  # A full pipe already holds an unread stop request.

    def _watch(self):
        while os.read(self._read, 4096):
            self._request_stop()

    def close(self):
        """Call after the handlers are restored: EOF ends the watcher."""
        os.close(self._write)
        self._thread.join()
        os.close(self._read)


def serve(args, *, application_factory=create_application):
    args.report_fd = getattr(args, 'report_fd', None)
    if getattr(args, 'launch_environment', None) is None:
        args.launch_environment = dict(os.environ)
    shutdown_requested = threading.Event()
    stopped = asyncio.Event()
    event_loop = None
    loop_lock = threading.Lock()

    def request_stop():
        shutdown_requested.set()
        with loop_lock:
            if event_loop is None:
                stopped.set()
            else:
                event_loop.call_soon_threadsafe(stopped.set)

    async def run():
        nonlocal event_loop
        with loop_lock:
            event_loop = asyncio.get_running_loop()
        try:
            await _run_application(args, record, stopped, shutdown_requested, application_factory)
        finally:
            with loop_lock:
                event_loop = None

    signal_stop = SignalStop(request_stop)
    previous = {sig: signal.signal(sig, signal_stop.handler)
                for sig in (signal.SIGTERM, signal.SIGINT)}
    record = {}
    try:
        os.fstat(args.lease_fd)
        record_format = RECORD_FORMAT
        if args.report_fd is not None:
            from serverctl_pkg.execution_shards import RECORD_FORMAT as record_format
        record = await_published_record(Path(args.state_file), service_id=args.service_id,
            manager_pid=args.manager_pid, manager_start=args.manager_start,
            record_format=record_format, stopped=shutdown_requested)
        if not record:
            return 1
        if not manager_control_matches(record, args.manager_fd, write=False):
            raise RuntimeError('Execution manager control identity does not match its record')
        asyncio.run(run())
        # The drain finished: a deliberate stop, never a crash the next boot
        # must count toward the restart-storm guard.
        from lib.shutdown_marker import mark_drained
        mark_drained()
        record['state'], record['error'] = 'stopped', ''
        write_record(Path(args.state_file), record)
        return 0
    except BaseException as error:
        if record:
            record['state'] = 'failed'
            record['error'] = f'{type(error).__name__}: {str(error)[:300]}'
            write_record(Path(args.state_file), record)
        logging.exception('resident execution host failed')
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        signal_stop.close()
        os.close(args.lease_fd)
        os.close(args.manager_fd)
        if args.report_fd is not None:
            os.close(args.report_fd)


def main(*, application_factory=create_application):
    # The launch policy shards inherit, before boot materializes anything.
    launch_environment = dict(os.environ)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--plan-output')
    parser.add_argument('--service-id')
    parser.add_argument('--manager-pid', type=int)
    parser.add_argument('--manager-start', type=int)
    parser.add_argument('--manager-fd', type=int)
    parser.add_argument('--lease-fd', type=int)
    parser.add_argument('--state-file')
    parser.add_argument('--report-fd', type=int)
    args = parser.parse_args()
    args.launch_environment = launch_environment
    if args.plan:
        from serverctl_pkg.execution_plan import prepare_plan
        plan = prepare_plan(Path(os.environ.get('TOFU_PROJECT_PATH', os.getcwd())), dict(os.environ))
        if args.plan_output:
            from lib.json_store import write_json_atomic
            write_json_atomic(args.plan_output, plan, mode=0o600)
        else:
            print(json.dumps(plan))
        return 0
    if any(getattr(args, name) is None for name in (
            'service_id', 'manager_pid', 'manager_start', 'manager_fd', 'lease_fd', 'state_file')):
        parser.error('service ownership arguments are required')
    if args.report_fd is not None:
        # Only this shard writes its reports; its own children never inherit them.
        os.set_inheritable(args.report_fd, False)
        os.set_blocking(args.report_fd, False)
    from serverctl_pkg.runtime_replica import hold_process_leases
    hold_process_leases()
    return serve(args, application_factory=application_factory)


if __name__ == '__main__':
    raise SystemExit(main())
