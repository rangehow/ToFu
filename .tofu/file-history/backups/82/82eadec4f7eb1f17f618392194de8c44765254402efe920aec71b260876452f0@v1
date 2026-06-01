"""lib/text_lang.py — Lightweight text-language helpers.

Pure-function port of policy decisions previously embedded in
``static/js/translation.js`` (specifically ``_isAlreadyChinese``).
Centralised so:

* The headless ``/api/v1/agents/translate`` flow and SDK callers
  building CI translation pipelines see the same skip-rule the UI
  applies (avoid translating already-Chinese text into Chinese, which
  wastes an LLM round and can hallucinate an English rewrite).
* The threshold can be tuned in one place.

Public API
----------

  cjk_ratio(text) -> float                 # 0.0 to 1.0
  is_predominantly_chinese(text)           # >= 30% CJK, ≥ 8 non-ws chars
  guess_language(text) -> str              # 'zh' | 'en' | 'mixed' | 'unknown'

Heuristic only — not a substitute for ``langdetect`` / ``cld3``. We
stay dependency-free here on purpose.
"""

from __future__ import annotations

import re

# Unified CJK ideographs + extension-A. Mirrors the JS regex used in
# `_isAlreadyChinese`.
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

# ASCII letters, common Latin-1 letters, and digits. Used for the
# "predominantly English" check.
_LATIN_RE = re.compile(r'[A-Za-z\u00c0-\u024f]')

_WHITESPACE_RE = re.compile(r'\s+')

# Same threshold the JS impl uses.
CHINESE_RATIO_THRESHOLD = 0.30
ENGLISH_RATIO_THRESHOLD = 0.55
MIN_CHARS_FOR_DETECTION = 8


def cjk_ratio(text: str) -> float:
    """Return the fraction of non-whitespace characters that are CJK.

    Returns 0.0 for empty / non-string / too-short input.
    """
    if not isinstance(text, str) or not text:
        return 0.0
    stripped = _WHITESPACE_RE.sub('', text)
    if len(stripped) < MIN_CHARS_FOR_DETECTION:
        return 0.0
    cjk_chars = _CJK_RE.findall(stripped)
    if not cjk_chars:
        return 0.0
    return len(cjk_chars) / len(stripped)


def is_predominantly_chinese(text: str) -> bool:
    """True when ``cjk_ratio(text) >= CHINESE_RATIO_THRESHOLD``.

    Direct port of the JS ``_isAlreadyChinese`` policy.
    """
    return cjk_ratio(text) >= CHINESE_RATIO_THRESHOLD


def latin_ratio(text: str) -> float:
    if not isinstance(text, str) or not text:
        return 0.0
    stripped = _WHITESPACE_RE.sub('', text)
    if len(stripped) < MIN_CHARS_FOR_DETECTION:
        return 0.0
    latin_chars = _LATIN_RE.findall(stripped)
    if not latin_chars:
        return 0.0
    return len(latin_chars) / len(stripped)


def guess_language(text: str) -> str:
    """Coarse language guess for translation routing.

    Returns one of ``'zh'``, ``'en'``, ``'mixed'``, ``'unknown'``.

    * ``'zh'``    — predominantly Chinese.
    * ``'en'``    — predominantly Latin / English.
    * ``'mixed'`` — both ratios are non-trivial (e.g. bilingual reply).
    * ``'unknown'`` — too short or no signal (fewer than 8 non-ws chars).
    """
    cjk = cjk_ratio(text)
    latin = latin_ratio(text)
    if cjk == 0.0 and latin == 0.0:
        return 'unknown'
    if cjk >= CHINESE_RATIO_THRESHOLD and latin >= 0.30:
        return 'mixed'
    if cjk >= CHINESE_RATIO_THRESHOLD:
        return 'zh'
    if latin >= ENGLISH_RATIO_THRESHOLD:
        return 'en'
    return 'unknown'


__all__ = [
    'CHINESE_RATIO_THRESHOLD', 'ENGLISH_RATIO_THRESHOLD',
    'MIN_CHARS_FOR_DETECTION',
    'cjk_ratio', 'latin_ratio', 'is_predominantly_chinese',
    'guess_language',
]
