"""lib/compat/_common.py — shared request-translation helpers for the
OpenAI- and Anthropic-compat surfaces.

``translate_openai_request`` and ``translate_anthropic_request`` independently
mapped the SAME cross-format fields onto the Tofu ``cfg`` — model/preset,
temperature, max_tokens→maxTokens, top_p→topP, tools, tool_choice — and both
ran the identical "explicit tools → disable auto-injected tools" block followed
by ``apply_headless_personal_defaults(cfg)``. That duplication lived here now so
the two translators share one implementation and only carry their format-
specific extras (OpenAI: seed / response_format / reasoning_effort; Anthropic:
system / stop_sequences / metadata / thinking).

Pure functions, no Flask imports.
"""

from __future__ import annotations

import uuid

from lib.log import get_logger

logger = get_logger(__name__)


def short_id(prefix: str = '', n: int = 24) -> str:
    """Return ``<prefix><n hex chars>`` — the id shape both compat surfaces use.

    Replaces the scattered ``f'chatcmpl-{uuid.uuid4().hex[:24]}'`` /
    ``'msg_' + uuid.uuid4().hex[:16]`` / ``'toolu_' + uuid.uuid4().hex[:16]``
    literals with one helper.
    """
    return f'{prefix}{uuid.uuid4().hex[:n]}'


def apply_common_cfg(cfg: dict, body: dict) -> None:
    """Map the fields common to both compat request shapes onto ``cfg``.

    Handles model/preset, temperature, max_tokens→maxTokens, top_p→topP,
    tools, tool_choice. Format-specific fields (seed, response_format, stop
    vs stop_sequences, metadata, thinking, reasoning_effort) are left to the
    caller. Mutates ``cfg`` in place.
    """
    if body.get('model'):
        cfg['model'] = body['model']
        cfg['preset'] = body['model']
    if 'temperature' in body:
        cfg['temperature'] = body['temperature']
    if 'max_tokens' in body:
        cfg['maxTokens'] = body['max_tokens']
    if 'top_p' in body:
        cfg['topP'] = body['top_p']
    if 'tools' in body:
        cfg['tools'] = body['tools']
    if 'tool_choice' in body:
        cfg['toolChoice'] = body['tool_choice']


def apply_tools_and_personal_defaults(cfg: dict, body: dict) -> None:
    """Apply the shared post-mapping cfg policy for a compat request.

    1. When the caller supplied an explicit ``tools`` list, treat it as the
       canonical surface: disable Tofu's auto-injected search/fetch/MCP tools
       so the model isn't surprised with extra capabilities (``setdefault`` so
       an explicit caller cfg still wins).
    2. Fail app-personal capabilities (memory store + preference profile)
       closed on this headless surface regardless of tools — the operator's
       personal state must never ride a compat call. Single source of truth:
       ``lib.agent_core.personal_scope``.
    """
    if 'tools' in body:
        cfg.setdefault('searchMode', 'off')
        cfg.setdefault('fetchEnabled', False)
        cfg.setdefault('mcpEnabled', False)

    from lib.agent_core.personal_scope import apply_headless_personal_defaults
    apply_headless_personal_defaults(cfg)


__all__ = ['short_id', 'apply_common_cfg', 'apply_tools_and_personal_defaults']
