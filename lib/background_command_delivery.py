"""Bounded retries for publishing an already-finished command's result.

The caller freezes payload and identity once; queue.enqueue's durable receipt
makes a lost-ACK retry safe. This does not claim recovery before first commit.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from lib.storage.errors import StorageError


def publish_completion(enqueue: Callable[..., dict[str, Any]], *args: Any,
                       **kwargs: Any) -> dict[str, Any]:
    """Retry transient storage failures three times using identical arguments."""
    for attempt in range(3):
        try:
            return enqueue(*args, **kwargs)
        except StorageError as error:
            transient = error.retryable or error.code in {
                'database_busy', 'database_unavailable', 'database_timeout'}
            if not transient or attempt == 2:
                raise
            time.sleep(min(1.0, max(0.1 * (2 ** attempt),
                                   (error.retry_after_ms or 0) / 1000)))
    raise AssertionError('unreachable completion retry state')
