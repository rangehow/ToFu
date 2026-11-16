"""lib/memory/user_profile — the rolling, bounded personal-preference profile.

This is a THIRD memory placement, distinct from the two that already exist:

  1. System-prefix injection (BP1–3, always-on) — cache-poison for anything
     that changes; the memory-count hint was ripped out for exactly this
     reason (see ``.tofu/memories/memory-count-hint-mutates-cached-system-prefix.md``).
  2. Per-turn local memory selection (``<relevant_memories>`` in the tail) —
     metadata-ranked and deliberately precision-first, so standing preferences
     belong in the profile rather than retrieval evidence.

A personal preference needs to be BOTH always-on AND cache-stable. The trick
(validated by Hermes Agent + our own CLAUDE.md placement): put it in the
Composer-owned head/tail ``_isMeta`` messages rather than rewriting real user
history. When the profile changes, only the synthetic context carrier changes.

Design choices:
  * Sized by its content, never capped or truncated: the always-present owner
    profile (Phase 1, docs/modules/context_engineering.md "Owner profile
    (Phase 1)") is exactly the owner's active statements. The former
    2.5 KB hard cap was removed with Phase 1 (AGENTS.md principle 5).
  * NOT part of the BM25 corpus — it is never "searched", it is always present.
  * SCOPED by owner. Every task's scope is its ``owner_user_id`` (``'1'`` for
    the personal owner, including open mode) → a per-owner file
    ``<data>/memories/profiles/<scope>/.tofu_user_profile.md`` so one owner's
    profile is never injected into another's prompt. ``scope=''`` is the
    legacy open-mode file ``<data>/memories/.tofu_user_profile.md``, which the
    personal owner inherits once with audited statuses. Scope is
    resolved from the request's ``AuthContext`` via ``resolve_profile_scope``
    and captured onto the task at creation (the consolidation daemon has no
    request context). The ``.tofu`` prefix (per the artifact registry — see
    ``lib/agent_loop/agent_artifacts.py``) is preserved on the filename either way.
  * Rendered in experiment 1c's directive form (``_render.profile_body``):
    only items the owner stated or confirmed, each with its date and source;
    grants only inside their bound.

This package is storage + rendering ONLY. The propose-confirm capture loop
(layer 3) builds on ``load_profile`` / ``save_profile``.

No code lives in this file — it is a pure re-export facade. All implementations
live in the sub-modules (``_paths`` / ``_io`` / ``_render`` / ``_mutate`` /
``_pending``). This module is the explicit public boundary for profile and
durable-context operations.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Scope + path resolution (._paths) ──
from lib.memory.user_profile._paths import (  # noqa: E402,F401
    _pending_path,
    _sanitize_scope,
    _server_memories_dir,
    context_changes_path,
    context_path,
    profile_path,
    resolve_profile_scope,
)

# ── Structured user context + undo history (._context) ──
from lib.memory.user_profile._context import (  # noqa: E402,F401
    CONTEXT_STATUS_REASONS,
    CONTEXT_STATUSES,
    CONTEXT_TYPES,
    ContextConflictError,
    ContextValidationError,
    context_char_count,
    context_markdown,
    context_status,
    context_transaction,
    create_context_item,
    delete_context_item,
    deleted_context_items,
    is_active_context_item,
    load_context,
    save_context_items,
    undo_context_change,
    undo_legacy_context_inheritance,
    update_context_item,
)

# ── Body persistence + markers + structured items (._io) ──
from lib.memory.user_profile._io import (  # noqa: E402,F401
    _CORE_HEADERS,
    _PROFILE_DETAIL_MARKER,
    _PROFILE_MARKER,
    load_profile,
    parse_items,
    profile_char_count,
    save_items,
    save_profile,
    serialize_items,
)

# ── Rendering + tiering (._render) ──
from lib.memory.user_profile._render import (  # noqa: E402,F401
    DELEGATION_BOUND,
    PROFILE_TRUST_CLASSES,
    USAGE_RULE,
    context_ids_for_event,
    context_items_for_event,
    profile_body,
    profile_items,
    profile_summary_for_event,
    render_profile_block,
)

# ── Consolidation write primitives (._mutate) ──
from lib.memory.user_profile._mutate import (  # noqa: E402,F401
    _DEFAULT_HEADER,
    apply_new_preference,
    apply_reinforcement,
)

# ── Propose-then-confirm gate (._pending) ──
from lib.memory.user_profile._pending import (  # noqa: E402,F401
    load_pending,
    resolve_pending,
    stage_pending,
)

__all__ = [
    'resolve_profile_scope',
    'profile_path',
    'context_path',
    'context_changes_path',
    'CONTEXT_TYPES',
    'CONTEXT_STATUSES',
    'CONTEXT_STATUS_REASONS',
    'is_active_context_item',
    'undo_legacy_context_inheritance',
    'ContextValidationError',
    'ContextConflictError',
    'load_context',
    'context_status',
    'context_transaction',
    'context_markdown',
    'context_char_count',
    'save_context_items',
    'create_context_item',
    'update_context_item',
    'delete_context_item',
    'deleted_context_items',
    'undo_context_change',
    'load_profile',
    'save_profile',
    'profile_char_count',
    'DELEGATION_BOUND',
    'PROFILE_TRUST_CLASSES',
    'USAGE_RULE',
    'profile_body',
    'profile_items',
    'render_profile_block',
    'context_ids_for_event',
    'context_items_for_event',
    'profile_summary_for_event',
    'apply_reinforcement',
    'apply_new_preference',
    'parse_items',
    'serialize_items',
    'save_items',
    'load_pending',
    'stage_pending',
    'resolve_pending',
]
