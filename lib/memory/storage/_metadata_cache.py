"""Bounded freshness-aware cache for parsed Memory frontmatter only.

The cache never owns Markdown bodies, authorization, package provenance, or
eligibility. Callers rebuild those request-sensitive projections after each
lookup. Entries are reconstructible and evicted by both identity count and an
estimated Python-residency byte envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections import OrderedDict
from copy import deepcopy
import json
import threading
import time
from types import MappingProxyType


MemoryFileFingerprint = tuple[int, int, int, int, int]
MEMORY_METADATA_FINGERPRINT_SETTLE_NS = 2_100_000_000


def _freeze_metadata_value(value, active_ids=None):
    """Copy JSON-like metadata into recursively immutable containers."""
    if active_ids is None:
        active_ids = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError('cyclic metadata mapping')
        active_ids.add(identity)
        try:
            return MappingProxyType({
                key: _freeze_metadata_value(item, active_ids)
                for key, item in value.items()
            })
        finally:
            active_ids.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError('cyclic metadata sequence')
        active_ids.add(identity)
        try:
            return tuple(
                _freeze_metadata_value(item, active_ids)
                for item in value
            )
        finally:
            active_ids.remove(identity)
    return deepcopy(value)


def _thaw_metadata_value(value):
    """Return a caller-owned mutable copy of one frozen metadata value."""
    if isinstance(value, Mapping):
        return {
            key: _thaw_metadata_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_metadata_value(item) for item in value]
    return deepcopy(value)


class MemoryMetadataCache:
    """Thread-safe LRU keyed by file identity and exact stat fingerprint."""

    def __init__(
        self,
        *,
        max_entries: int,
        max_bytes: int,
        fingerprint_settle_ns: int = MEMORY_METADATA_FINGERPRINT_SETTLE_NS,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_bytes = max(1, int(max_bytes))
        self._fingerprint_settle_ns = max(0, int(fingerprint_settle_ns))
        self._clock_ns = time.time_ns
        self._entries: OrderedDict[
            str, tuple[MemoryFileFingerprint, Mapping, int, str]
        ] = OrderedDict()
        self._retained_bytes = 0
        self._lock = threading.Lock()
        self._metrics = {
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'stale': 0,
            'unstable': 0,
            'oversized': 0,
            'unfreezable': 0,
        }

    @staticmethod
    def _weight(identity: str, metadata: dict) -> int:
        encoded = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
            default=str,
        ).encode('utf-8')
        # Dict/list/string object overhead materially exceeds wire JSON. Four
        # times encoded bytes plus path/container overhead is a conservative
        # deterministic accounting unit, not a claim about allocator internals.
        return 256 + len(identity.encode('utf-8')) * 2 + max(2, len(encoded)) * 4

    def _lookup_frozen(
        self,
        identity: str,
        fingerprint: MemoryFileFingerprint,
    ) -> tuple[bool, Mapping, str]:
        with self._lock:
            entry = self._entries.get(identity)
            if entry is None:
                self._metrics['misses'] += 1
                return False, {}, identity
            cached_fingerprint, metadata, weight, retained_identity = entry
            if cached_fingerprint != fingerprint:
                self._entries.pop(identity, None)
                self._retained_bytes -= weight
                self._metrics['stale'] += 1
                self._metrics['misses'] += 1
                return False, {}, identity
            # Several valid filesystems expose nanosecond fields backed by a
            # much coarser write clock (1 ms here; FAT can be 2 s). A same-size
            # in-place edit inside that tick can therefore preserve all five
            # stat fields. Do not trust a matching cache entry until its newest
            # write/change timestamp has aged past the conservative window.
            newest_revision_ns = max(fingerprint[3], fingerprint[4])
            if (self._fingerprint_settle_ns
                    and newest_revision_ns >= (
                        self._clock_ns() - self._fingerprint_settle_ns)):
                self._metrics['unstable'] += 1
                self._metrics['misses'] += 1
                return False, {}, identity
            self._entries.move_to_end(identity)
            self._metrics['hits'] += 1
        return True, metadata, retained_identity

    def lookup(
        self,
        identity: str,
        fingerprint: MemoryFileFingerprint,
    ) -> tuple[bool, dict]:
        """Return a mutable caller-owned metadata copy on cache hit."""
        cached, metadata, _identity = self._lookup_frozen(identity, fingerprint)
        if not cached:
            return False, {}
        return True, _thaw_metadata_value(metadata)

    def lookup_readonly(
        self,
        identity: str,
        fingerprint: MemoryFileFingerprint,
    ) -> tuple[bool, Mapping, str]:
        """Return the recursively immutable cached view without copying.

        The third item is the identity string this cache retains for a hit
        (``identity`` itself otherwise), so a caller's record can share that
        object instead of holding an equal copy per listing.
        """
        return self._lookup_frozen(identity, fingerprint)

    def clock_ns(self) -> int:
        """The wall clock the settle window is measured against."""
        return self._clock_ns()

    def store(
        self,
        identity: str,
        fingerprint: MemoryFileFingerprint,
        metadata: dict,
        *,
        read_started_ns: int | None = None,
    ) -> bool:
        """Retain ``metadata`` read from a file with ``fingerprint``.

        ``read_started_ns`` (``clock_ns()`` before the read) refuses a read
        that began inside the fingerprint's settle window: a same-size edit in
        the same timestamp tick could follow it without changing the
        fingerprint, and a later lookup would then return the older text.
        """
        if (read_started_ns is not None and self._fingerprint_settle_ns
                and read_started_ns - max(fingerprint[3], fingerprint[4])
                < self._fingerprint_settle_ns):
            with self._lock:
                previous = self._entries.pop(identity, None)
                if previous is not None:
                    self._retained_bytes -= previous[2]
                self._metrics['unstable'] += 1
            return False
        try:
            weight = self._weight(identity, metadata)
            retained = _freeze_metadata_value(metadata)
        except (TypeError, ValueError, RecursionError):
            # Parsed metadata is expected to be JSON-like and acyclic. A
            # malformed hand-authored/YAML object remains readable for this
            # request but is never retained in the reconstructible cache.
            with self._lock:
                self._metrics['unfreezable'] += 1
            return False
        with self._lock:
            previous = self._entries.pop(identity, None)
            if previous is not None:
                self._retained_bytes -= previous[2]
            if weight > self._max_bytes:
                self._metrics['oversized'] += 1
                return False
            while self._entries and (
                len(self._entries) >= self._max_entries
                or self._retained_bytes + weight > self._max_bytes
            ):
                _key, (_fingerprint, _metadata, evicted_weight, _identity) = (
                    self._entries.popitem(last=False))
                self._retained_bytes -= evicted_weight
                self._metrics['evictions'] += 1
            self._entries[identity] = (fingerprint, retained, weight, identity)
            self._retained_bytes += weight
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._retained_bytes = 0
            for key in self._metrics:
                self._metrics[key] = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                'entries': len(self._entries),
                'retainedBytes': self._retained_bytes,
                'maxEntries': self._max_entries,
                'maxBytes': self._max_bytes,
                **self._metrics,
            }


__all__ = [
    'MEMORY_METADATA_FINGERPRINT_SETTLE_NS',
    'MemoryFileFingerprint',
    'MemoryMetadataCache',
]
