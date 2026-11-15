"""Length-prefixed ``orjson`` framing for ``storage.v1``.

Command-value admission and encoding (finite numbers, ``Mapping`` views, the
exact value bytes) are owned by the storage-free ``lib.agent_core.storage_json``
and re-exported here for the storage clients.
"""

from __future__ import annotations

import re
import socket
import struct
from typing import Any, Callable, Mapping

import orjson

from lib.agent_core import storage_json
from lib.agent_core.storage_json import (
    encode_json_value,
    materialize_json_mappings,
    validate_finite_json_numbers,
)
from lib.storage.errors import StorageError


PROTOCOL_VERSION = 'storage.v1'
# Must fit the largest legitimate conversation document: the production
# authority holds transcripts up to ~46 MiB, and every checkpoint sync ships
# the full document. The historical 8 MiB cap silently rejected those writes
# (import + live sync). 64 MiB leaves headroom without dropping the anti-abuse
# bound (loopback + token-authenticated; rpc slots cap concurrency).
MAX_FRAME_BYTES = 64 * 1024 * 1024
_HEADER = struct.Struct('!I')
_OPERATION = re.compile(r'^[a-z][a-z0-9_.:-]{0,191}$')


def canonical_json(value: Any) -> bytes:
    validate_finite_json_numbers(value)
    return orjson.dumps(materialize_json_mappings(value), option=orjson.OPT_SORT_KEYS)


class FrozenPayload(dict):
    """A command payload whose container values were admitted and encoded once.

    Every replay of one receipt must send the same bytes. Freezing encodes each
    dict/list value here, so a retry splices that JSON verbatim instead of
    copying, re-walking and re-encoding the caller's graph. The mapping view is
    the caller's payload; storage clients send :meth:`wire_body`.

    ``encoded`` adopts values the caller already encoded with
    :func:`encode_json_value` (for example to measure them first); they are
    neither walked nor encoded again.
    """

    __slots__ = ('encoded',)

    def __init__(self, payload: Mapping[str, Any],
                 encoded: Mapping[str, bytes] | None = None):
        super().__init__(payload)
        adopted = encoded or {}
        validate_finite_json_numbers(
            {key: value for key, value in self.items() if key not in adopted})
        self.encoded = {
            key: adopted[key] if key in adopted else storage_json.json_value_bytes(value)
            for key, value in self.items()
            if key in adopted or isinstance(value, (Mapping, list, tuple))}

    def wire_body(self) -> dict[str, Any]:
        body = dict(self)
        for key, encoded in self.encoded.items():
            body[key] = orjson.Fragment(encoded)
        return body


def wire_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The mutable request body a client encodes for one attempt."""
    if isinstance(payload, FrozenPayload):
        return payload.wire_body()
    return dict(payload or {})


def validate_operation(operation: Any) -> str:
    value = str(operation or '')
    if not _OPERATION.fullmatch(value):
        raise StorageError(
            'database_protocol_error', 'Invalid storage operation name')
    return value


def encode_frame(message: Mapping[str, Any]) -> bytes:
    validate_finite_json_numbers(message)
    try:
        body = orjson.dumps(materialize_json_mappings(message))
    except (TypeError, RecursionError, orjson.JSONEncodeError) as exc:
        raise StorageError(
            'database_protocol_error', 'Storage frame is not serializable') from exc
    if not body or len(body) > MAX_FRAME_BYTES:
        raise StorageError(
            'database_protocol_error', 'Storage frame exceeds the size limit')
    return _HEADER.pack(len(body)) + body


def _recv_exact(sock: socket.socket, size: int) -> bytearray:
    # Allocate the declared frame once. The previous chunk-list + join path
    # retained both copies at the join boundary (92 MiB for a fragmented
    # 46 MiB production-sized frame) and copied the entire payload again.
    buffer = bytearray(size)
    view = memoryview(buffer)
    offset = 0
    try:
        while offset < size:
            received = sock.recv_into(view[offset:], size - offset)
            if not received:
                # EOF before a complete frame means the PEER went away — an
                # overload close, a crash, or a supervised restart — which is
                # transport unavailability, not a framing violation.  The
                # previous 'database_protocol_error' classification was
                # non-retryable, so a transient blip killed idempotent reads
                # that the client retry loop exists to absorb (2026-08-19: 181
                # SSE streams died on a sidecar capacity/restart window).
                raise StorageError(
                    'database_unavailable',
                    'Storage connection closed mid-frame', True, 100)
            offset += received
    finally:
        view.release()
    return buffer


def recv_frame(
    sock: socket.socket,
    *,
    before_payload: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    raw_size = _recv_exact(sock, _HEADER.size)
    (size,) = _HEADER.unpack(raw_size)
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise StorageError(
            'database_protocol_error', 'Invalid storage frame length')
    reservation = before_payload(size) if before_payload is not None else None
    from tofu_db._frame_body import received_frame_body
    try:
        with received_frame_body(lambda count: _recv_exact(sock, count), size, reservation) as body:
            with memoryview(body) as view:
                value = orjson.loads(view)
    except orjson.JSONDecodeError as exc:
        raise StorageError(
            'database_protocol_error', 'Invalid storage frame JSON') from exc
    if not isinstance(value, dict):
        raise StorageError(
            'database_protocol_error', 'Storage frame must be an object')
    return value


def send_frame(sock: socket.socket, message: Mapping[str, Any], *,
               before_payload: Callable[[int], Any] | None = None) -> int:
    frame = encode_frame(message)
    if before_payload is not None:
        before_payload(len(frame) - _HEADER.size)
    sock.sendall(frame)
    return len(frame)


__all__ = [
    'FrozenPayload', 'MAX_FRAME_BYTES', 'PROTOCOL_VERSION', 'canonical_json',
    'encode_frame', 'encode_json_value', 'recv_frame', 'send_frame',
    'validate_finite_json_numbers',
    'validate_operation', 'wire_payload',
]
