# HOT_PATH
"""Validated request-local token evidence shared across dispatch stages.

The root prompt-admission boundary may count one complete provider prompt once
and carry that positive integer through retries.  The private body key is an
in-process optimization hint, never a provider-wire field; invalid or absent
values always make consumers fall back to their local estimator.
"""

from __future__ import annotations

from typing import Any


ADMITTED_INPUT_TOKENS_KEY = '_admitted_input_tokens'
"""The admitted request size in the serving model's units (serving_units)."""

ADMITTED_LOCAL_TOKENS_KEY = '_admitted_local_tokens'
"""The selected-slot guard's local tiktoken recount of the same body.

Sealed into the request identity beside the serving-unit size, so persisted
rounds keep the provider/local pair the token-unit facts are measured from.
"""

ADMITTED_SERVING_UNITS_KEY = '_admitted_serving_units'
"""Root admission's ``ServingMeasurement`` of the request a body was built from.

It converts a body's local recount only for the model it measured, and its
ledger binds the provider's count of this body to the request it describes.
A rebuilt body carries none. Provider boundaries strip it.
"""

TEXT_COUNT_VIEW_KEY = '_text_count_view'
"""A task body's provider view of its owner's text-count memo.

It proves nothing about the body: every admission still recounts the body's
content and only reuses digest-keyed counts. Provider boundaries strip it.
"""


def validated_admitted_input_tokens(value: Any) -> int | None:
    """Return trusted positive integer evidence, excluding bool-as-int."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


__all__ = [
    'ADMITTED_INPUT_TOKENS_KEY',
    'ADMITTED_LOCAL_TOKENS_KEY',
    'ADMITTED_SERVING_UNITS_KEY',
    'TEXT_COUNT_VIEW_KEY',
    'validated_admitted_input_tokens',
]
