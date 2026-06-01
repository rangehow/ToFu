"""Tier 2 tiktoken backend (OpenAI official tokenizer).

Exact for: GPT-4o, GPT-4, GPT-3.5, o1/o3/o4.
Close-enough for: Qwen, MiniMax, Doubao, GLM, Gemini (BPE with similar
vocabularies — typically within ±10 %).

Encoding choice:
  ``o200k_base`` → GPT-4o, GPT-5, o-series
  ``cl100k_base`` → GPT-4 / 3.5 / everything else
"""

from __future__ import annotations

import re
import threading
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


_lock = threading.Lock()
_encoders: dict[str, Any] = {}
_available: Optional[bool] = None


def _get_encoder(name: str):
    """Return a cached tiktoken encoder, or None if tiktoken isn't installed."""
    global _available
    if _available is False:
        return None
    with _lock:
        if name in _encoders:
            return _encoders[name]
        try:
            import tiktoken  # type: ignore
            enc = tiktoken.get_encoding(name)
            _encoders[name] = enc
            _available = True
            return enc
        except ImportError:
            logger.info('[TokenCounter] tiktoken not installed — Tier 2 unavailable')
            _available = False
            return None
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken.get_encoding(%s) failed: %s',
                           name, e)
            return None


def encoding_for_model(model: str) -> str:
    """Pick the best tiktoken encoding for a model id."""
    m = (model or '').lower()
    if 'gpt-4o' in m or 'gpt-5' in m or re.search(r'\bo[134]\b', m):
        return 'o200k_base'
    return 'cl100k_base'


def count_text(text: str, model: str = '') -> int:
    """Exact-ish count for a single text blob (public API)."""
    if not text:
        return 0
    enc = _get_encoder(encoding_for_model(model))
    if enc is None:
        return 0  # caller should fall back to heuristic
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception as e:
        logger.debug('[TokenCounter] tiktoken encode failed: %s', e)
        return 0


class TiktokenCounter(TokenCounter):
    """Local universal tokenizer (OpenAI-exact, rest ±10 %)."""

    name = 'tiktoken'
    # ``confidence`` is refined per-model in ``count()``.
    confidence = 'good'
    needs_network = False

    def supports(self, model: str) -> bool:
        # Always supported as long as tiktoken is installed.
        return _get_encoder('cl100k_base') is not None

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        enc = _get_encoder(encoding_for_model(model))
        if enc is None:
            return None
        try:
            total = 0
            per_msg_overhead = 4  # role token + separators (OpenAI chat format)
            texts = list(iter_message_texts(messages, system, tools))
            if texts:
                # encode_batch is ~2-3× faster than looping
                for ids in enc.encode_batch(texts, disallowed_special=()):
                    total += len(ids)
            total += per_msg_overhead * (len(messages) if messages else 0)
            total += count_images(messages) * _IMAGE_TOKENS_DEFAULT
            total += _STRUCTURAL_OVERHEAD_TOKENS
            return total
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken count failed: %s', e)
            return None


__all__ = ['TiktokenCounter', 'count_text', 'encoding_for_model']
