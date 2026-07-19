"""lib/tasks_pkg/chat_mode.py — Canonical three-tier chat-mode derivation.

Tofu's toolbar collapses to ONE user-facing dial with three tiers:

    ┌─────────────────────────────────────────────────────────────────┐
    │  air     — lean, cheapest.  Web search + fetch + read only.       │
    │            No code-exec / memory / todo / scheduler / swarm.       │
    │            Goal: cleaner than ChatGPT web, far fewer tool tokens.  │
    │  pro      — the everyday all-rounder (the old default, now with    │
    │            code execution ON).  Full default tool set.             │
    │  studio   — a project/repo is attached; the project tool family    │
    │            (list_dir/grep/write/apply_diff/run_command/…) replaces │
    │            the standalone code_exec tool, plus coordination tools. │
    └─────────────────────────────────────────────────────────────────┘

Single source of truth
-----------------------
``chatMode`` is the ONLY new wire field (camelCase, like every other cfg key).
This module is the ONE place that expands it into the pre-existing atomic
flags (``searchMode`` / ``codeExecEnabled`` / ``memoryEnabled`` / …).  The
frontend mirrors :func:`chat_mode_defaults` verbatim in
``static/js/main/main_toolbar_ui.js`` (``_CHAT_MODE_DEFAULTS``); a parity test
(``tests/test_chat_mode_parity.py``) asserts the two tables are byte-equal so
the two ends can never silently drift.

Authority rules (mirrors ``lib/agent_core/profiles.apply_profile``)
-------------------------------------------------------------------
* **Explicit ``chatMode`` is authoritative.**  When the request carries a
  valid ``cfg['chatMode']``, the derived flags OVERRIDE the atomic flags —
  the high-level intent wins, so the UI dial and the resolved tool set can
  never disagree.
* **Absent ``chatMode`` ⇒ byte-identical legacy behaviour.**  Headless callers
  and old clients that send atomic flags without a ``chatMode`` are left
  completely untouched (no override, ``lean`` stays False).  ``studio`` is
  never inferred onto them — a project is still gated purely on
  ``projectPath`` downstream.
* **``studio`` ⟺ a project is attached.**  ``chatMode='studio'`` sets the
  everyday defaults; the project TOOLS still switch on only when
  ``projectPath`` is non-empty (resolved downstream).  This keeps the
  "click Studio → pick a project → only THEN in studio" flow honest.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'CHAT_MODES',
    'DEFAULT_CHAT_MODE',
    'normalize_chat_mode',
    'chat_mode_defaults',
    'is_lean_mode',
    'apply_chat_mode',
]

CHAT_MODES: tuple[str, ...] = ('air', 'pro', 'studio')
DEFAULT_CHAT_MODE = 'pro'


def normalize_chat_mode(cfg: dict | None) -> str | None:
    """Return the EXPLICIT chat mode from *cfg*, or ``None`` if absent/invalid.

    ``None`` (not a fallback string) is deliberate: callers must distinguish
    "the request declared a tier" (→ authoritative override) from "no tier was
    declared" (→ leave the legacy atomic flags untouched).  A malformed value
    is treated as absent.
    """
    if not cfg or not isinstance(cfg, dict):
        return None
    mode = cfg.get('chatMode')
    if isinstance(mode, str) and mode.strip().lower() in CHAT_MODES:
        return mode.strip().lower()
    if mode:
        logger.debug('[ChatMode] ignoring unknown chatMode=%r', mode)
    return None


def chat_mode_defaults(mode: str) -> dict[str, Any]:
    """The atomic-flag table for a tier (camelCase wire keys).

    THIS is the single source of truth mirrored verbatim by the frontend
    ``_CHAT_MODE_DEFAULTS`` map.  Keep the two in lock-step — the parity test
    guards it.  Only the keys a tier actually pins are listed; anything absent
    is left to the caller's explicit value / existing default.

    Note ``browserEnabled`` / ``desktopEnabled`` / ``imageGenEnabled`` /
    ``humanGuidanceEnabled`` / ``autoTranslate`` are intentionally NOT pinned
    here — they are orthogonal "extras" the user toggles independently of the
    tier (they depend on physical connections / model availability), so a tier
    switch must not clobber them.
    """
    if mode == 'air':
        return {
            'searchMode': 'multi',
            'fetchEnabled': True,
            'codeExecEnabled': False,
            'memoryEnabled': False,
            'swarmEnabled': False,
        }
    if mode == 'studio':
        # A project is attached; run_command (in PROJECT_TOOLS) supersedes the
        # standalone code_exec tool, so codeExecEnabled is intentionally left
        # alone (downstream _build_project_or_code_exec ignores it in project
        # mode). Everyday capabilities on.
        return {
            'searchMode': 'multi',
            'fetchEnabled': True,
            'memoryEnabled': True,
        }
    # pro — the everyday all-rounder; code execution ON by default.
    return {
        'searchMode': 'multi',
        'fetchEnabled': True,
        'codeExecEnabled': True,
        'memoryEnabled': True,
    }


def is_lean_mode(mode: str | None) -> bool:
    """True only for the ``air`` tier — the backend-authoritative lean gate.

    Consumed by the tool registry (``_build_memory`` / ``_build_todo`` /
    ``_build_scheduler`` skip themselves when lean) to drop the ~11 always-on
    default tools that would otherwise attach on ``has_base_tools`` and make
    "cleaner than ChatGPT" unreachable through flags alone.
    """
    return mode == 'air'


def apply_chat_mode(cfg: dict | None) -> dict:
    """Return a NEW cfg with the tier's atomic flags applied when declared.

    ``effective = {**cfg, **chat_mode_defaults(mode)}`` when a valid
    ``chatMode`` is present — the derived flags OVERRIDE, because the tier is
    the higher-level intent.  Absent/invalid ``chatMode`` ⇒ a shallow copy of
    *cfg*, byte-identical to the input (legacy pass-through).  The input dict
    is never mutated.
    """
    base = dict(cfg or {})
    mode = normalize_chat_mode(base)
    if mode is None:
        return base
    defaults = chat_mode_defaults(mode)
    effective = {**base, **defaults}
    effective['chatMode'] = mode  # normalized, for downstream/logging
    _changed = [k for k in defaults if base.get(k) != defaults[k]]
    if _changed:
        logger.info('[ChatMode] applied tier=%s — set %d flag(s): %s',
                    mode, len(_changed), ', '.join(sorted(_changed)))
    return effective
