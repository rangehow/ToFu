#!/usr/bin/env python3
"""debug_panel_fidelity.py — READ-ONLY diagnostic: quantify how far the debug
panel's message list drifts from what the LLM actually receives.

WHY
===
The debug panel is fed by two backend paths that DISAGREE with each other and
both stop short of the real outbound (wire) message array:

  • COLD path  — ``GET /api/v1/conversations/<id>/debug-messages`` →
                 ``build_api_messages_from_db(conv_id, {'systemPrompt': ...})``
                 → ``_transform_messages``.  Does NOT run
                 ``_inject_system_contexts`` → no CLAUDE.md / guidance / memory /
                 date / swarm / preference blocks.
  • HOT path   — the live ``messages_snapshot`` SSE, captured in
                 ``orchestrator.py`` AFTER ``_inject_system_contexts`` but
                 BEFORE ``sort_tool_results`` (cache_tracking) and BEFORE
                 ``build_body`` (llm_sanitize). So it shows the injected
                 contexts but NOT the cache-reorder or the gateway/orphan/merge
                 sanitization the model truly sees.

This script reconstructs, for ONE conversation, three message lists and diffs
them so we can decide the alignment design with DATA, not guesswork:

  (A) COLD  = build_api_messages_from_db(...)                         [today's /debug-messages]
  (B) HOT   = (A) + _inject_system_contexts                          [today's live snapshot]
  (C) WIRE  = (B) + sort_tool_results + <OpenAI-form sanitize subset> [what the model receives]

The "OpenAI-form sanitize subset" is exactly the model-agnostic, IO-free part
of build_body:
    _strip_non_api_fields → _sanitize_messages (gateway terms) →
    _fix_orphaned_tool_calls → _merge_consecutive_same_role → _fix_empty_user_messages
It DELIBERATELY OMITS the transport-layer / provider-specific steps
(_validate_image_blocks disk I/O, _downscale_oversized_images Pillow,
vision-strip, gemini/claude reasoning injections) — those are flagged as
"transport-layer transforms" and out of scope for the OpenAI-form array, per
the design decision.

SAFETY
======
* 100% READ-ONLY. Performs a single SELECT (``--conv-id`` mode) or reads a
  local JSON file (``--messages-json`` mode, the recommended offline default).
  Never writes to the conversations table, never starts a task, never touches
  the push hub.
* ``_inject_system_contexts`` is called with a THROWAWAY ``task={}`` dict and
  ``conv_id=''`` so its only side effects (task['_appliedPreferences'] /
  task['_relatedConversations'] and conv-keyed TTL caches) land on garbage and
  are never shared with a live task. Pass ``--real-conv-id`` to opt into the
  shared conv-keyed cache (slightly more faithful related-convs digest, but
  touches the same TTL cache the live path uses — read-mostly, same values).
* Project-context (CLAUDE.md) and memory reads are READ-ONLY FUSE reads. Use
  ``--no-project`` / ``--no-memory`` to stub them out entirely (avoids FUSE).
* Output (JSON report) defaults to a DolphinFS scratch dir under the project,
  NOT /tmp. Override with ``--out``.

USAGE (staged — do NOT run until confirmed)
===========================================
  # Offline, fully stubbed (no DB, no FUSE) — feed a raw messages array:
  python debug/debug_panel_fidelity.py --messages-json debug/_scratch/raw_msgs.json

  # Read-only against a real conversation row (single SELECT):
  python debug/debug_panel_fidelity.py --conv-id <CONV_ID> --no-memory

  # Faithful memory + project, real conv-keyed cache:
  python debug/debug_panel_fidelity.py --conv-id <CONV_ID> --real-conv-id
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project importable when run from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DEFAULT_OUT = os.path.join(_ROOT, 'debug', '_scratch', 'debug_panel_fidelity')


def _load_raw_messages(args) -> list[dict]:
    """Return the raw conversation messages (offline file OR read-only SELECT)."""
    if args.messages_json:
        with open(args.messages_json, encoding='utf-8') as f:
            data = json.load(f)
        # Accept either a bare list or a conv-row dict {messages: [...]}.
        if isinstance(data, dict) and isinstance(data.get('messages'), list):
            return data['messages']
        if isinstance(data, list):
            return data
        raise SystemExit('--messages-json must contain a list or {"messages": [...]}')

    if args.conv_id:
        # READ-ONLY: this is the exact loader the cold path uses (a SELECT).
        from lib.tasks_pkg.conv_message_builder import _load_messages_from_db
        raw = _load_messages_from_db(args.conv_id)
        if raw is None:
            raise SystemExit(f'conv {args.conv_id!r} not found / unreadable')
        return raw

    raise SystemExit('provide either --messages-json or --conv-id')


def _build_cold(raw: list[dict], system_prompt: str) -> list[dict]:
    """(A) Today's /debug-messages output: _transform_messages only."""
    from lib.tasks_pkg.conv_message_builder import _transform_messages
    return _transform_messages([dict(m) for m in raw], {'systemPrompt': system_prompt})


