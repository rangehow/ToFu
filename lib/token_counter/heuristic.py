"""Tier 0 heuristic: CJK-aware char-level estimator.

This backend *always* works — no dependencies, no network. It serves
two purposes:

  1. Final fallback when every other tier fails.
  2. Cheap pre-filter that short-circuits expensive tiers when the
     context is clearly far below the model's limit.

Accuracy model: ``1 token / CJK char`` + ``1 token / 3.5 other chars``.
Off by ±15 % vs. tiktoken on mixed English/code, but crucially never
*under*-counts CJK (the failure mode that bit conv=mo4fr5xeup9ogp).
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import get_logger

from .base import (
    TokenCounter,
    count_images,
    iter_message_texts,
    _IMAGE_TOKENS_DEFAULT,
    _STRUCTURAL_OVERHEAD_TOKENS,
)

logger = get_logger(__name__)


# Range covers BMP CJK Unified Ideographs + CJK punctuation + fullwidth
# ASCII + hangul + katakana/hiragana. Each char ≈ 1 token in real BPE.
_CJK_LO = '\u3000'
_CJK_HI = '\uffef'


def cheap_estimate_text(text: str) -> int:
    """CJK-aware estimate for a single text blob."""
    if not text:
        return 0
    cjk = sum(1 for c in text if _CJK_LO <= c <= _CJK_HI)
    other = len(text) - cjk
    return cjk + (other // 3 + 1 if other else 0)


def cheap_estimate(messages: list, system: Any = None, tools: Any = None) -> int:
    """Cheap estimate for a full request."""
    text_tokens = sum(
        cheap_estimate_text(t)
        for t in iter_message_texts(messages, system, tools)
    )
    image_tokens = count_images(messages) * _IMAGE_TOKENS_DEFAULT
    return text_tokens + image_tokens + _STRUCTURAL_OVERHEAD_TOKENS


class HeuristicCounter(TokenCounter):
    """Final-fallback backend — always returns a number."""

    name = 'heuristic'
    confidence = 'approx'
    needs_network = False

    def supports(self, model: str) -> bool:
        return True  # universal fallback

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        return cheap_estimate(messages, system=system, tools=tools)


__all__ = ['HeuristicCounter', 'cheap_estimate', 'cheap_estimate_text']
