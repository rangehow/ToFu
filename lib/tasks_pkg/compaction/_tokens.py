"""Token estimation + context-limit decision helpers.

Pure functions of ``messages`` + ``task`` — no side effects, no DB, no LLM.
That makes this the cleanest target for unit tests, and the safest module
for the orchestrator to import for "should I trigger force-compact?"
decisions.

Imports nothing from sibling sub-modules except ``_constants``.
"""

import re
import time

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _COMPACTION_RESERVE,
    _cooldown_lock,
    _DEFAULT_CONTEXT_LIMIT,
    _IMAGE_TOKENS_DEFAULT,
    _OUTPUT_RESERVE,
    _SUMMARY_COOLDOWN,
    _SUMMARY_TRIGGER_RATIO,
    _summary_cooldowns,
)

logger = get_logger(__name__)


def _estimate_msg_tokens(msg: dict) -> int:
    """Rough token estimate for a single message (CJK-aware).

    Uses ``lib.token_counter.heuristic.cheap_estimate_text`` which is
    the same CJK-aware heuristic (1 token per CJK char + 1 token per
    ~3.5 other chars) that gates the richer counter backends.

    For a bit-exact authoritative count (via tiktoken / Anthropic
    count_tokens / HF tokenizer), callers should use
    ``_count_tokens_authoritative()`` below.

    Images: fixed estimate per image (NOT base64 length) — the LLM API
    processes images natively and charges ~85-1105 tokens regardless of
    the data-URL size.
    """
    from lib.token_counter.heuristic import cheap_estimate_text

    text_tokens = 0
    image_tokens = 0
    for field in ('content', 'reasoning_content'):
        val = msg.get(field)
        if not val:
            continue
        if isinstance(val, str):
            text_tokens += cheap_estimate_text(val)
        elif isinstance(val, list):
            for block in val:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        text_tokens += cheap_estimate_text(block.get('text', ''))
                    elif block.get('type') == 'image_url':
                        image_tokens += _IMAGE_TOKENS_DEFAULT
    for tc in msg.get('tool_calls', []):
        text_tokens += cheap_estimate_text(tc.get('function', {}).get('arguments', ''))
    return text_tokens + image_tokens


def _estimate_total_tokens(messages: list) -> int:
    """Sum per-message CJK-aware estimates. Fast — never networks."""
    return sum(_estimate_msg_tokens(m) for m in messages)


def _count_tokens_authoritative(messages: list, task: dict | None = None) -> tuple[int, str]:
    """Authoritative token count via ``lib.token_counter.count_tokens``.

    Tries (in order): usage_cache → native count_tokens API →
    exact offline tokenizer (tiktoken / deepseek / HF) → heuristic.

    Returns ``(tokens, method)`` where method is the backend that
    produced the count (``'usage_cache' | 'anthropic_api' | 'tiktoken' | …``).
    """
    try:
        from lib.token_counter import count_tokens as _ct_count_tokens
    except Exception as e:
        logger.debug('[Compact] token_counter unavailable, using heuristic: %s', e)
        return _estimate_total_tokens(messages), 'heuristic_fallback'

    cfg = (task or {}).get('config', {}) or {}
    model = cfg.get('model', '') or ''
    context_limit = _get_context_limit(task)
    conv_id = (task or {}).get('convId', '') or ''

    try:
        result = _ct_count_tokens(
            messages,
            model=model,
            conv_id=conv_id or None,
            context_limit=context_limit,
        )
        return int(result.get('tokens', 0)), str(result.get('method', 'unknown'))
    except Exception as e:
        logger.warning('[Compact] count_tokens call failed, falling back to '
                       'heuristic: %s', e)
        return _estimate_total_tokens(messages), 'heuristic_fallback'


# ── Parse Bedrock / Anthropic "prompt too long" error text ─────────────

_PROMPT_TOO_LONG_RE = re.compile(
    r'(\d[\d,]*)\s*tokens?\s*(?:>|exceeds?|greater than)?\s*(\d[\d,]*)?\s*(?:maximum|limit)?',
    re.IGNORECASE,
)


