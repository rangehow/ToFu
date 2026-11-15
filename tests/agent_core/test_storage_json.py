"""The storage-free value encoder is the storage clients' encoder.

Pre-compaction archive sizing also runs where ``lib.storage`` is absent (the
tofu-agent wheel excludes it), so it measures values with
``lib.agent_core.storage_json``. Those sizes mean something only if they are
the bytes the storage clients send: ``lib.storage.protocol`` re-exports the
same functions, and these properties pin byte equality and identical typed
refusals over representative JSON values, plus the additive list size the
archive budget sums.
"""
from __future__ import annotations

from types import MappingProxyType

from hypothesis import given, settings, strategies as st
import pytest

from lib.agent_core import storage_json
from lib.agent_core.storage_errors import StorageError
from lib.storage import protocol

pytestmark = pytest.mark.unit

_INT64 = 2 ** 63


def _json_values(scalars, keys):
    return st.recursive(
        scalars,
        lambda children: (
            st.lists(children, max_size=4)
            | st.tuples(children, children)
            | st.dictionaries(keys, children, max_size=4)
            # Shared caches hand out read-only Mapping views.
            | st.dictionaries(keys, children, max_size=4).map(MappingProxyType)),
        max_leaves=20)


# Values orjson encodes itself: string keys, 64-bit integers, finite floats.
native_values = _json_values(
    st.none() | st.booleans() | st.integers(-_INT64, _INT64 - 1)
    | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=12),
    st.text(max_size=6))
# Every encoder path: the stdlib fallback (integers beyond 64 bits, non-string
# keys) and refusals (non-finite numbers, lone surrogates, fallback failures).
any_values = _json_values(
    st.none() | st.booleans() | st.integers(-(2 ** 80), 2 ** 80) | st.floats()
    | st.text(st.characters(exclude_categories=()), max_size=12),
    st.text(max_size=6) | st.integers(-3, 3))


def _outcome(encode, value):
    try:
        return encode(value)
    except StorageError as refusal:
        return ('refused', refusal.code, refusal.retryable)


def test_storage_protocol_reexports_the_storage_free_encoder():
    for name in ('encode_json_value', 'validate_finite_json_numbers',
                 'materialize_json_mappings'):
        assert getattr(protocol, name) is getattr(storage_json, name)


@settings(max_examples=400, deadline=None)
@given(any_values)
def test_storage_free_encoding_equals_the_storage_client_encoding(value):
    expected = _outcome(protocol.encode_json_value, value)

    observed = _outcome(storage_json.encode_json_value, value)

    assert observed == expected
    if isinstance(expected, bytes):
        assert len(observed) == len(expected)


@settings(max_examples=200, deadline=None)
@given(st.lists(native_values, max_size=6))
def test_archive_budget_sums_member_sizes_to_the_sent_document(messages):
    """The archive budget sizes a snapshot as 2 + its members' sizes + commas."""
    sizes = [len(storage_json.encode_json_value(message)) for message in messages]

    sent = protocol.FrozenPayload({'messages': messages}).encoded['messages']

    assert len(sent) == 2 + sum(sizes) + max(0, len(sizes) - 1)
