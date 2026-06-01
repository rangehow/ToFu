#!/usr/bin/env python3
"""debug/reeval_pricing_tags.py — Static rewriter for pricing-tier tags.

Scans committed source files that encode pricing-tier tags (currently
just 'cheap', but anything in PRICING_TIERS) and rewrites them in place
so they match what reevaluate_pricing_tags() would compute at runtime.

The runtime layer in lib/llm_dispatch/config.py already normalizes tags
on every /api/server-config GET, /api/provider-templates GET,
/api/discover-models, and /api/provider-probe call — so the committed
source is effectively cosmetic.  This script keeps the source and the
runtime view in sync so code review diffs reflect reality.

Targets
-------
- ``static/provider_templates/*.json`` — JSON templates consumed by the
  one-click provider setup UI.
- ``lib/llm_dispatch/config.py`` — ``DEFAULT_SLOT_CONFIGS`` reference
  table (rewrites the ``caps`` set in each entry).
- ``static/js/settings.js`` — the ``_PROVIDER_TEMPLATES`` JS literal that
  powers the "Sync template" / "Add provider" UI.  Stale tags here are
  directly visible because ``_syncFromTemplate`` writes the template's
  capabilities into the user's provider list client-side, bypassing any
  backend re-eval until the next save.

Usage
-----
    python3 debug/reeval_pricing_tags.py             # rewrite in place
    python3 debug/reeval_pricing_tags.py --dry-run   # show diff only
    python3 debug/reeval_pricing_tags.py --check     # fail if stale
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

# Ensure we can import lib.* from the project root
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.llm_dispatch.config import (  # noqa: E402
    DEFAULT_SLOT_CONFIGS,
    MANAGED_TIER_TAGS,
    get_pricing_tiers,
    reevaluate_pricing_tags,
)


# ══════════════════════════════════════════════════════
#  JSON provider templates
# ══════════════════════════════════════════════════════

# Regex for a model entry's "capabilities" array in a template JSON file.
# Non-greedy between [ and ] so we match per-entry even when the file has
# many entries on consecutive lines.  Captures the leading indent so we
# can preserve it in the replacement.
_JSON_CAPS_RE = re.compile(
    r'("capabilities"\s*:\s*)\[[^\]]*\]',
    re.DOTALL,
)


def _canonical_caps_order(caps: set[str]) -> list[str]:
    """Return caps in the project's canonical display order."""
    order = ['text', 'vision', 'thinking', 'cheap', 'image_gen', 'embedding']
    ordered = [c for c in order if c in caps]
    extras = sorted(c for c in caps if c not in order)
    return ordered + extras