def _build_hot(cold: list[dict], args) -> list[dict]:
    """(B) Cold + _inject_system_contexts (today's live snapshot shape).

    Uses a throwaway task dict and (by default) conv_id='' for full isolation.
    """
    from lib.tasks_pkg.system_context import _inject_system_contexts
    msgs = [dict(m) for m in cold]
    throwaway_task: dict = {}
    cid = args.conv_id if (args.real_conv_id and args.conv_id) else ''
    _inject_system_contexts(
        msgs,
        project_path=(args.project_path or _ROOT),
        project_enabled=not args.no_project,
        memory_enabled=not args.no_memory,
        search_enabled=True,
        swarm_enabled=not args.no_swarm,
        has_real_tools=True,
        conv_id=cid,
        task=throwaway_task,
        model=args.model,
        system_prompt_mode='append',
        tool_names=None,
        disabled_blocks=None,
    )
    # Surface what inject wrote onto the throwaway task — these are the
    # round-variant / personal chips the cold path has no round context for.
    if throwaway_task.get('_appliedPreferences') or throwaway_task.get('_relatedConversations'):
        print('[inject side-effects on throwaway task] '
              f"prefs={bool(throwaway_task.get('_appliedPreferences'))} "
              f"relatedConvs={bool(throwaway_task.get('_relatedConversations'))}")
    return msgs


def _build_wire(hot: list[dict], conv_id: str) -> list[dict]:
    """(C) Hot + sort_tool_results + OpenAI-form sanitize subset of build_body.

    Mirrors build_body() up to the transport/provider/image steps, which are
    intentionally omitted (transport-layer transforms).
    """
    from lib.llm_sanitize import (
        _fix_empty_user_messages,
        _fix_orphaned_tool_calls,
        _merge_consecutive_same_role,
        _sanitize_messages,
        _strip_non_api_fields,
    )
    from lib.tasks_pkg.cache_tracking import sort_tool_results

    msgs = [dict(m) for m in hot]
    sort_tool_results(msgs, conv_id=conv_id or '')        # cache-aware reorder (orchestrator:1531)
    clean = _strip_non_api_fields(msgs)                   # drop frontend metadata
    _sanitize_messages(clean)                             # gateway-blocked terms (sankuai)
    clean = _fix_orphaned_tool_calls(clean)               # orphan tool_use/result repair
    clean = _merge_consecutive_same_role(clean)           # consecutive same-role merge
    _fix_empty_user_messages(clean)                       # empty-content placeholder
    return clean


# ── Diff helpers ─────────────────────────────────────────────────────────────

