"""Policy port of the Rust lifecycle manager (``packages/tofu-manager``).

The Rust manager owns lifecycle decisions, process identity, the restart
commit point and ``logs/server-manager.log``. Launch-environment composition,
storage-engine retention, known-good rollback, lead audit and log retention are
still owned by Python modules that other phases port (resource policy:
``runtime_guards``; engine identity: ``native_engine``; logging:
``lib.logging_pkg``). This
module answers those questions for one request, so both managers derive them
from the same code instead of a second copy. Exit criterion: each operation is
deleted when its owner moves to Rust.

Protocol: one JSON object on stdin ``{"operation": ..., ...}``; one JSON object
on stdout. Standard library only at import time (``python3 -I`` safe apart from
the project modules it names).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _describe(request: dict) -> dict:
    import runtime_guards
    import server_manager
    from serverctl_pkg import python_bytecode_cache
    return {
        'serverEnvKeys': sorted(server_manager.SERVER_ENV_KEYS),
        'resourceBudgetKeys': sorted(runtime_guards.RESOURCE_BUDGET_ENV_KEYS),
        'resourceBudgetPolicyVersion': runtime_guards.RESOURCE_BUDGET_POLICY_VERSION,
        'bytecodeSweepIntervalSeconds': python_bytecode_cache.SWEEP_INTERVAL_SECONDS,
        'bytecodeSweepMarkerName': python_bytecode_cache.SWEEP_MARKER_NAME,
    }


def _compose(request: dict) -> dict:
    """The worker launch environment (``LifecycleManager._worker_environment``)."""
    import server_manager
    from serverctl_pkg.execution_service import ExecutionService
    from serverctl_pkg.native_storage_service import NativeStorageService
    project = os.path.realpath(str(request['project']))
    server_env = {str(key): str(value) for key, value in (request.get('serverEnv') or {}).items()}
    manager_values = {str(key): str(value) for key, value in (request.get('managerValues') or {}).items()}
    environment = server_manager.managed_worker_environment(project, manager_values, server_env)
    execution = ExecutionService(project, sys.executable)
    execution_enabled = execution.enabled(environment)
    if execution_enabled:
        environment = NativeStorageService(project, sys.executable).shared_environment(environment)
    return {'environment': environment, 'executionEnabled': execution_enabled}


def _retention_refusal(request: dict) -> dict:
    from serverctl_pkg.native_engine import retention_refusal
    return {'refusal': retention_refusal(Path(str(request['project'])), dict(request['plan']))}


def _record_exit(request: dict) -> dict:
    from lib.process_owner import record_exit
    try:
        return {'path': record_exit(request['owner'], str(request['directory']))}
    except ValueError as exc:
        return {'notProved': str(exc)}


def _storage(request: dict):
    from serverctl_pkg.native_storage_service import NativeStorageService
    return NativeStorageService(os.path.realpath(str(request['project'])), sys.executable)


def _retain_known_good(request: dict) -> dict:
    return {'knownGood': _storage(request).retain_known_good()}


def _restore_known_good(request: dict) -> dict:
    return {'action': _storage(request).restore_known_good(dict(request.get('knownGood') or {}))}


def _source_only_change(request: dict) -> dict:
    from serverctl_pkg.execution_plan import source_only_change
    return {'sourceOnly': source_only_change(dict(request['expected']), dict(request['actual']))}


def _lead_audit(request: dict) -> dict:
    from lib.logging_pkg.lead_audit import record
    return {'recorded': record(str(request['kind']), dict(request.get('fields') or {}),
                               logs_dir=str(request['logsDir']))}


def _maintain_logs(request: dict) -> dict:
    """``LifecycleManager._maintain_process_logs`` on the manager's two streams."""
    from lib.logging_pkg.log_policy import stream_backup_count, stream_max_bytes
    from lib.logging_pkg.log_retention import (
        copytruncate_if_oversize, ensure_private_log_directory, ensure_private_log_file,
    )
    logs_dir = Path(str(request['logsDir']))
    ensure_private_log_directory(logs_dir)
    failures = []
    for name, path in (('server_console', logs_dir / 'server-console.log'),
                       ('server_manager', logs_dir / 'server-manager.log')):
        try:
            ensure_private_log_file(path, create=True)
            copytruncate_if_oversize(path, max_bytes=stream_max_bytes(name),
                                     backup_count=stream_backup_count(name))
        except Exception as exc:
            failures.append(f'lifecycle log maintenance failed for stream={name} path={path}: {exc}')
    return {'failures': failures}


