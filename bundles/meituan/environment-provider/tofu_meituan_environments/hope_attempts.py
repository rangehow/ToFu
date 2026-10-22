"""Select one terminal/recovering Worker from sealed, bounded preemption history.

Only explicit restart-safe opt-in permits sequential platform replacements.
No submission, cancellation, credential or storage side effect occurs here.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from tofu_environments.contracts import EnvironmentError

PREEMPTION_DIAGNOSTIC = 'reason: Preempting, message: Pod is evicted, because of preempt'

def select_worker_attempt(workers, spec, *, observed_at=None):
    recovery = spec.get('recovery')
    if recovery is None:
        if len(workers) != 1:
            raise EnvironmentError('hope_execution_repeated', 'Single-attempt job has multiple Worker attempts')
        return workers[0]
    maximum = recovery['max_attempts']
    if len(workers) > maximum:
        raise EnvironmentError('hope_attempt_limit', 'Platform exceeded sealed Worker attempt allowance')
    parsed = []
    pod_identities = set()
    container_identities = set()
    try:
        for worker in workers:
            # These identifiers come from the authenticated platform attempt list.
            pod, container = worker['podName'], worker['containerId']
            if not isinstance(pod,str) or not pod or not isinstance(container,str) or not container:
                raise ValueError('Missing Worker identity')
            if pod in pod_identities or container in container_identities:
                raise ValueError('Duplicate Worker identity')
            pod_identities.add(pod)
            container_identities.add(container)
            started = datetime.strptime(worker['startTime'], '%Y-%m-%d %H:%M:%S')
            finished_text = worker.get('finishedTime')
            finished = datetime.strptime(finished_text, '%Y-%m-%d %H:%M:%S') if finished_text else None
            if finished is not None and finished < started:
                raise ValueError('Reversed attempt time')
            if str(worker['status']).upper() in {'FAILED','SUCCEEDED','SUCCEED','SUCCESS','KILLED','STOPPED','CANCELLED','FINISHED'} and finished is None:
                raise ValueError('Terminal attempt lacks allocation end time')
            parsed.append((started, finished, worker))
        parsed.sort(key=lambda item: item[0])
        for previous, current in zip(parsed, parsed[1:]):
            started, finished, worker = previous
            if finished is None or finished > current[0]:
                raise ValueError('Overlapping or unclosed previous Worker')
            if str(worker['status']).upper() != 'FAILED':
                raise ValueError('Previous Worker did not fail')
            if worker.get('diagnostics') != PREEMPTION_DIAGNOSTIC:
                raise ValueError('Replacement reason is not verified scheduler preemption')
        # MLP attempt times are rendered in the cluster's UTC+08:00 locale.
        # Keep clock injection explicit for deterministic budget verification.
        now = observed_at or datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        allocation_seconds = 0
        for started, finished, worker in parsed:
            end = finished or now
            if end < started:
                raise ValueError('Allocation starts after observation clock')
            allocation_seconds += (end-started).total_seconds()
        if allocation_seconds * spec['resources']['gpu_count'] > spec['limits']['gpu_seconds']:
            raise ValueError('Observed GPU allocation exceeds sealed budget')
    except (KeyError, TypeError, ValueError) as error:
        raise EnvironmentError('hope_preemption_unverified', 'Sequential scheduler-preemption history and bounded allocation required') from error
    return parsed[-1][2]
