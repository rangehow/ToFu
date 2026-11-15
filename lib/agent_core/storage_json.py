"""Storage JSON value admission and encoding, shared with storage-free runs.

Responsibility
--------------
Admit one storage command value (finite JSON numbers only) and encode it to
the exact bytes both storage clients splice into a storage.v1 frame and a
storage.v2 request. It lives outside ``lib.storage`` because the agent core
measures those bytes before it hands a value to the persistence port (the
pre-compaction archive budget), including in storage-free runs such as the
``tofu-agent`` wheel, which excludes that package. ``lib.storage.protocol``
re-exports these functions for its clients; there is no second encoder.

Entry points
------------
``encode_json_value`` admits and encodes one value. ``json_value_bytes`` is
the encoder alone, for values a caller already admitted (``FrozenPayload``).
``validate_finite_json_numbers`` and ``materialize_json_mappings`` are the
admission and ``Mapping``-view steps.

Dependencies: ``orjson`` and the shared ``lib.agent_core.storage_errors``.
"""
#
# Cross-process contract: the storage sidecar (a separate process) also
# imports this module through lib.storage.protocol; keep dependencies to the
# standard library, orjson and other sidecar-shared modules (enforced by
# scripts/check_architecture.py).

from __future__ import annotations

import json
import math
from typing import Any, Mapping, cast

import orjson

from lib.agent_core.storage_errors import StorageError


def validate_finite_json_numbers(value: Any) -> None:
    """Reject values that JSON cannot represent without silent coercion."""
    pending = [value]
    visited_containers: set[int] = set()
    while pending:
        item = pending.pop()
        item_type = type(item)
        # Exact JSON scalars cannot hide children or a non-finite float.
        # Subclasses retain the generic checks: a registered Mapping may
        # expose values even when it also subclasses a scalar or sequence.
        if item_type is str or item_type is int or item_type is bool or item is None:
            continue
        if isinstance(item, float) and not math.isfinite(item):
            raise StorageError(
                'database_protocol_error',
                'Storage value contains a non-finite JSON number',
            )
        if item_type is float:
            continue
        if item_type is dict:
            is_mapping = True
        elif item_type is list or item_type is tuple:
            is_mapping = False
        elif isinstance(item, Mapping):
            is_mapping = True
        elif isinstance(item, (list, tuple)):
            is_mapping = False
        else:
            continue
        identity = id(item)
        if identity in visited_containers:
            continue
        visited_containers.add(identity)
        pending.extend(
            cast(Mapping[Any, Any], item).values() if is_mapping
            else cast('list[Any] | tuple[Any, ...]', item))


def materialize_json_mappings(value: Any) -> Any:
    """Copy ``Mapping`` views (e.g. ``MappingProxyType`` from shared caches)
    into plain ``dict``; ``orjson`` rejects non-dict mappings. Clean
    containers pass through unchanged (copy-on-write) so full-transcript
    frames only pay the scan."""
    value_type = type(value)
    if value_type is str or value_type is int or value_type is bool or value_type is float or value is None:
        return value
    if isinstance(value, dict):
        rebuilt: dict[Any, Any] | None = None
        for key, item in value.items():
            converted = materialize_json_mappings(item)
            if converted is not item:
                if rebuilt is None:
                    rebuilt = dict(value)
                rebuilt[key] = converted
        return value if rebuilt is None else rebuilt
    if isinstance(value, Mapping):
        return {key: materialize_json_mappings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt_list: list[Any] | None = None
        for index, item in enumerate(value):
            converted = materialize_json_mappings(item)
            if converted is not item:
                if rebuilt_list is None:
                    rebuilt_list = list(value)
                rebuilt_list[index] = converted
        return value if rebuilt_list is None else rebuilt_list
    return value


def json_value_bytes(value: Any) -> bytes:
    """Encode one already admitted value exactly as both storage clients send it."""
    try:
        # Builtin trees (the common case) need no materializing walk.
        return orjson.dumps(value)
    except (TypeError, RecursionError):
        pass
    try:
        return orjson.dumps(materialize_json_mappings(value))
    except (TypeError, RecursionError, orjson.JSONEncodeError):
        pass
    # The storage.v2 encoder's own fallback for values orjson cannot
    # represent exactly (integers beyond 64 bits, non-string keys).
    try:
        return json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise StorageError(
            'database_protocol_error', 'Storage frame is not serializable') from exc


def encode_json_value(value: Any) -> bytes:
    """Admit and encode one command value exactly as both clients send it.

    These are the bytes a ``lib.storage.protocol.FrozenPayload`` splices into
    the storage.v1 frame and the storage.v2 request, so a byte bound measured
    with ``len`` of them measures what the authority receives.
    """
    validate_finite_json_numbers(value)
    return json_value_bytes(value)


__all__ = [
    'encode_json_value', 'json_value_bytes', 'materialize_json_mappings',
    'validate_finite_json_numbers',
]
