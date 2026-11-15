"""Authoritative token counting — modular backend package.

Centralised entry point for "how many tokens will this request cost?".
Replaces the naïve ``chars/4`` estimator that under-counted CJK-heavy
conversations by 2.5× and caused the ``conv=mo4fr5xeup9ogp`` compaction
failure (see 2026-05-04 post-mortem).

Two unit systems
----------------

``count_tokens`` / ``count_text`` count in a LOCAL tokenizer's units
(tiktoken; HF / DeepSeek / provider count APIs when explicitly selected).
Most serving models tokenize differently, so every admission, compaction and
hard-window decision converts to the SERVING model's units in
:mod:`lib.token_counter.serving_units`. Sources there, per request:

  ===================  =====================================================
  Source               When
  ===================  =====================================================
  ``provider_count``   This owner's previous admitted request to the same
                       serving model was answered: its provider-reported
                       prompt tokens (``usage_cache``) plus the ledger
                       entries that changed since, charged at the family's
                       measured provider/local maximum (removed entries
                       credited at the minimum) and any replayed opaque
                       reasoning the provider reported.
  ``measured_bound``   No usable provider count: local count × the family's
                       measured maximum ratio.
  ``local_unbounded``  The family has no measured fact yet: local count,
                       flagged unbounded (provider count + local change when
                       one exists).
  ===================  =====================================================

The measured ratios, their sample sizes and windows are maintained facts in
``lib/model_info/token_unit_facts.json`` (``scripts/measure_token_units.py``
derives them from persisted request rounds); there is no tuning knob. The
Anthropic/Gemini count APIs are not used for admission: they need the selected
route's credential before route selection and an exact wire translation.

Environment variables:

  TOFU_TOKEN_COUNTER
      Forced local backend. ``auto`` (default), ``api``, ``anthropic_api``,
      ``gemini_api``, ``tiktoken``, ``deepseek``, ``hf``, ``heuristic``.

  TOFU_TOKEN_COUNTER_API_TIMEOUT
      Timeout (sec) for the network tiers. Default 10.

  TOFU_TOKEN_COUNTER_API_THRESHOLD
      Skip network tiers when cheap estimate is below this fraction
      of the model's context limit. Default 0.50.

  TOFU_TOKEN_COUNTER_HF_AUTOFETCH
      When ``1``, allow HF ``AutoTokenizer.from_pretrained()`` to
      download the tokenizer on first use (otherwise requires a
      locally-cached copy). Default off.

Public API (imported from ``lib.token_counter``):

  :func:`count_tokens`   Full-request local count (messages + system + tools).
  :func:`count_text`     Fast single-string count.
  :func:`cached_cheap_estimate_text`
                         Entropy safety estimate with bounded digest reuse.
  :class:`RequestTextCounts`
                         Owner-held per-text count memo for repeated
                         ``count_tokens(..., text_count_memo=...)`` calls;
                         ``memo.view(label)`` counts one projection of the
                         request (:class:`RequestTextCountView`).
  :func:`invalidate`     Drop an owner's provider counts (after compaction).
"""

from __future__ import annotations

from .api import (
    RequestTextCounts,
    RequestTextCountView,
    cached_cheap_estimate_text,
    count_text,
    count_tokens,
    invalidate,
    request_text_count_view,
)
from .base import CountResult

__all__ = [
    'count_tokens', 'count_text', 'cached_cheap_estimate_text',
    'invalidate',
    'CountResult', 'RequestTextCounts', 'RequestTextCountView',
    'request_text_count_view',
]
