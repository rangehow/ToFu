"""Storage client facade for the shared, backend-neutral failure contract.

Also owns StorageStartRefused, the supervisor's typed deterministic start
refusal, and StorageStartDeferred, a start waiting for the previous engine's
reap; StorageRuntime keeps both out of its crash breaker.
"""

from lib.agent_core.storage_errors import (
    ERROR_CODES,
    TRANSIENT_STORAGE_CODES,
    is_resource_bound_refusal,
    is_transient_storage_error,
    StorageError,
    ProjectionRebaseRequired,
    describe_storage_failure,
    http_status_for_storage_error,
)


class StorageStartDeferred(StorageError):
    """A start that must wait until the project lease is free.

    Every storage child takes the project lease's OS lock before it serves.
    While another process holds it, including a SIGKILLed engine stuck in
    uninterruptible I/O that the kernel has not reaped, a successor can only
    fail on that lease. Waiting is neither crash pressure nor a refusal:
    StorageRuntime keeps its restart worker on the ordinary backoff without
    feeding the breaker, and callers keep the retryable restart-in-flight
    fence (2026-11-11, 19:54 and 23:53 CST: five lease-held starts in 8 s
    latched storage degraded until an explicit restart).
    """

    def __init__(self, holder: str) -> None:
        super().__init__(
            'database_unavailable',
            f'{holder}; the successor starts once the project lease is free',
            retryable=True)
        self.holder = holder


class StorageStartRefused(StorageError):
    """A deterministic start refusal: repeating the same start cannot succeed.

    ``reason`` is a stable machine code and ``remedy`` the operator action.
    Repetition is not crash pressure: StorageRuntime reports it as the
    ``refused`` state and rechecks at a fixed slow cadence instead of feeding
    the restart breaker (2026-10-27: five identical admission refusals inside
    three seconds latched storage for 5 h 23 min).
    """

    def __init__(self, reason: str, detail: str, remedy: str) -> None:
        super().__init__('database_unavailable', f'{detail}; {remedy}', retryable=False)
        self.reason = reason
        self.detail = detail
        self.remedy = remedy


__all__ = [
    'ERROR_CODES', 'StorageError', 'ProjectionRebaseRequired', 'describe_storage_failure',
    'StorageStartDeferred', 'StorageStartRefused', 'TRANSIENT_STORAGE_CODES', 'is_resource_bound_refusal',
    'is_transient_storage_error', 'http_status_for_storage_error',
]
