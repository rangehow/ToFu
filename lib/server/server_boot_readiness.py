"""Shared boot projection including the existing execution-control dependency.

Entry: application_boot_snapshot. HTTP probes and the boot gate consume the
same verdict. The BootRunner and resident manager retain all recovery ownership;
this read-only projection never starts work, retries, or mutates an Attempt.
"""


# Existing HTTP booting Retry-After cadence; deferred API owners also use
# it while the independently launched execution host is not yet ready.
BOOT_RETRY_AFTER_SECONDS = 1


def application_boot_snapshot(app) -> dict:
    runner = app.extensions.get('tofu_boot_runner')
    snapshot = runner.snapshot() if runner is not None else {'status': 'ready', 'phases': []}
    if snapshot['status'] != 'ready':
        return snapshot
    controls = app.extensions.get('tofu_command_controls')
    readiness = controls.readiness() if controls is not None else {'state': 'ready'}
    if readiness['state'] == 'ready':
        return snapshot
    starting = readiness['state'] == 'starting'
    error = None if starting else (
        f"{readiness['error']}; inspect serverctl status for recovery or restart instructions")
    from lib.startup_phases import (
        EXECUTION_SERVICE_PHASE_KEY, EXECUTION_SERVICE_PHASE_LABEL)

    total = max(snapshot.get('phaseTotal', 0), len(snapshot.get('phases', []))) + 1
    phase = {'index': total, 'key': EXECUTION_SERVICE_PHASE_KEY,
             'label': EXECUTION_SERVICE_PHASE_LABEL,
             'status': 'running' if starting else 'failed', 'duration_s': None, 'error': error}
    return {**snapshot, 'status': 'running' if starting else 'failed', 'phaseTotal': total,
            'phases': [*snapshot.get('phases', []), phase],
            'currentPhase': phase if starting else None,
            'failedPhase': None if starting else phase, 'error': error,
            'retryCount': snapshot.get('retryCount', 0), 'readyAt': None}
