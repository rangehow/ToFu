"""Shared boot projection including the existing execution-control dependency.

Entry: application_boot_snapshot. HTTP probes and the boot gate consume the
same verdict. The BootRunner and resident manager retain all recovery ownership;
this read-only projection never starts work, retries, or mutates an Attempt.
"""


def application_boot_snapshot(app) -> dict:
    runner = app.extensions.get('tofu_boot_runner')
    snapshot = runner.snapshot() if runner is not None else {'status': 'ready', 'phases': []}
    if snapshot['status'] != 'ready':
        return snapshot
    controls = app.extensions.get('tofu_command_controls')
    error = controls.readiness_error() if controls is not None else ''
    if not error:
        return snapshot
    total = max(snapshot.get('phaseTotal', 0), len(snapshot.get('phases', []))) + 1
    phase = {'index': total, 'key': 'execution_service', 'label': 'Execution service',
             'status': 'failed', 'duration_s': None, 'error': error}
    return {**snapshot, 'status': 'failed', 'phaseTotal': total,
            'phases': [*snapshot.get('phases', []), phase],
            'currentPhase': None, 'failedPhase': phase, 'error': error,
            'retryCount': snapshot.get('retryCount', 0), 'readyAt': None}
