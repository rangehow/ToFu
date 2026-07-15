#!/usr/bin/env python3
"""debug/cache_within_task_probe.py — reproduce the WITHIN-TASK pinned-cache_read
scenario (the screenshot: one task, 42 rounds, cache_read pinned at the system
floor, whole body re-written every round).

Unlike cache_live_conv_byte_diff.py (which grows the RAW message list — that
crosses user turns and moves the board/digest block), this simulates a SINGLE
task's inner tool loop: _inject_system_contexts runs ONCE, then each round
appends an assistant(tool_calls) + tool(result) pair, and we run the exact
per-round production pipeline (run_compaction_pipeline → sort_tool_results →
build_body → add_cache_breakpoints → openai_body_to_anthropic) and byte-diff
the SHARED prefix of consecutive rounds' final wire bodies.

  identical shared prefix  → within-task miss is genuinely server-side / cache
                             mechanics (breakpoint layout), NOT our bytes.
  diverging shared prefix  → we mutate the cached body every round → client bug.

Run:  python debug/cache_within_task_probe.py <conv_id> [--rounds 5]
"""
import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _msg_bytes(msg):
    def _strip_cc(o):
        if isinstance(o, dict):
            return {k: _strip_cc(v) for k, v in o.items() if k != 'cache_control'}
        if isinstance(o, list):
            return [_strip_cc(x) for x in o]
        return o
    return json.dumps(_strip_cc(msg), ensure_ascii=False, sort_keys=False).encode('utf-8')


def _pipeline_body(messages, model, tools, task, round_num):
    """Run the exact per-round pipeline on a COPY, return post-translation body."""
    from lib.tasks_pkg.compaction import run_compaction_pipeline
    from lib.tasks_pkg.cache_tracking import sort_tool_results
    from lib.llm import build_body, add_cache_breakpoints
    from lib.llm.anthropic_outbound import openai_body_to_anthropic

    msgs = copy.deepcopy(messages)
    run_compaction_pipeline(msgs, round_num, task=task)
    sort_tool_results(msgs, conv_id=task.get('convId', ''))
    body = build_body(model, msgs, max_tokens=2048, thinking_enabled=True,
                      thinking_depth='medium', tools=tools, stream=True)
    body['_task_id'] = ''
    add_cache_breakpoints(body, log_prefix='[within-probe]')
    body = openai_body_to_anthropic(body)
    return body


def _dump_markers(msgs):
    out = []
    for i, m in enumerate(msgs):
        c = m.get('content')
        if isinstance(c, list):
            for bi, blk in enumerate(c):
                if isinstance(blk, dict) and 'cache_control' in blk:
                    out.append(f'msg[{i}]/{m.get("role")}/blk{bi}:{blk.get("type")}')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('conv_id')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--model', default='aws.claude-opus-4.8')
    args = ap.parse_args()

    from debug.cache_live_conv_byte_diff import _load_raw, _tools_for
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    from lib.tasks_pkg.system_context import _inject_system_contexts

    raw, settings = _load_raw(args.conv_id)
    if raw is None:
        print('conv not found'); return 2
    model = settings.get('model') or args.model
    config = {'systemPrompt': settings.get('systemPrompt', ''), 'model': model}
    pp = os.getcwd()

    # Build api messages + inject ONCE (as the real loop does at round 0).
    messages = _transform_messages(copy.deepcopy(raw), config)
    tools = _tools_for()
    _tn = {(t.get('function') or {}).get('name') for t in (tools or []) if isinstance(t, dict)}
    _tn.discard(None)
    task = {'id': 'probe', 'convId': args.conv_id, 'config': config,
            'toolRounds': [], 'content': '', 'segments': []}
    _inject_system_contexts(messages, pp, project_enabled=True,
                            memory_enabled=True, search_enabled=True,
                            swarm_enabled=True, has_real_tools=bool(tools),
                            conv_id=args.conv_id, task=task, model=model,
                            tool_names=_tn or None)
    print(f'== within-task probe == conv={args.conv_id} model={model} '
          f'base_msgs={len(messages)} rounds={args.rounds}\n')

    prev_body = None
    prev_msgs = None
    for rn in range(args.rounds):
        body = _pipeline_body(messages, model, tools, task, rn)
        bmsgs = body['messages']
        if prev_body is not None:
            shared = len(prev_msgs) - 1  # prev minus its in-flight tail
            div = -1
            for i in range(min(shared, len(bmsgs))):
                if _msg_bytes(prev_msgs[i]) != _msg_bytes(bmsgs[i]):
                    div = i
                    break
            sa = json.dumps(prev_body.get('system'), ensure_ascii=False).encode()
            sb = json.dumps(body.get('system'), ensure_ascii=False).encode()
            sys_same = sa == sb
            if div == -1:
                print(f'round {rn}: shared prefix ({shared} msgs) BYTE-IDENTICAL; '
                      f'system {"same" if sys_same else "CHANGED"}')
            else:
                r = prev_msgs[div].get('role')
                a, b = _msg_bytes(prev_msgs[div]), _msg_bytes(bmsgs[div])
                j = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]),
                         min(len(a), len(b)))
                print(f'round {rn}: DIVERGES at msg[{div}] role={r} byte{j} '
                      f'(lenPrev={len(a)} lenCur={len(b)}); system '
                      f'{"same" if sys_same else "CHANGED"}')
                print(f'    prev: ...{a[max(0,j-30):j+120]!r}')
                print(f'    cur : ...{b[max(0,j-30):j+120]!r}')
            mk = _dump_markers(bmsgs)
            print(f'    markers: {mk}')

        # Simulate the loop appending this round's assistant(tool_calls)+tool.
        tcid = f'probe_call_{rn}'
        messages.append({'role': 'assistant', 'content': f'Working on step {rn}.',
                         'tool_calls': [{'id': tcid, 'type': 'function',
                                         'function': {'name': 'grep_search',
                                                      'arguments': json.dumps({'pattern': f'x{rn}'})}}]})
        messages.append({'role': 'tool', 'tool_call_id': tcid,
                         'content': f'result payload for round {rn} ' + ('lorem ' * 40)})
        prev_body, prev_msgs = body, bmsgs
    return 0


if __name__ == '__main__':
    sys.exit(main())