def _rewrite_template_json(path: Path, dry_run: bool) -> tuple[bool, int]:
    """Re-evaluate tier tags in a provider template JSON file.

    Preserves the original file's compact formatting (one model per line)
    by doing per-entry text-level replacements rather than json.dump().

    Returns ``(changed, num_models_changed)``.
    """
    try:
        text = path.read_text(encoding='utf-8')
        tpl = json.loads(text)
    except Exception as e:
        print('  [json] failed to parse %s: %s' % (path, e), file=sys.stderr)
        return False, 0

    models = tpl.get('models')
    if not isinstance(models, list):
        return False, 0

    # Compute desired caps per model using the runtime helper on a copy,
    # so we don't mutate the parsed dict (we rewrite the file by text).
    import copy as _copy
    models_copy = _copy.deepcopy(models)
    reevaluate_pricing_tags(
        models_copy, log_prefix='template=%s' % tpl.get('key', path.stem),
    )

    # Build (model_id → desired caps list) diff table.
    diffs: list[tuple[str, list[str], list[str]]] = []
    desired_by_id: dict[str, list[str]] = {}
    for orig, new in zip(models, models_copy):
        mid = new.get('model_id') or orig.get('model_id') or ''
        if not mid:
            continue
        old_caps = set(orig.get('capabilities') or [])
        new_caps = set(new.get('capabilities') or [])
        if old_caps == new_caps:
            continue
        desired_by_id[mid] = _canonical_caps_order(new_caps)
        diffs.append((
            mid,
            sorted(old_caps & MANAGED_TIER_TAGS),
            sorted(new_caps & MANAGED_TIER_TAGS),
        ))

    if not diffs:
        return False, 0

    for mid, b, a in diffs:
        added = sorted(set(a) - set(b))
        removed = sorted(set(b) - set(a))
        pieces = []
        if added:
            pieces.append('+' + ','.join(added))
        if removed:
            pieces.append('-' + ','.join(removed))
        print('  [%s] %s  %s' % (path.name, mid, ' '.join(pieces)))

    if dry_run:
        return True, len(diffs)

    # ── Surgical text-level rewrite to preserve formatting ──
    # For each changed model, locate its JSON entry block and replace
    # just the "capabilities": [...] inside it.
    new_text = text
    for mid, new_caps in desired_by_id.items():
        # Find the entry: look for "model_id": "<mid>".  Escape the id
        # for regex (model ids may contain '.', ':').  Then find the
        # nearest capabilities array after it (within a reasonable span).
        id_pat = re.compile(
            r'"model_id"\s*:\s*"' + re.escape(mid) + r'"',
        )
        m = id_pat.search(new_text)
        if not m:
            print('  [%s] WARNING: could not locate entry for %s — skipped'
                  % (path.name, mid), file=sys.stderr)
            continue
        # Search for the capabilities key within a bounded window (one entry)
        window_start = m.end()
        # Cap search to next 2 KB or next "model_id" — whichever is first
        next_id = id_pat.search(new_text, window_start)
        window_end = min(
            len(new_text),
            (next_id.start() if next_id else len(new_text)),
            window_start + 2048,
        )
        window = new_text[window_start:window_end]
        caps_match = _JSON_CAPS_RE.search(window)
        if not caps_match:
            print('  [%s] WARNING: no capabilities array for %s — skipped'
                  % (path.name, mid), file=sys.stderr)
            continue
        # Render replacement as inline JSON array, preserving the prefix
        # (the part before '[').  Use the same quote style as source.
        caps_inline = '[' + ', '.join('"%s"' % c for c in new_caps) + ']'
        replacement = caps_match.group(1) + caps_inline
        abs_start = window_start + caps_match.start()
        abs_end = window_start + caps_match.end()
        new_text = new_text[:abs_start] + replacement + new_text[abs_end:]

    path.write_text(new_text, encoding='utf-8')
    return True, len(diffs)


def _rewrite_templates(dry_run: bool) -> int:
    tpl_dir = _PROJECT_ROOT / 'static' / 'provider_templates'
    if not tpl_dir.is_dir():
        return 0
    total_changed = 0
    for path in sorted(tpl_dir.glob('*.json')):
        changed, n = _rewrite_template_json(path, dry_run)
        if changed:
            total_changed += n
    return total_changed


# ══════════════════════════════════════════════════════
#  DEFAULT_SLOT_CONFIGS (lib/llm_dispatch/config.py)
# ══════════════════════════════════════════════════════

# Capture one model entry:
#   'model_id':                     {'caps': {...}, 'rpm': ..., 'cost': ..., ...},
# The model id may contain : and . (e.g. 'us.anthropic.foo-v1:0').
# We match across lines because some entries span two rows.
_ENTRY_RE = re.compile(
    r"""(?P<indent>^[ \t]*)
        '(?P<mid>[^']+)'\s*:\s*
        \{(?P<body>[^{}]*)\}
        (?P<trail>,?)[ \t]*$""",
    re.VERBOSE | re.MULTILINE,
)

# Match 'caps' set literal:   'caps': {'text', 'vision', 'cheap'},
_CAPS_RE = re.compile(r"'caps'\s*:\s*\{(?P<items>[^{}]*)\}")

