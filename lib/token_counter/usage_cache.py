"""Provider-reported prompt counts, bound to the request they measured.

After a provider answers, its usage reports the prompt size in the serving
model's own tokens: the only exact count of that request Tofu ever sees. This
store keeps the latest such count per ``(owner, serving model)`` together with
the content-free ledger of the request it measured
(:class:`~lib.token_counter.tiktoken_counter.RequestLedger`). The owner's next
admission measures its request as that count plus a bounded charge for the
ledger entries that changed since (:mod:`lib.token_counter.serving_units`).

The owner key is ``compaction_owner_key(task)``: the conversation for a root,
the invocation for a child. Content identity is verified on every use, so age
never makes a count unsound and no count expires: an owner returning after an
idle hour keeps its provider-anchored measurement instead of the cold bound.
Retention is bounded by the launch-probed LRU capacity
(``TOFU_USAGE_CACHE_CAPACITY``) and memory-pressure relief
(:func:`clear_usage_cache`); an evicted count falls back to the measured cold
bound. Writers: the task stream settlement
(``lib/tasks_pkg/manager/_stream.py``); invalidation: reactive compaction,
native compaction usage and child teardown.
"""

from __future__ import annotations

import threading
from array import array
from collections import OrderedDict
from typing import Optional

from lib.log import get_logger
from runtime_guards import resolve_resource_budget

from .tiktoken_counter import RequestLedger

logger = get_logger(__name__)

_IDENTITY_BYTES = 32
_MAX_MODEL_CHARS = 160


class ProviderCount:
    """One provider-reported prompt count and the ledger of that request.

    Identities are packed in request order (``_IDENTITY_BYTES`` each) beside
    an array of their local tokens: about 40 bytes per message, no text.
    """

    __slots__ = ('model', 'provider_tokens', 'local_tokens', 'encoding',
                 'identities', 'entry_tokens', 'opaque_replay_tokens',
                 '_index')

    def __init__(self, *, model: str, provider_tokens: int,
                 ledger: RequestLedger, opaque_replay_tokens: int) -> None:
        self.model = model
        self.provider_tokens = provider_tokens
        self.local_tokens = ledger.total
        self.encoding = ledger.encoding
        self.identities = b''.join(identity for identity, _ in ledger.entries)
        self.entry_tokens = array('Q', (tokens for _, tokens in ledger.entries))
        self.opaque_replay_tokens = opaque_replay_tokens
        self._index: dict[bytes, tuple[int, int]] | None = None

    def index(self) -> dict[bytes, tuple[int, int]]:
        """``identity -> (occurrences, local tokens)``; built once, never mutated."""
        index = self._index
        if index is None:
            index = {}
            identities = self.identities
            for position, tokens in enumerate(self.entry_tokens):
                start = position * _IDENTITY_BYTES
                identity = identities[start:start + _IDENTITY_BYTES]
                occurrences, _ = index.get(identity, (0, tokens))
                index[identity] = (occurrences + 1, tokens)
            self._index = index
        return index


_lock = threading.Lock()
_cache: OrderedDict[tuple[str, str], ProviderCount] = OrderedDict()
_USAGE_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_USAGE_CACHE_CAPACITY', maximum=8192)
_cache_metrics = {
    'hits': 0,
    'misses': 0,
    'capacityEvictions': 0,
}


def record_provider_count(owner_key: str, *, model: str, provider_tokens: int,
                          ledger: Optional[RequestLedger],
                          opaque_replay_tokens: int = 0) -> None:
    """Keep ``provider_tokens`` as the measurement of the request ``ledger``
    describes, for ``owner_key``'s next request to ``model``.

    A count without a ledger (a rebuilt fallback or reactive body) cannot be
    bound to content and is not kept. The owner's earlier count for this
    model still measures the request its own ledger describes, so it stays.
    """
    if (not owner_key or not model or len(str(model)) > _MAX_MODEL_CHARS
            or not isinstance(ledger, RequestLedger)
            or type(provider_tokens) is not int or provider_tokens <= 0):
        return
    key = (owner_key, str(model))
    entry = ProviderCount(
        model=str(model), provider_tokens=provider_tokens, ledger=ledger,
        opaque_replay_tokens=max(0, int(opaque_replay_tokens or 0)))
    with _lock:
        _cache.pop(key, None)
        while len(_cache) >= _USAGE_CACHE_CAPACITY:
            _cache.popitem(last=False)
            _cache_metrics['capacityEvictions'] += 1
        _cache[key] = entry
    logger.debug('[TokenCounter][ProviderCount] owner=%s model=%s provider=%d '
                 'local=%d entries=%d opaque_replay=%d', owner_key, model,
                 provider_tokens, entry.local_tokens, len(entry.entry_tokens),
                 entry.opaque_replay_tokens)


def provider_count(owner_key: str, model: str) -> Optional[ProviderCount]:
    """The owner's latest provider count for ``model``, or ``None``."""
    if not owner_key or not model:
        return None
    key = (owner_key, str(model))
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            _cache_metrics['misses'] += 1
            return None
        _cache.move_to_end(key)
        _cache_metrics['hits'] += 1
        return entry


def invalidate(owner_key: str) -> None:
    """Drop every model's count for ``owner_key`` (compaction, teardown)."""
    if not owner_key:
        return
    with _lock:
        for key in [key for key in _cache if key[0] == owner_key]:
            _cache.pop(key, None)


def clear_usage_cache() -> int:
    """Drop reconstructible entries under memory pressure; return the count."""
    with _lock:
        count = len(_cache)
        _cache.clear()
        return count


def usage_cache_snapshot() -> dict[str, int | float]:
    """Return content-free capacity/eviction telemetry."""
    with _lock:
        return {
            **_cache_metrics,
            'entries': len(_cache),
            'capacity': _USAGE_CACHE_CAPACITY,
        }


def _reset_usage_cache_for_tests() -> None:
    with _lock:
        _cache.clear()
        for key in _cache_metrics:
            _cache_metrics[key] = 0


__all__ = [
    'ProviderCount',
    'clear_usage_cache',
    'invalidate',
    'provider_count',
    'record_provider_count',
    'usage_cache_snapshot',
]
