#!/usr/bin/env python3
"""Generate the tool inventory — one machine-readable row per tool × facet.

WHY THIS EXISTS
---------------
Tofu ships ~90 tools. Their SCHEMA and their HANDLER are centralized in
``lib/tools/registry`` + the dispatch ``ToolRegistry``, but a tool's *other*
facets (UI label, approval enricher, gates, serial dispatch, …) live in
separate tables across ``lib/tasks_pkg/tool_dispatch/`` and
``lib/tasks_pkg/handlers/``. Nothing anywhere describes ONE tool completely,
so reviewing 90 tools meant reading ~15 files per tool — in practice nobody
did, and hand-sampling proved unreliable (a manual probe of 8 tools concluded
"one tool lacks a UI label"; the real number is 75).

This script DERIVES that description instead of anyone maintaining it, and
the output doubles as a drift guard (``--check``).

DESIGN RULES (mirrors scripts/gen_frontend_globals.py — see the charter's
"generative declarations" discipline):

* **Derived, never hand-written.** A hand-maintained inventory becomes the
  19th disagreeing copy of the tool list. Editing the generated file is
  pointless: ``--check`` overwrites the argument.
* **Built-ins only in the checked table.** Third-party ``tofu.tools`` entry
  points differ per deployment (this host loads ``liantong_resume``), so
  pinning them in a CI-enforced file makes the gate red on any machine with a
  different plugin set. Plugins are reported in a separate DIAGNOSTIC section
  that ``--check`` ignores — visible, but never a false CI failure.
* **Facts, not judgements.** Each column states what the code does; the
  ratchets in ``tests/test_tool_inventory_generated.py`` decide what is
  acceptable.

USAGE
    python3 scripts/gen_tool_inventory.py            # write the inventory
    python3 scripts/gen_tool_inventory.py --check    # CI: fail if stale
    python3 scripts/gen_tool_inventory.py --json     # machine consumption
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

OUTPUT_PATH = os.path.join(REPO_ROOT, 'docs', 'TOOL_INVENTORY.md')

#: Facet columns, in report order.
COLUMNS = [
    'tool', 'category', 'spec', 'dispatch', 'write', 'idempotent',
    'label', 'approval_enricher', 'serial', 'read_gate', 'fresh_gate',
    'streamable', 'arg_repair',
]


def _load_registry():
    """Import the app far enough that every built-in handler is registered."""
    import lib.tasks_pkg.handlers  # noqa: F401 — registration side-effect
    from lib.tasks_pkg.executor import tool_registry
    from lib.tools import all_specs
    return tool_registry, all_specs()


def _facet_tables():
    """Collect the scattered per-facet tables, each behind its own try.

    A facet whose module fails to import degrades to "unknown" for every tool
    rather than aborting the whole inventory — the point is to SEE the tool
    surface, and a partial view still beats none. Failures are reported on
    stderr so a silently-empty column can't masquerade as "no tool has this".
    """
    tables: dict[str, object] = {}

    def _try(name, fn):
        try:
            tables[name] = fn()
        except Exception as e:  # noqa: BLE001 — diagnostic breadth is the point
            print(f'[gen_tool_inventory] WARNING: facet {name!r} unavailable: {e}',
                  file=sys.stderr)
            tables[name] = None

    def _labels():
        from lib.tasks_pkg.tool_dispatch._labels import tool_label
        return tool_label

    def _enrichers():
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
        return set(_APPROVAL_META_ENRICHERS)

    def _serial():
        from lib.tasks_pkg.tool_dispatch._heartbeat import _SERIAL_BLOCKING_TOOLS
        return set(_SERIAL_BLOCKING_TOOLS)

    def _partitions():
        from lib.tasks_pkg.tool_dispatch._flags import (
            _IDEMPOTENT_TOOLS, _WRITE_TOOLS,
        )
        return {'write': set(_WRITE_TOOLS), 'idempotent': set(_IDEMPOTENT_TOOLS)}

    def _read_gate():
        from lib.tasks_pkg.handlers._read_gate import _GATED_TOOLS
        return set(_GATED_TOOLS)

    def _fresh_gate():
        from lib.tasks_pkg.handlers._write_freshness_gate import (
            _GATED_BATCH_TOOLS, _GATED_SINGLE_TOOLS,
        )
        return set(_GATED_SINGLE_TOOLS) | set(_GATED_BATCH_TOOLS)

    def _streamable():
        from lib.tasks_pkg.streaming_tool_executor import _STREAMABLE_TOOLS
        return set(_STREAMABLE_TOOLS)

    def _arg_repair():
        from lib.tool_input_repair._transform import _PARAM_ALIASES
        return set(_PARAM_ALIASES)

    _try('label', _labels)
    _try('enricher', _enrichers)
    _try('serial', _serial)
    _try('partitions', _partitions)
    _try('read_gate', _read_gate)
    _try('fresh_gate', _fresh_gate)
    _try('streamable', _streamable)
    _try('arg_repair', _arg_repair)
    return tables


def _dispatch_path(registry, name: str) -> str:
    """How ``lookup(name)`` resolves — EXACT / SET / NONE.

    Worth a column: most tools (83 of 90) resolve through a ``_sets`` entry,
    and that asymmetry is exactly what made the plugin-shadow hijack possible.
    ``NONE`` means a schema is advertised to the model with no handler behind
    it — a tool that can be called but never executed.
    """
    if name in registry._exact:
        return 'EXACT'
    if any(name in s for s, _ in registry._sets):
        return 'SET'
    return 'NONE'


def collect() -> dict:
    """Build the full inventory: built-in rows + a plugin diagnostic list."""
    registry, specs = _load_registry()
    t = _facet_tables()
    label_fn = t.get('label')
    parts = t.get('partitions') or {}
    write_set = parts.get('write') or set()
    idem_set = parts.get('idempotent') or set()

    builtin_rows, plugin_rows = [], []
    for spec in specs:
        for name in sorted(spec.provides):
            has_label = (label_fn is not None and label_fn(name) != name)
            row = {
                'tool': name,
                'category': spec.category or '',
                'spec': spec.key,
                'dispatch': _dispatch_path(registry, name),
                'write': name in write_set,
                'idempotent': name in idem_set,
                'label': has_label,
                'approval_enricher': name in (t.get('enricher') or set()),
                'serial': name in (t.get('serial') or set()),
                'read_gate': name in (t.get('read_gate') or set()),
                'fresh_gate': name in (t.get('fresh_gate') or set()),
                'streamable': name in (t.get('streamable') or set()),
                'arg_repair': name in (t.get('arg_repair') or set()),
            }
            if spec.source == 'plugin':
                row['plugin'] = spec.plugin_name
                plugin_rows.append(row)
            else:
                builtin_rows.append(row)

    builtin_rows.sort(key=lambda r: (r['category'], r['tool']))
    plugin_rows.sort(key=lambda r: (r.get('plugin', ''), r['tool']))
    return {'builtin': builtin_rows, 'plugin': plugin_rows}


def _mark(v) -> str:
    return '✓' if v is True else ('' if v is False else str(v))


def render(inv: dict) -> str:
    """Render the inventory as Markdown (the committed, --check'd artifact)."""
    b = inv['builtin']
    out: list[str] = []
    out.append('# Tool inventory (GENERATED — do not edit)')
    out.append('')
    out.append('Regenerate with `python3 scripts/gen_tool_inventory.py`; CI pins it')
    out.append('via `tests/test_tool_inventory_generated.py --check`.')
    out.append('')
    out.append('One row per BUILT-IN tool. Every column is derived from the live')
    out.append('registry + the per-facet tables — nothing here is hand-maintained.')
    out.append('Third-party plugin tools vary per deployment and are listed in the')
    out.append('diagnostic section at the end, which `--check` ignores.')
    out.append('')
    out.append(f'Built-in tools: **{len(b)}**')
    out.append('')

    gap_write_no_enricher = [r['tool'] for r in b
                             if r['write'] and not r['approval_enricher']]
    gap_no_label = [r['tool'] for r in b if not r['label']]
    gap_no_handler = [r['tool'] for r in b if r['dispatch'] == 'NONE']

    out.append('## Gaps')
    out.append('')
    out.append('| gap | count | meaning |')
    out.append('|---|---|---|')
    out.append(f'| write tool with no approval enricher | {len(gap_write_no_enricher)} | '
               'the approval dialog renders a bare tool name — the user approves '
               'blind, which the approval module itself calls "worse than not '
               'prompting at all" |')
    out.append(f'| no UI label | {len(gap_no_label)} | the raw tool name is shown '
               'in the activity line |')
    out.append(f'| no reachable handler | {len(gap_no_handler)} | schema advertised '
               'to the model but nothing executes it |')
    out.append('')
    if gap_write_no_enricher:
        out.append('Write tools lacking an approval enricher:')
        out.append('')
        for name in gap_write_no_enricher:
            out.append(f'- `{name}`')
        out.append('')

    out.append('## Built-in tools')
    out.append('')
    out.append('| ' + ' | '.join(COLUMNS) + ' |')
    out.append('|' + '---|' * len(COLUMNS))
    for r in b:
        cells = [r['tool'], r['category'], r['spec'], r['dispatch']] + \
                [_mark(r[c]) for c in COLUMNS[4:]]
        out.append('| ' + ' | '.join(cells) + ' |')
    out.append('')

    out.append('## Plugin tools (diagnostic — NOT pinned by --check)')
    out.append('')
    if inv['plugin']:
        out.append('| tool | plugin | dispatch | write |')
        out.append('|---|---|---|---|')
        for r in inv['plugin']:
            out.append(f"| {r['tool']} | {r.get('plugin', '')} | {r['dispatch']} "
                       f"| {_mark(r['write'])} |")
    else:
        out.append('_No third-party plugin tools loaded in this environment._')
    out.append('')
    return '\n'.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='exit 1 if the committed inventory is stale')
    ap.add_argument('--json', action='store_true',
                    help='dump the raw inventory as JSON to stdout')
    args = ap.parse_args()

    inv = collect()
    if args.json:
        print(json.dumps(inv, indent=2, ensure_ascii=False))
        return 0

    rendered = render(inv)
    if args.check:
        try:
            with open(OUTPUT_PATH, encoding='utf-8') as f:
                current = f.read()
        except OSError:
            print(f'MISSING: {OUTPUT_PATH} — run scripts/gen_tool_inventory.py',
                  file=sys.stderr)
            return 1
        if current != rendered:
            print(f'STALE: {OUTPUT_PATH} does not match the live registry. '
                  f'Run: python3 scripts/gen_tool_inventory.py', file=sys.stderr)
            return 1
        print(f'OK: {OUTPUT_PATH} is in sync '
              f'({len(inv["builtin"])} built-in tools)')
        return 0

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(rendered)
    print(f'Wrote {OUTPUT_PATH} ({len(inv["builtin"])} built-in, '
          f'{len(inv["plugin"])} plugin)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
