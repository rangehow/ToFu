#!/usr/bin/env python3
"""presence_smoke.py — exercise the cross-conversation presence registry.

Two modes:

  • **In-process (default)** — drive the registry API directly
    (``lib/presence/registry.py``) and CAPTURE the exact ``presence`` frames it
    broadcasts via ``hub.add_listener``. This proves the wire contract
    (announce / record_files→conflict / depart all emit the right
    fully-formed frames) WITHOUT a running server or a browser. Verifiable in
    CI / on any host.

  • **--live** — POST to a RUNNING server's gated debug route
    (``/api/push/debug/presence``) so the registry mutations run INSIDE the
    server process and their broadcasts reach connected browsers. Use this to
    eyeball the "who's working" strip light up. The route is OFF unless the
    server was started with ``TOFU_PRESENCE_DEBUG=1``.

WHY two modes: the push hub is an in-process singleton (``lib/agent_core/push.py``),
so a standalone process calling ``announce()`` broadcasts to ITS OWN empty hub
— nothing reaches the browser. The in-process mode therefore verifies the
contract by listening to that local hub; the --live mode delegates the emit to
the server process that actually owns the browser's WebSocket.

Usage:
    # Contract smoke (no server needed) — prints the captured frames:
    python debug/presence_smoke.py [/abs/project/root]

    # Light up a running server's strip (server must have TOFU_PRESENCE_DEBUG=1):
    python debug/presence_smoke.py --live --root /abs/project/root \
        [--base-url http://localhost:5000]
    python debug/presence_smoke.py --live --root /abs/project/root --clear
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _in_process(root: str) -> int:
    """Drive the registry directly and print the captured broadcast frames."""
    from lib.presence import announce, depart, record_files, snapshot
    from lib.push import hub

    captured: list[dict] = []
    hub.add_listener(lambda channel, task_id, payload:
                     captured.append({'channel': channel, **payload}))

    print(f'== in-process presence smoke (root={root}) ==\n')

    print('1) announce two peers …')
    announce(root, 'smoke-peer-1', task_id='smoke-task-1',
             title='Refactor the parser', objective='make it ship',
             phase='working')
    announce(root, 'smoke-peer-2', task_id='smoke-task-2',
             title='Tune the LLM stream', objective='cut TTFT',
             phase='working')

    print('2) both touch the SAME file (should emit a conflict advisory) …')
    record_files(root, 'smoke-peer-1',
                 [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    record_files(root, 'smoke-peer-2',
                 [{'path': 'lib/llm/stream.py', 'action': 'patched'}])

    snap = snapshot(root)
    print(f'3) snapshot: {len(snap["peers"])} active peer(s):')
    for p in snap['peers']:
        print(f'     • {p["title"]!r} status={p["status"]} '
              f'label={p["statusLabel"]!r} files={p.get("files")}')

    print('\n4) depart both …')
    depart(root, 'smoke-peer-1')
    depart(root, 'smoke-peer-2')

    # ── Sub-agent scenario: ONE conversation, TWO sub-agents, same file. ──
    print('\n5) announce ONE conversation with TWO sub-agents on the SAME '
          'file (within-conversation conflict) …')
    sub_captured: list[dict] = []
    hub.add_listener(lambda channel, task_id, payload:
                     sub_captured.append({'channel': channel, **payload}))
    announce(root, 'smoke-swarm', task_id='smoke-task-3', title='Swarm session',
             objective='parallel refactor', phase='working')
    for aid in ('agent-coder-1', 'agent-coder-2'):
        announce(root, 'smoke-swarm', agent_id=aid, task_id='smoke-task-3',
                 title='coder', parent_title='Swarm session', phase='working')
        record_files(root, 'smoke-swarm',
                     [{'path': 'lib/llm/stream.py', 'action': 'patched'}],
                     agent_id=aid)
    sub_conflicts = [f.get('conflict', {}).get('message', '')
                     for f in sub_captured if f.get('kind') == 'conflict']
    sub_peer_sets = [tuple(sorted(f.get('conflict', {}).get('peers', [])))
                     for f in sub_captured if f.get('kind') == 'conflict']
    if sub_conflicts:
        print('  within-conversation conflict advisory (verbatim):')
        for m in sub_conflicts:
            print(f'     ⚠ {m}')
    depart(root, 'smoke-swarm', agent_id='agent-coder-1')
    depart(root, 'smoke-swarm', agent_id='agent-coder-2')
    depart(root, 'smoke-swarm')

    # Summarise the captured frames by kind.
    kinds: dict[str, int] = {}
    conflict_msgs: list[str] = []
    for f in captured:
        k = f.get('kind', '?')
        kinds[k] = kinds.get(k, 0) + 1
        if k == 'conflict':
            conflict_msgs.append(f.get('conflict', {}).get('message', ''))

    print('\n== captured presence frames ==')
    print(f'  cross-conv by kind: {kinds}')
    if conflict_msgs:
        print('  cross-conv conflict advisory (rendered verbatim by the strip):')
        for m in conflict_msgs:
            print(f'     ⚠ {m}')

    # The sub-agent conflict must name two DISTINCT sub-agent peer keys.
    sub_ok = bool(sub_conflicts) and any(
        set(ps) == {'smoke-swarm#agent-coder-1', 'smoke-swarm#agent-coder-2'}
        for ps in sub_peer_sets)

    # Assert the contract held (cross-conv AND within-conv). The two listeners
    # overlap (the first captures sub-agent frames too), so use >= bounds for
    # the aggregate counts; the decisive within-conv check is sub_ok.
    ok = (kinds.get('update', 0) >= 4         # 2 announce + ≥2 record_files
          and kinds.get('conflict', 0) >= 1   # the cross-conv shared-file overlap
          and kinds.get('depart', 0) >= 2     # at least both conv peers departed
          and sub_ok)                         # within-conversation conflict (decisive)
    print(f'\n  every frame channel == "presence": '
          f'{all(f["channel"] == "presence" for f in captured + sub_captured)}')
    print(f'  within-conversation sub-agent conflict OK: {sub_ok}')
    print(f'  contract OK: {ok}')
    if not ok:
        print('\nFULL FRAMES:')
        print(json.dumps(captured + sub_captured, indent=2, ensure_ascii=False))
        return 1
    return 0


def _live(base_url: str, root: str, action: str) -> int:
    """POST to the running server's gated debug route."""
    import requests
    url = base_url.rstrip('/') + '/api/push/debug/presence'
    payload = {'root': root, 'action': action}
    print(f'POST {url}  {payload}')
    try:
        resp = requests.post(url, json=payload,
                             proxies={'http': None, 'https': None}, timeout=15)
    except Exception as e:
        print(f'request failed: {e}')
        return 1
    print(f'  HTTP {resp.status_code}: {resp.text[:300]}')
    if resp.status_code == 403:
        print('\n  → the server has presence debug DISABLED. Restart it with '
              'TOFU_PRESENCE_DEBUG=1 to enable this route.')
        return 1
    if resp.status_code != 200:
        return 1
    if action == 'subagents':
        print('\n  ✓ sub-agent scenario fired — open a conversation whose project '
              f'root is\n    {root}\n  and the strip should show ONE conversation '
              'with TWO nested sub-agent rows + a within-conversation conflict '
              'advisory.\n  Run with --clear to remove them.')
    elif action != 'clear':
        print('\n  ✓ scenario fired — open a conversation whose project root is\n'
              f'    {root}\n  and the presence strip should light up with two peers'
              ' + a conflict advisory.\n  Run with --clear to remove them.')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', nargs='?', default=os.getcwd(),
                    help='project root path (default: cwd)')
    ap.add_argument('--live', action='store_true',
                    help='POST to a running server instead of in-process')
    ap.add_argument('--root', dest='root_opt', default=None,
                    help='project root (overrides positional; for --live)')
    ap.add_argument('--base-url', default=os.environ.get(
        'TOFU_BASE_URL', 'http://localhost:5000'),
        help='server base URL for --live (default: http://localhost:5000)')
    ap.add_argument('--clear', action='store_true',
                    help='(--live) depart the fake peers instead of announcing')
    ap.add_argument('--subagents', action='store_true',
                    help='(--live) fire the within-conversation sub-agent '
                         'conflict scenario instead of the cross-conversation one')
    args = ap.parse_args()

    root = os.path.abspath(args.root_opt or args.root)
    if args.live:
        action = 'clear' if args.clear else ('subagents' if args.subagents else 'scenario')
        return _live(args.base_url, root, action)
    return _in_process(root)


if __name__ == '__main__':
    sys.exit(main())
