"""Content-free cache evidence from a sealed request and its provider outcome."""

from lib.cost import observed_cache_read, reported_responses_input_usage, split_input_tokens
from lib.llm.request import AdmittedRequest
from lib.llm.request_prefix import request_history_field
from lib.llm.sent_frame import (
    FRAME_CONTENT_ENCODINGS,
    FRAME_KINDS,
    FRAME_TRANSPORTS,
    SentFrame,
)

CACHE_EXCHANGE_KEY = '_cache_exchange'
# Bounded full-frame/transform vocabulary the WS chain and request comparison
# produce; a provider- or caller-supplied string never enters a log record.
FULL_REASONS = (
    'no_checkpoint', 'request_fields_changed', 'input_not_list',
    'history_shortened', 'history_changed', 'previous_output_changed',
    'empty_delta', 'non_application_delta', 'provider_reconnect',
)
_HEX_DIGITS = frozenset('0123456789abcdef')


def cache_exchange_receipt(
        request, usage: dict, *, transport: str,
        verified: bool, sent_items: int | None = None, incremental: bool = False,
        full_reason: str = '', reused_connection: bool = False,
        reused_idle_connection: bool = False, frame: SentFrame | None = None,
) -> dict | None:
    """Separate actual frame size from the provider's logical token counts.

    A ``frame`` names the exact bytes this transport sent. When it belongs to
    ``request`` on this ``transport`` its item counts, kind and reason replace
    the caller's projections, so cache audits compare sent bytes, not a
    pre-transport body. A frame for another request or transport is ignored:
    evidence must never fail a completed response, and wrong evidence is
    worse than none.
    """
    if not isinstance(request, AdmittedRequest):
        return None

    frame = _matching_frame(request, transport, frame)
    if frame is not None:
        logical_items = frame.logical_items
        sent_items = frame.sent_items if frame.sent_items is not None else logical_items
        incremental = frame.kind == 'incremental'
        full_reason = frame.reason
    else:
        logical = request.sealed_items(request_history_field(request.protocol))
        logical_items = len(logical) if logical is not None else None
    if request.protocol == 'responses':
        observed = reported_responses_input_usage(usage) or {}
        provider_input = observed.get('input_tokens')
        provider_cached = observed.get('cache_read')
    elif request.protocol == 'anthropic':
        provider_input = usage.get('_wire_anthropic_input_tokens')
        provider_cached = usage.get('_wire_anthropic_cached_tokens')
    else:
        provider_cached = observed_cache_read(usage)
        raw_input = usage.get('prompt_tokens', usage.get('input_tokens'))
        provider_input = None
        if type(raw_input) is int and raw_input >= 0:
            try:
                provider_input = split_input_tokens(usage)[1]
            except (TypeError, ValueError, OverflowError):
                pass
    receipt = {
        'protocol': request.protocol,
        'transport': transport,
        'verified': verified,
        'logical_input_items': logical_items,
        'sent_input_items': logical_items if sent_items is None else sent_items,
        'incremental': incremental,
        'reused_connection': reused_connection,
        'reused_idle_connection': reused_idle_connection,
        'full_reason': full_reason or None,
        'provider_input_tokens': provider_input,
        'provider_cached_tokens': provider_cached,
    }
    if frame is not None:
        receipt['frame_sha256'] = frame.sha256
        receipt['frame_bytes'] = frame.byte_length
        receipt['frame_kind'] = frame.kind
        receipt['frame_reason'] = frame.reason or None
        receipt['frame_content_encoding'] = frame.content_encoding
    return receipt


def _matching_frame(request, transport, frame):
    """``frame`` only when it names this request's bytes on this transport."""
    if not isinstance(frame, SentFrame):
        return None
    if (frame.transport != transport or frame.protocol != request.protocol
            or frame.semantic_id != request.semantic_id
            or frame.request_prefix_id != request.provider_prefix_id):
        return None
    return frame


def loggable_cache_exchange(receipt) -> dict | None:
    """Keep log records bounded even for a custom or legacy usage producer."""
    if not isinstance(receipt, dict):
        return None
    transport = receipt.get('transport')
    protocol = receipt.get('protocol')
    if transport not in FRAME_TRANSPORTS or protocol not in (
            'openai', 'anthropic', 'responses'):
        return None
    result = {'transport': transport, 'protocol': protocol}
    for key in ('verified', 'incremental', 'reused_connection', 'reused_idle_connection'):
        result[key] = receipt.get(key) is True
    for key in ('logical_input_items', 'sent_input_items',
                'provider_input_tokens', 'provider_cached_tokens'):
        value = receipt.get(key)
        result[key] = value if type(value) is int and 0 <= value < 2**53 else None
    reason = receipt.get('full_reason')
    result['full_reason'] = reason if reason in FULL_REASONS else None
    frame_sha256 = receipt.get('frame_sha256')
    result['frame_sha256'] = (frame_sha256 if isinstance(frame_sha256, str)
                              and len(frame_sha256) == 64
                              and set(frame_sha256) <= _HEX_DIGITS else None)
    frame_bytes = receipt.get('frame_bytes')
    result['frame_bytes'] = (frame_bytes if type(frame_bytes) is int
                             and 0 <= frame_bytes < 2**53 else None)
    frame_kind = receipt.get('frame_kind')
    result['frame_kind'] = frame_kind if frame_kind in FRAME_KINDS else None
    frame_reason = receipt.get('frame_reason')
    result['frame_reason'] = (frame_reason if isinstance(frame_reason, str)
                              and frame_reason in FULL_REASONS else None)
    frame_encoding = receipt.get('frame_content_encoding')
    result['frame_content_encoding'] = (
        frame_encoding if frame_encoding in FRAME_CONTENT_ENCODINGS else None)
    return result
