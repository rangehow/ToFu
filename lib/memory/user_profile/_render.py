"""Render the structured, always-on My Context prompt block and event items.

The block body is the owner profile (design record "Owner understanding",
Phase 1; docs/modules/context_engineering.md "Owner profile (Phase 1)"):
experiment 1c's directive form, the carrier FM-2x2 measured
(``refs/recovered/memory-design/fm2x2`` ``7f641bf390``,
``evaluations/owner_profile_arm/profile.py``). Only active items the owner
stated or confirmed are shown (``PROFILE_TRUST_CLASSES``), each whole, with its
date and source; grants only inside their bound. Nothing counts, ranks or
truncates it.

Entry points: ``profile_items``, ``profile_body``, ``render_profile_block``,
``context_items_for_event``, ``context_ids_for_event``.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.memory.contracts import context_item_trust_class
from lib.memory.user_profile._io import (
    _PROFILE_MARKER,
    load_profile,
)

logger = get_logger(__name__)

#: Trust classes (``lib.memory.contracts.memory_trust_class``) the profile may
#: present as the owner's own: an inferred item would contradict ``USAGE_RULE``.
PROFILE_TRUST_CLASSES = frozenset({'owner-stated', 'verified'})

USAGE_RULE = (
    "Each line is a standing directive the owner stated himself, with the date he last said it "
    "and where. Apply the ones that fit this request without asking him to restate them. Name the "
    "[id] of any directive that changes what you do. A directive never supplies a missing fact "
    "(who, which, where) and never approves an irreversible or external act: when the request "
    "lacks such a fact or needs such an approval, ask. If one looks out of scope or stale for this "
    "situation, say so rather than applying it silently."
)
DIRECTIVES_HEADING = '## Owner directives: all projects'
IDENTITY_HEADING = '## About the owner'
DELEGATION_HEADING = '## Delegation grants, bounded by existing policy'
#: The grants' bound: existing policy quoted verbatim, so the profile adds no
#: rule of its own. Entries are (quote, label, authority); a spec checks every
#: quote against its authority (tests/memory/test_owner_profile.py).
DELEGATION_BOUND = (
    ("Ask only what only the user can decide or provide",
     "Coordinator policy", "lib/conversations/project_coordination.py"),
    ("(a) reversible local setting or action: the Router may do it under the grant; (b) destructive or "
     "irreversible and (c) external or visible to others (sending, publishing, inviting): never automatic, "
     "only on the owner's explicit instruction in the current request, through the action's own tool; "
     "(d) human-only by nature (entering a secret, approving a login, scanning a QR code): never attempted",
     "docs/ROUTER.md R11, authority classes of the R9 grant", "docs/ROUTER.md"),
    ("Operations whose result speaks for the owner (answers, approvals, publications) stay outside the grant.",
     "docs/ROUTER.md R9", "docs/ROUTER.md"),
    ('Never delegated: revealing a credential, subscription or device login, the "Act for me" grant, '
     "lifecycle approvals, and deletes, exports, update or shutdown without the owner's explicit "
     "instruction in the current request.",
     "docs/ROUTER.md R11", "docs/ROUTER.md"),
    ("Approval does not extend to unrelated targets or consequences.",
     "system prompt, Executing actions with care", "lib/tasks_pkg/system_prompt_base/_sections.py"),
)


def profile_items(items: list[dict], *, identity: bool = True) -> list[dict]:
    """The active items one role's profile shows, in the owner's order.

    ``identity=False`` (delegated Workers) withholds ``identity`` items.
    """
    from lib.memory.user_profile._context import is_active_context_item
    return [item for item in items or []
            if is_active_context_item(item)
            and context_item_trust_class(item) in PROFILE_TRUST_CLASSES
            and (identity or item.get('type') != 'identity')]


def _statement(item: dict) -> str:
    if item.get('type') == 'work_rule':
        text = f'When {item.get("condition", "")} → {item.get("action", "")}'
    else:
        text = item.get('text') or ''
    return ' '.join(text.split())


def _source(item: dict) -> str:
    """``owner, <date>, <where>``: the owner Turn that stated it, else My Context."""
    provenance = item.get('provenance') or {}
    at = str(provenance.get('at') or item.get('updated_at') or item.get('created_at') or '')[:10]
    where = (f'{provenance["conversation_id"]} turn {str(provenance["turn_id"])[:8]}'
             if provenance.get('conversation_id') and provenance.get('turn_id') else 'My Context')
    return f'owner, {at}, {where}'


def profile_body(items: list[dict]) -> str:
    """The profile text for items ``profile_items`` admitted; '' when none."""
    if not items:
        return ''
    directives = [item for item in items if item.get('type') in ('work_rule', 'response_preference')]
    identities = [item for item in items if item.get('type') == 'identity']
    grants = [item for item in items if item.get('type') == 'delegation_grant']
    lines = [USAGE_RULE]
    for heading, group in ((DIRECTIVES_HEADING, directives), (IDENTITY_HEADING, identities)):
        if group:
            lines += ['', heading]
            lines += [f'- [{item["id"]}] {_statement(item)} ({_source(item)})' for item in group]
    if grants:
        lines += ['', DELEGATION_HEADING, "The owner's grants, verbatim:"]
        lines += [f'- [{item["id"]}] 「{_statement(item)}」 ({_source(item)})' for item in grants]
        lines += ['Their bound, quoted verbatim from existing policy:']
        lines += [f'- “{quote}” ({label})' for quote, label, _authority in DELEGATION_BOUND]
    return '\n'.join(lines)


def render_profile_block(body: str | None = None, scope: str = '') -> str | None:
    """Render the cache-stable injection block, or None when empty.

    The returned string is wrapped in ``<system-reminder>`` (matching every
    other out-of-band injection) and carries the ``_PROFILE_MARKER`` so the
    injection-side idempotency probe can detect it. Without ``body`` it renders
    the full-audience profile of ``scope``.

    NOTE: this is placed on the appended ``_isMeta`` user message (BP4 tail),
    NEVER messages[0]. See module docstring + the injection site in
    ``lib.tasks_pkg.context_composer``.
    """
    if body is None:
        from lib.memory.user_profile._context import load_context
        body = profile_body(profile_items(load_context(scope)['items']))
    body = (body or '').strip()
    if not body:
        return None
    return (
        '<system-reminder>\n'
        f'{_PROFILE_MARKER} — durable, '
        'user-specific context. Apply every item on every turn. Work rules are '
        'conditional: when a WHEN condition matches, follow its DO action. If '
        'a rule requires an unavailable tool, explain that it is unavailable '
        'and ask before using an alternative; never bypass it silently. About-user '
        'facts provide background and are not commands. Explicit instructions in '
        'the current turn override this context; system/developer instructions '
        'remain higher authority.\n\n'
        f'{body}\n'
        '</system-reminder>'
    )


def context_items_for_event(body: str | None = None,
                            scope: str = '', *,
                            context_items: list[dict] | None = None) -> list[str]:
    """Return durable event items; supplied context_items avoids a storage reread.

    Structured snapshots preserve work-rule actions and original item order,
    unlike parsing the grouped prompt Markdown. Explicit body behavior is unchanged.
    """
    if body is None:
        if context_items is None:
            from lib.memory.user_profile._context import load_context
            context_items = load_context(scope)['items']
        return [text for _item_id, text in _event_entries(context_items)]
    return [line[2:].strip() for line in (body or '').splitlines()
            if line.startswith('- ')]


def context_ids_for_event(context_items: list[dict]) -> list[str]:
    """Item ids parallel to ``context_items_for_event``: the exact usage record.

    ``preferencesApplied.ids`` lets the owner-memory lookup name the Turns that
    applied an item even after its text was edited.
    """
    return [item_id for item_id, _text in _event_entries(context_items)]


def _event_entries(context_items: list[dict]):
    from lib.memory.user_profile._context import is_active_context_item
    for item in context_items:
        if not is_active_context_item(item):
            continue
        if item.get('type') == 'work_rule':
            yield item.get('id', ''), (f'When {item.get("condition", "")} → '
                                       f'{item.get("action", "")}')
        elif item.get('text'):
            yield item.get('id', ''), item['text']


def profile_summary_for_event(body: str | None = None,
                              max_items: int = 8, scope: str = '') -> list[str]:
    """Extract a short list of preference bullet lines for the UI chip.

    Pulls markdown bullet lines (``- ``/``* ``) from the profile so the
    "preferences applied" chip can show WHICH preferences were in play this
    turn without dumping the whole file. Header lines and blanks are skipped.
    """
    if body is None:
        body = load_profile(scope)
    items: list[str] = []
    for raw in (body or '').splitlines():
        line = raw.strip()
        if line.startswith(('- ', '* ')):
            items.append(line[2:].strip())
        if len(items) >= max_items:
            break
    return items