def _prepare_worker_runtime(request: dict) -> dict:
    """The interpreter and bytecode prefix of the next worker (``LifecycleManager._spawn``).

    The leases this process takes end with it; the Rust manager re-takes them
    (``tofu-manager`` ``runtime.rs``) and falls back to the source interpreter
    or no prefix when it cannot.
    """
    python = str(request['python'])
    environment = {str(key): str(value) for key, value in (request.get('environment') or {}).items()}
    runtime_status: dict = {'replica': False, 'reason': 'runtime-helper-unavailable', 'python': python}
    worker_python, build_root, build_pythons = python, '', []
    try:
        from serverctl_pkg.runtime_replica import select_worker_runtime
        runtime = select_worker_runtime(python, environment, avoid=str(request.get('avoid') or ''))
        runtime.close_parent_lock()
        worker_python = runtime.python
        runtime_status = {**runtime.status, 'replica': runtime.replica, 'reason': runtime.reason,
                          'python': runtime.python, 'generationPaths': list(runtime.generations)}
        build_root, build_pythons = runtime.build_root, list(runtime.build_pythons)
    except Exception as exc:
        runtime_status['error'] = f'worker runtime replica selection skipped: {exc}'
    bytecode_status: dict = {'selected': False, 'managed': False, 'reason': 'cache-helper-unavailable'}
    prefix = ''
    try:
        from serverctl_pkg.python_bytecode_cache import prepare_server_python_cache
        activation = prepare_server_python_cache(str(request['project']), python, environment)
        activation.close_parent_lock()
        bytecode_status = activation.as_status()
        prefix = activation.pycache_prefix if activation.managed else ''
    except Exception as exc:
        bytecode_status['error'] = f'server bytecode cache setup skipped: {exc}'
    return {'runtime': runtime_status, 'workerPython': worker_python, 'buildRoot': build_root,
            'buildPythons': build_pythons, 'bytecode': bytecode_status, 'pycachePrefix': prefix}


def _validate_bytecode_namespace(request: dict) -> dict:
    """``reacquire_server_python_cache_lease``'s identity check for an adopted worker."""
    import os as _os
    from serverctl_pkg.python_bytecode_cache import reacquire_server_python_cache_lease
    descriptor = reacquire_server_python_cache_lease(
        str(request['project']), str(request['python']), str(request.get('cacheRoot') or ''),
        str(request.get('namespace') or ''))
    if descriptor is not None:
        _os.close(descriptor)
    return {'valid': descriptor is not None}


def _request_runtime_build(request: dict) -> dict:
    from serverctl_pkg.runtime_replica import request_build
    return {'started': request_build([str(item) for item in request.get('pythons') or []],
                                     str(request['root']), launcher=str(request['python']))}


def _request_bytecode_sweep(request: dict) -> dict:
    from serverctl_pkg.python_bytecode_cache import request_bytecode_sweep
    return {'started': request_bytecode_sweep(str(request.get('cacheRoot') or ''),
                                              str(request.get('namespace') or ''),
                                              python=str(request['python']))}


def _reject_runtime_generation(request: dict) -> dict:
    from serverctl_pkg.runtime_replica import reject_generation
    reject_generation(str(request['generation']), str(request.get('reason') or ''))
    return {'rejected': True}


def _precertify(request: dict) -> dict:
    from serverctl_pkg.engine_precertify import start_background
    return {'result': start_background(Path(str(request['project'])), str(request['python']))}


_OPERATIONS = {
    'describe': _describe,
    'retain-known-good': _retain_known_good,
    'restore-known-good': _restore_known_good,
    'source-only-change': _source_only_change,
    'lead-audit': _lead_audit,
    'maintain-logs': _maintain_logs,
    'precertify': _precertify,
    'prepare-worker-runtime': _prepare_worker_runtime,
    'validate-bytecode-namespace': _validate_bytecode_namespace,
    'request-runtime-build': _request_runtime_build,
    'request-bytecode-sweep': _request_bytecode_sweep,
    'reject-runtime-generation': _reject_runtime_generation,
    'compose': _compose,
    'retention-refusal': _retention_refusal,
    'record-exit': _record_exit,
}


def main() -> int:
    request = json.loads(sys.stdin.read() or '{}')
    operation = _OPERATIONS.get(str(request.get('operation')))
    if operation is None:
        json.dump({'error': f'unknown operation {request.get("operation")!r}'}, sys.stdout)
        return 2
    try:
        result = operation(request)
    except Exception as exc:  # the caller reports it as this operation's failure
        json.dump({'error': f'{type(exc).__name__}: {exc}'}, sys.stdout)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
