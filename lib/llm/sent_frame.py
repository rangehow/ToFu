"""Exact sent-frame evidence shared by every LLM request transport.

Responsibility: name the bytes a transport actually handed to the socket or
HTTP client. ``AdmittedRequest`` is the approved pre-transport snapshot; this
module hashes the bytes *after* transport encoding (zstd for Codex Responses,
plain JSON for SSE bodies and WebSocket frames), so a cache audit can no
longer compare a pre-transport body and call it byte-identical. This is the
in-process send identity of the provider-bound body; a desktop relay forwards
those bytes as base64 to the agent, so it is not an end-to-end wire proof.

Public API
----------
``SentFrame``
    Frozen identity of one sent frame: the request's semantic and
    provider-prefix identities, protocol, transport, kind, the SHA-256 hex of
    the exact bytes, their length, logical and sent item counts, a bounded
    full-frame/transform reason and the bytes' content encoding.
``sent_frame(request, payload, *, transport, kind='full', reason='',
sent_items=None, content_encoding='')``
    Hash ``payload`` exactly as given (``str`` is UTF-8 encoded).
``full_sent_frame(request, *, transport)``
    Hash ``request.encoded_payload()``: the exact HTTP bytes, including the
    request's content encoding.
``transport_send(body, *, transport, frame=None)``
    Send-boundary helper: return ``(payload, frame)`` for one send. A
    content-encoded body is encoded exactly once, so the hashed bytes are the
    bytes handed to the client and a request is never compressed twice.
``SentFrame.evidence()``
    Bounded mapping for receipts/logs: ``frame_*`` keys plus the request
    identities.

Contract
--------
- ``sha256`` covers exactly ``payload``; nothing is re-serialized here, so a
  frame can only be built from bytes a transport is about to send.
- ``transport`` is one of :data:`FRAME_TRANSPORTS`; ``kind`` is one of
  :data:`FRAME_KINDS`. An unsupported value raises ``ValueError`` before
  evidence can be mislabelled.
- ``logical_items`` is the sealed request's item count for its history field
  (``messages``/``input``); ``sent_items`` is what this frame carried.
  ``sent_frame`` fills ``sent_items`` from ``logical_items`` when the caller
  passes ``None`` (a full request); an incremental frame must pass its own
  count. The receipt treats a ``None`` ``sent_items`` the same way.
- ``reason`` explains why a full or reset frame replaced a continuation. It is
  truncated to :data:`MAX_REASON_CHARS`; the loggable receipt additionally
  keeps only its closed vocabulary.
- ``content_encoding`` is one of :data:`FRAME_CONTENT_ENCODINGS` and names the
  encoding applied to *these* bytes. A plain WebSocket frame is ``''`` even
  when the same request's HTTP body would be zstd-compressed.
- A frame belongs to one request: it copies ``semantic_id`` and
  ``request_prefix_id``, and ``cache_exchange_receipt`` attaches it only when
  the request identities, protocol and transport all match.

WebSocket follow-up
-------------------
``lib/llm/responses_websocket.py`` builds one frame per ``response.create``
message from the exact JSON text written to the socket: a continuation uses
``kind='incremental'`` with ``sent_items=len(delta)``, a chain reset uses
``kind='reset'`` with its bounded reason, and a complete request uses
``'full'``. It passes that frame to
``SSEAccumulator.finalize(sent_frame=...)`` instead of reusing the plan's SSE
frame: socket bytes are not the HTTP body (no content encoding, no ``stream``
field).
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.llm.request import AdmittedRequest
from lib.llm.request_prefix import request_history_field
from lib.utils import sha256_holding_gil

FRAME_TRANSPORTS = ('http', 'sse', 'websocket', 'native')
FRAME_KINDS = ('full', 'incremental', 'reset')
FRAME_CONTENT_ENCODINGS = ('', 'zstd', 'gzip', 'deflate')
# The longest closed reason today is well under this; a longer provider text
# is cut so a frame can never carry an unbounded string into usage records.
MAX_REASON_CHARS = 64
_HEX_DIGITS = frozenset('0123456789abcdef')
# Record consumers (JSON/JS clients) hold counts as exact 53-bit integers.
_MAX_COUNT = 2**53


@dataclass(frozen=True, slots=True)
class SentFrame:
    """Identity of the exact bytes one transport handed to its client."""

    request_prefix_id: str
    semantic_id: str
    protocol: str
    transport: str
    kind: str
    sha256: str
    byte_length: int
    logical_items: int | None
    sent_items: int | None
    reason: str = ''
    content_encoding: str = ''

    def __post_init__(self):
        if self.transport not in FRAME_TRANSPORTS:
            raise ValueError('unsupported sent-frame transport: ' + repr(self.transport))
        if self.kind not in FRAME_KINDS:
            raise ValueError('unsupported sent-frame kind: ' + repr(self.kind))
        if self.content_encoding not in FRAME_CONTENT_ENCODINGS:
            raise ValueError('unsupported sent-frame content encoding: '
                             + repr(self.content_encoding))
        if (not isinstance(self.sha256, str) or len(self.sha256) != 64
                or not set(self.sha256) <= _HEX_DIGITS):
            raise ValueError('sent-frame sha256 must be 64 lowercase hex characters')
        for name, value in (('byte_length', self.byte_length),
                            ('logical_items', self.logical_items),
                            ('sent_items', self.sent_items)):
            if value is not None and (type(value) is not int
                                      or not 0 <= value < _MAX_COUNT):
                raise ValueError(f'sent-frame {name} must be a non-negative '
                                 f'integer below 2**53')
        reason = self.reason
        if not isinstance(reason, str):
            raise TypeError('sent-frame reason must be a string')
        if len(reason) > MAX_REASON_CHARS:
            object.__setattr__(self, 'reason', reason[:MAX_REASON_CHARS])

    def evidence(self) -> dict:
        """Bounded record for logs and receipts; no payload bytes."""
        return {
            'semantic_request_id': self.semantic_id,
            'provider_prefix_id': self.request_prefix_id,
            'protocol': self.protocol,
            'frame_sha256': self.sha256,
            'frame_bytes': self.byte_length,
            'frame_kind': self.kind,
            'frame_reason': self.reason or None,
            'frame_content_encoding': self.content_encoding,
        }


def sent_frame(request, payload, *, transport, kind='full', reason='',
               sent_items=None, content_encoding='') -> SentFrame:
    """Name ``payload`` (``str`` is UTF-8 encoded) as one sent frame."""
    request = _require_admitted(request)
    if isinstance(payload, str):
        data = payload.encode('utf-8')
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        data = bytes(payload)
    else:
        raise TypeError('sent-frame payload must be bytes or str')
    logical_items = _logical_items(request)
    return SentFrame(
        request_prefix_id=request.provider_prefix_id,
        semantic_id=request.semantic_id,
        protocol=request.protocol,
        transport=transport,
        kind=kind,
        sha256=sha256_holding_gil(data).hexdigest(),
        byte_length=len(data),
        logical_items=logical_items,
        sent_items=logical_items if sent_items is None else sent_items,
        reason=reason,
        content_encoding=content_encoding,
    )


def full_sent_frame(request, *, transport) -> SentFrame:
    """Name ``request.encoded_payload()``: the exact HTTP bytes, encoding included."""
    request = _require_admitted(request)
    return sent_frame(request, request.encoded_payload(), transport=transport,
                      kind='full', content_encoding=request.content_encoding)


def transport_send(body, *, transport, frame=None):
    """Return ``(payload, frame)`` for exactly the bytes one send hands over.

    ``payload`` is ``None`` for a legacy non-admitted body (the transport keeps
    its own JSON path). For an admitted body it is the bytes the transport
    must pass unchanged, and ``frame`` names those bytes. A supplied ``frame``
    is reused only for an identity-encoded body: the sealed payload object is
    the sent bytes, so its hash is already exact. A content-encoded body is
    compressed once here and its frame always hashes this call's output.
    """
    if not isinstance(body, AdmittedRequest):
        return None, frame
    payload = body.encoded_payload()
    if frame is None or body.content_encoding:
        frame = sent_frame(body, payload, transport=transport,
                           content_encoding=body.content_encoding)
    return payload, frame


def _require_admitted(request):
    if not isinstance(request, AdmittedRequest):
        raise TypeError('sent frame requires an AdmittedRequest')
    return request


def _logical_items(request):
    sealed = request.sealed_items(request_history_field(request.protocol))
    return None if sealed is None else len(sealed)