# Match 'cost': 0.002  (accept scientific notation just in case)
_COST_RE = re.compile(r"'cost'\s*:\s*(?P<v>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


def _parse_caps_literal(body: str) -> set[str] | None:
    """Return the current set of caps strings, or None if not found."""
    m = _CAPS_RE.search(body)
    if not m:
        return None
    raw = '{' + m.group('items') + '}'
    try:
        val = ast.literal_eval(raw)
    except Exception:
        return None
    if isinstance(val, set):
        return {str(x) for x in val}
    return None


def _render_caps_literal(caps: set[str]) -> str:
    """Render caps set in the project's canonical order.

    Order: text, vision, thinking, cheap, image_gen, embedding, then any
    extras alphabetically.  Matches the human-readable ordering used
    throughout the existing DEFAULT_SLOT_CONFIGS table.
    """
    order = ['text', 'vision', 'thinking', 'cheap', 'image_gen', 'embedding']
    ordered = [c for c in order if c in caps]
    extras = sorted(c for c in caps if c not in order)
    final = ordered + extras
    inner = ', '.join("'%s'" % c for c in final)
    return "{" + inner + "}"


def _rewrite_default_slot_configs(dry_run: bool) -> int:
    """Rewrite the caps set inside every entry of DEFAULT_SLOT_CONFIGS.

    Returns the number of entries whose caps changed.
    """
    path = _PROJECT_ROOT / 'lib' / 'llm_dispatch' / 'config.py'
    text = path.read_text(encoding='utf-8')

    # Only rewrite within the DEFAULT_SLOT_CONFIGS block — find its span.
    start = text.find('DEFAULT_SLOT_CONFIGS = {')
    if start < 0:
        print('  [config.py] DEFAULT_SLOT_CONFIGS not found — skipped',
              file=sys.stderr)
        return 0
    # Find the matching closing brace at column 0 (the table is dedented).
    end = text.find('\n}\n', start)
    if end < 0:
        end = len(text)
    block = text[start:end + 2]  # include closing brace + newline

    changes = 0

    def _sub(match: re.Match) -> str:
        nonlocal changes
        mid = match.group('mid')
        body = match.group('body')
        caps = _parse_caps_literal(body)
        if caps is None:
            return match.group(0)
        # Non-chat models: leave tier tags untouched.
        if caps & {'image_gen', 'embedding'}:
            return match.group(0)
        cost_m = _COST_RE.search(body)
        cost_val = float(cost_m.group('v')) if cost_m else None
        desired_tiers = get_pricing_tiers(mid, fallback_cost_per_1k=cost_val)
        new_caps = (caps - MANAGED_TIER_TAGS) | desired_tiers
        if new_caps == caps:
            return match.group(0)
        changes += 1
        added = sorted(new_caps - caps)
        removed = sorted(caps - new_caps)
        pieces = []
        if added:
            pieces.append('+' + ','.join(added))
        if removed:
            pieces.append('-' + ','.join(removed))
        print('  [config.py] %s  %s' % (mid, ' '.join(pieces)))
        # Replace the caps literal in the body.
        new_body = _CAPS_RE.sub(
            lambda _m: "'caps': " + _render_caps_literal(new_caps),
            body,
            count=1,
        )
        return (match.group('indent')
                + "'" + mid + "': {" + new_body + '}'
                + match.group('trail'))

    new_block = _ENTRY_RE.sub(_sub, block)
    if changes and not dry_run:
        new_text = text[:start] + new_block + text[end + 2:]
        path.write_text(new_text, encoding='utf-8')

    return changes


# ══════════════════════════════════════════════════════
#  static/js/settings.js — _PROVIDER_TEMPLATES JS literal
# ══════════════════════════════════════════════════════
#
# Each model entry in the JS literal looks like one of:
#     { model_id: 'gpt-5.4-nano', capabilities: ['text', 'vision', 'cheap'], rpm: 200, cost: 0.001 },
#     { model_id: 'foo', aliases: ['bar'], capabilities: ['text'], rpm: 60, cost: 0.001 },
# We match one line at a time: lines that contain BOTH `model_id:` and
# `capabilities:` are candidates for rewriting.  This keeps the regex
# small and avoids false positives elsewhere in the ~4k-line file.

_JS_ENTRY_RE = re.compile(
    r"""
    (?P<prefix>model_id\s*:\s*['"](?P<mid>[^'"]+)['"][^\n]*?
               capabilities\s*:\s*)
    \[(?P<items>[^\]]*)\]
    """,
    re.VERBOSE,
)

# `cost: 0.003` or `cost: 0` (no scientific notation in practice).
_JS_COST_RE = re.compile(r"cost\s*:\s*(?P<v>-?\d+(?:\.\d+)?)")


def _parse_js_caps(items: str) -> set[str] | None:
    """Parse a JS array body like ``'text', 'vision', 'cheap'`` into a set."""
    # Allow single- or double-quoted strings; tolerate trailing commas / whitespace.
    out: set[str] = set()
    for m in re.finditer(r"""['"]([^'"]+)['"]""", items):
        out.add(m.group(1))
    return out if out else None


def _render_js_caps(caps: set[str]) -> str:
    """Render caps in canonical order using single-quoted strings."""
    order = _canonical_caps_order(caps)
    return ', '.join("'%s'" % c for c in order)


def _rewrite_settings_js(dry_run: bool) -> int:
    """Rewrite ``capabilities: [...]`` arrays inside the _PROVIDER_TEMPLATES
    literal in static/js/settings.js.

    Only matches lines that contain BOTH ``model_id:`` AND
    ``capabilities:`` on the same line — this precisely targets the
    template literal's single-line entries and ignores everything else
    (function code, pricing-cache lookups, etc.).

    Returns the number of model entries whose caps changed.
    """
    path = _PROJECT_ROOT / 'static' / 'js' / 'settings.js'
    if not path.is_file():
        return 0
    text = path.read_text(encoding='utf-8')

    changes = 0
    duplicates: dict[str, int] = {}

    def _sub(match: re.Match) -> str:
        nonlocal changes
        mid = match.group('mid')
        items = match.group('items')
        # Find the cost value on the SAME line for the blended-fallback
        # path.  Look in a short window around the match.
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_end = text.find('\n', match.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        cost_m = _JS_COST_RE.search(line)
        cost_val = float(cost_m.group('v')) if cost_m else None

        caps = _parse_js_caps(items)
        if caps is None:
            return match.group(0)
        # Non-chat entries: leave untouched.
        if caps & {'image_gen', 'embedding'}:
            return match.group(0)

        desired_tiers = get_pricing_tiers(mid, fallback_cost_per_1k=cost_val)
        new_caps = (caps - MANAGED_TIER_TAGS) | desired_tiers
        if new_caps == caps:
            return match.group(0)
        changes += 1
        duplicates[mid] = duplicates.get(mid, 0) + 1
        added = sorted(new_caps - caps)
        removed = sorted(caps - new_caps)
        pieces = []
        if added:
            pieces.append('+' + ','.join(added))
        if removed:
            pieces.append('-' + ','.join(removed))
        dup_note = ' (#%d)' % duplicates[mid] if duplicates[mid] > 1 else ''
        print('  [settings.js] %s%s  %s' % (mid, dup_note, ' '.join(pieces)))
        return match.group('prefix') + '[' + _render_js_caps(new_caps) + ']'

    new_text = _JS_ENTRY_RE.sub(_sub, text)
    if changes and not dry_run:
        path.write_text(new_text, encoding='utf-8')

    return changes


# ══════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='Show what would change without writing files.')
    ap.add_argument('--check', action='store_true',
                    help='Exit 1 if any file is stale (CI-friendly).')
    args = ap.parse_args()

    dry = args.dry_run or args.check

    print('[PricingTags] Re-evaluating against tiers: %s'
          % ', '.join(sorted(MANAGED_TIER_TAGS)))
    print('[PricingTags] Scanning static/provider_templates/*.json …')
    tpl_changes = _rewrite_templates(dry_run=dry)

    print('[PricingTags] Scanning lib/llm_dispatch/config.py '
          '(DEFAULT_SLOT_CONFIGS) …')
    cfg_changes = _rewrite_default_slot_configs(dry_run=dry)

    print('[PricingTags] Scanning static/js/settings.js '
          '(_PROVIDER_TEMPLATES) …')
    js_changes = _rewrite_settings_js(dry_run=dry)

    total = tpl_changes + cfg_changes + js_changes
    mode = 'WOULD CHANGE' if dry else 'CHANGED'
    print('[PricingTags] %s: %d model(s) across templates + config.py '
          '+ settings.js' % (mode, total))

    if args.check and total > 0:
        print('[PricingTags] --check: stale tags detected. Run without '
              '--check to rewrite.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
