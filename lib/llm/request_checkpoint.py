"""Process-local exclusive checkout of protocol-owned continuation state."""

from collections.abc import Callable, Mapping
import hashlib
import json
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar('T')


def request_checkpoint_key(
        endpoint: str, headers: Mapping, scope: str, owner: str,
        *, transient_headers: frozenset[str] = frozenset(),
) -> str:
    """Hash route and principal scope without retaining header credentials."""
    stable = sorted((str(name).lower(), str(value))
                    for name, value in headers.items()
                    if str(name).lower() not in transient_headers)
    material = json.dumps([endpoint, stable, scope, owner], ensure_ascii=False)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


class IdleRequestCheckpoints(Generic[T]):
    """Retain settled resources until checkout, replacement, or eviction.

    A transport provides a namespace key and owns compatibility checks.
    Checkout transfers ownership to one caller. Disposal runs outside the lock.
    """

    def __init__(self, capacity: int, dispose: Callable[[T, str], None]):
        if capacity <= 0:
            raise ValueError('checkpoint capacity must be positive')
        self._capacity = capacity
        self._dispose = dispose
        self._checkpoints: dict[str, T] = {}
        self._lock = Lock()

    def park(self, key: str, checkpoint: T) -> None:
        with self._lock:
            displaced = self._checkpoints.pop(key, None)
            evicted = None
            if len(self._checkpoints) >= self._capacity:
                evicted = self._checkpoints.pop(next(iter(self._checkpoints)))
            self._checkpoints[key] = checkpoint
        if displaced is not None:
            self._dispose(displaced, 'checkpoint replaced')
        if evicted is not None:
            self._dispose(evicted, 'idle checkpoint evicted')

    def take(self, key: str) -> T | None:
        with self._lock:
            return self._checkpoints.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            detached, self._checkpoints = self._checkpoints, {}
        for checkpoint in detached.values():
            self._dispose(checkpoint, 'idle checkpoints cleared')