def _msg_text(m: dict) -> str:
    c = m.get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return '\n'.join(b.get('text', '') for b in c
                         if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _fingerprint(m: dict) -> dict:
    txt = _msg_text(m)
    return {
        'role': m.get('role'),
        'len': len(txt),
        'tool_calls': [(tc.get('function') or {}).get('name')
                       for tc in (m.get('tool_calls') or [])],
        'tool_call_id': m.get('tool_call_id'),
        'head': txt[:80].replace('\n', '⏎'),
    }


def _system_blocks(msgs: list[dict]) -> str:
    return '\n'.join(_msg_text(m) for m in msgs if m.get('role') == 'system')


def _summarize(label: str, msgs: list[dict]) -> dict:
    sys_chars = len(_system_blocks(msgs))
    return {
        'label': label,
        'count': len(msgs),
        'system_chars': sys_chars,
        'tool_msgs': sum(1 for m in msgs if m.get('role') == 'tool'),
        'assistant_tool_call_msgs': sum(1 for m in msgs if m.get('tool_calls')),
        'fingerprints': [_fingerprint(m) for m in msgs],
    }


# Markers that identify each injected block (mirror system_context.py).
_BLOCK_MARKERS = {
    'project_claude_md': '[PROJECT CO-PILOT MODE]',
    'static_guidance': '# Function Result Clearing',
    'memory': '<memory_accumulation>',
    'relevant_memories': '<relevant_memories>',
    'swarm': '<parallel_execution>',
    'preference_profile': '[USER PREFERENCE PROFILE]',
    'current_date': 'Current date:',
}


def _block_presence(text: str) -> dict:
    return {name: (marker in text) for name, marker in _BLOCK_MARKERS.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--conv-id', help='read-only SELECT of this conversation row')
    src.add_argument('--messages-json', help='offline: path to a raw messages JSON array')
    ap.add_argument('--system-prompt', default='', help='user systemPrompt to seed the cold path')
    ap.add_argument('--model', default='claude-sonnet-4', help='model id for the # Environment block')
    ap.add_argument('--project-path', default='', help='project root for CLAUDE.md (default: this repo)')
    ap.add_argument('--no-project', action='store_true', help='stub out project context (no FUSE)')
    ap.add_argument('--no-memory', action='store_true', help='stub out memory (no FUSE)')
    ap.add_argument('--no-swarm', action='store_true', help='disable the swarm block')
    ap.add_argument('--real-conv-id', action='store_true',
                    help='pass the real conv_id into inject (shares conv-keyed TTL cache)')
    ap.add_argument('--out', default=_DEFAULT_OUT, help='report output dir (DolphinFS scratch)')
    args = ap.parse_args()

    raw = _load_raw_messages(args)
    print(f'[load] {len(raw)} raw conversation message(s)')

    cold = _build_cold(raw, args.system_prompt)
    hot = _build_hot(cold, args)
    wire = _build_wire(hot, args.conv_id or '')

    sum_cold = _summarize('COLD (/debug-messages today)', cold)
    sum_hot = _summarize('HOT  (live snapshot today)', hot)
    sum_wire = _summarize('WIRE (what the model receives)', wire)

    print('\n=== HEADLINE ===')
    for s in (sum_cold, sum_hot, sum_wire):
        print(f"  {s['label']:34s} msgs={s['count']:3d}  "
              f"system_chars={s['system_chars']:7d}  tool_msgs={s['tool_msgs']:3d}")

    print('\n=== INJECTED-BLOCK PRESENCE (system text) ===')
    pres_cold = _block_presence(_system_blocks(cold))
    pres_hot = _block_presence(_system_blocks(hot))
    for name in _BLOCK_MARKERS:
        print(f"  {name:20s} cold={int(pres_cold[name])}  hot={int(pres_hot[name])}  "
              f"{'<-- ONLY IN HOT (drift)' if pres_hot[name] and not pres_cold[name] else ''}")

    print('\n=== HOT → WIRE STRUCTURAL DELTA (sanitize/sort effect) ===')
    print(f"  msg count   {sum_hot['count']} -> {sum_wire['count']} "
          f"(delta {sum_wire['count'] - sum_hot['count']})")
    # Tool-result order signature before/after sort.
    def _tool_order(msgs):
        return [m.get('tool_call_id') for m in msgs if m.get('role') == 'tool']
    to_hot, to_wire = _tool_order(hot), _tool_order(wire)
    print(f"  tool order  {'UNCHANGED' if to_hot == to_wire else 'REORDERED by sort_tool_results'}")

    os.makedirs(args.out, exist_ok=True)
    report_path = os.path.join(args.out, 'report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({
            'cold': sum_cold, 'hot': sum_hot, 'wire': sum_wire,
            'block_presence': {'cold': pres_cold, 'hot': pres_hot},
        }, f, ensure_ascii=False, indent=2)
    print(f'\n[report] wrote {report_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