def _parse_reported_token_count(error_text: str) -> int | None:
    """Extract the N in "prompt is too long: N tokens > M maximum"."""
    if not error_text:
        return None
    try:
        m = _PROMPT_TOO_LONG_RE.search(error_text)
        if not m:
            return None
        n = int(m.group(1).replace(',', ''))
        return n if 0 < n < 50_000_000 else None
    except (ValueError, AttributeError) as _e_audit:
        logger.debug('[_tokens] _parse_reported_token_count caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None


def _human_size(byte_count: int) -> str:
    """Format a byte/char count as a human-readable string."""
    if byte_count < 1024:
        return f'{byte_count}B'
    elif byte_count < 1024 * 1024:
        return f'{byte_count / 1024:.1f}KB'
    else:
        return f'{byte_count / (1024 * 1024):.1f}MB'


# ═══════════════════════════════════════════════════════════════════════════════
#  Context limit helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _get_static_context_limit(task: dict | None = None) -> int:
    """Static (preset) context window for the model in *task*.

    Pure name-based heuristic — does NOT consult auto-learned values.
    Use :func:`_get_context_limit` for the operational lookup that
    layers learned overrides on top.
    """
    if task:
        model_raw = (task.get('config', {}) or {}).get('model', '') or ''
        model = model_raw.lower()

        try:
            from lib.model_info import is_claude_opus_47
            if is_claude_opus_47(model_raw):
                return 1_000_000
        except Exception as e:
            logger.debug('[Compact] is_claude_opus_47 probe failed: %s', e)

        m = re.search(r'(?:claude|anthropic).*sonnet[-_.]?(\d+)[-_.](\d+)', model)
        if m and (int(m.group(1)), int(m.group(2))) >= (4, 6):
            return 1_000_000
        m = re.search(r'(?:claude|anthropic).*opus[-_.]?(\d+)[-_.](\d+)', model)
        if m and (int(m.group(1)), int(m.group(2))) >= (4, 6):
            return 1_000_000

        limits = {
            'claude-opus-4.6':   1_000_000,
            'claude-sonnet-4.6': 1_000_000,
            'claude':   200_000,
            'gpt-4':    128_000,
            'gpt-4o':   128_000,
            'o1':       200_000,
            'o3':       200_000,
            'o4':       200_000,
            'gemini':   1_000_000,
            'qwen':     128_000,
            'deepseek': 128_000,
            'doubao':   128_000,
            'minimax':  1_000_000,
        }
        for key, limit in limits.items():
            if key in model:
                return limit
    return _DEFAULT_CONTEXT_LIMIT


def _get_context_limit(task: dict | None = None) -> int:
    """Look up the model's effective context window in tokens."""
    static_limit = _get_static_context_limit(task)
    if not task:
        return static_limit
    try:
        from lib.context_limits import lookup_learned_context_limit
        provider_id = task.get('provider_id') or ''
        model = (task.get('config', {}) or {}).get('model', '') or ''
        learned = lookup_learned_context_limit(provider_id, model)
        if learned:
            return learned
    except Exception as e:
        logger.debug('[Compact] context_limits lookup failed: %s', e)
    return static_limit


def _should_force_compact(messages: list, task: dict | None = None) -> bool:
    """Decide whether force-compact should fire.

    Returns True when estimated token count exceeds
    ``_SUMMARY_TRIGGER_RATIO`` of usable context.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'

    with _cooldown_lock:
        last = _summary_cooldowns.get(conv_id, 0)
        elapsed = time.time() - last
        if elapsed < _SUMMARY_COOLDOWN:
            logger.debug('[Compact] conv=%s  cooldown active (%.0fs remaining)',
                         log_id, _SUMMARY_COOLDOWN - elapsed)
            return False

    context_limit = _get_context_limit(task)
    usable = context_limit - _OUTPUT_RESERVE - _COMPACTION_RESERVE
    trigger_threshold = int(usable * _SUMMARY_TRIGGER_RATIO)

    total_tokens, method = _count_tokens_authoritative(messages, task)

    logger.debug('[Compact] conv=%s  tokens=%d (via %s)  threshold=%d  '
                 'limit=%d  usable=%d',
                 log_id, total_tokens, method, trigger_threshold,
                 context_limit, usable)

    if total_tokens > trigger_threshold:
        logger.info('[Compact] Force-compact TRIGGERED  conv=%s  '
                    'tokens=%d (via %s) > threshold=%d  '
                    '(limit=%d, usable=%d, ratio=%.0f%%)',
                    log_id, total_tokens, method, trigger_threshold,
                    context_limit, usable,
                    _SUMMARY_TRIGGER_RATIO * 100)
        return True

    return False
