#!/usr/bin/env python3
"""Post-restart acceptance check for the runaway-agent guards (2026-07-27).

WHY THIS EXISTS
---------------
"Merged" is not "live". On 2026-07-27 one swarm sub-agent ran 26,683,114
rounds in 3.5h and wrote 9.1 GB into logs/app.log. The fix (chassis
no-progress breaker + bounded SubTaskSpec defaults) is committed, but a
long-lived server keeps running the code it was STARTED with — the running
processes predated the commit and were still emitting the unbounded
``Round N/∞`` shape hours later.

The project already learned this the hard way with the LoopWatch non-blocking
logging fix, which was only declared effective after a post-restart
faulthandler dump PROVED it. This script is the same discipline for the
runaway guards, made repeatable.

It is deliberately EVIDENCE-BASED and does NOT require the incident to recur:
every check reads either the live code the process would load, or the log
surface an operator actually watches.

USAGE
    python tests/_acceptance_runaway_guards.py            # after a restart
    python tests/_acceptance_runaway_guards.py --json     # machine-readable

Exit code 0 = all checks passed; 1 = at least one FAILED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_LOG = os.path.join(_ROOT, 'logs', 'app.log')
_AUDIT = os.path.join(_ROOT, 'logs', 'audit.log')

# The commit that introduced the breaker. A server started BEFORE this
# commit's timestamp cannot possibly be running it.
_FIX_COMMIT = '7f2df9bd'


def _result(name, ok, detail):
    return {'check': name, 'ok': bool(ok), 'detail': detail}


def check_spec_defaults():
    """SubTaskSpec must not be constructible as unlimited + untimed."""
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='researcher', objective='probe')
    ok = spec.timeout_seconds == 1800
    unbounded = (spec.max_rounds == 0 and spec.timeout_seconds == 0)
    return _result(
        'spec_defaults_bounded', ok and not unbounded,
        f'max_rounds={spec.max_rounds} timeout_seconds={spec.timeout_seconds} '
        f'(expected timeout_seconds=1800, never 0+0)')


def check_breaker_wired():
    """The chassis breaker must be passed by SubAgent, not left at 0."""
    from lib.swarm.agent import SubAgent
    threshold = getattr(SubAgent, '_MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS', 0)
    import inspect
    src = inspect.getsource(SubAgent._run_loop)
    wired = 'max_consecutive_no_progress_rounds' in src
    return _result(
        'breaker_wired', bool(threshold) and wired,
        f'threshold={threshold} wired_into_run_agent_loop={wired}')


def check_round_line_states_bounds():
    """The per-round log line must show real bounds, never a bare ∞."""
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    agent = SubAgent.__new__(SubAgent)
    agent.max_rounds = 0
    agent.spec = SubTaskSpec(role='r', objective='o')
    label = SubAgent._round_budget_label(agent)
    ok = label != '\u221e' and 'np=' in label
    return _result('round_line_shows_bounds', ok,
                   f'unlimited label renders as {label!r}')


def check_running_process_has_fix():
    """A server started before the fix commit is NOT running it."""
    try:
        out = subprocess.run(
            ['git', 'show', '-s', '--format=%ct', _FIX_COMMIT],
            cwd=_ROOT, capture_output=True, text=True, timeout=20)
        fix_ts = int(out.stdout.strip())
    except Exception as e:
        return _result('running_process_has_fix', False,
                       f'cannot resolve commit time: {e}')

    try:
        ps = subprocess.run(
            ['ps', '-eo', 'pid,etimes,cmd'], capture_output=True,
            text=True, timeout=20).stdout
    except Exception as e:
        return _result('running_process_has_fix', False, f'ps failed: {e}')

    now = time.time()
    stale, fresh = [], []
    for line in ps.splitlines():
        if 'server.py' not in line or 'grep' in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes = parts[0], parts[1]
        try:
            started = now - int(etimes)
        except ValueError:
            continue
        (fresh if started > fix_ts else stale).append(
            f'pid={pid} started={time.strftime("%H:%M:%S", time.localtime(started))}')

    if not fresh and not stale:
        return _result('running_process_has_fix', False,
                       'no server.py process found — nothing to accept')
    return _result(
        'running_process_has_fix', not stale,
        f'fresh={fresh or "none"} STALE(pre-fix)={stale or "none"}')


def check_no_bare_infinity_in_recent_log(window_bytes=8_000_000):
    """After a restart, freshly emitted round lines must not print a bare ∞.

    Historical lines from before the restart still contain it, so only the
    TAIL is scanned — and a bare-∞ line there means the running process is
    still the old code.
    """
    if not os.path.exists(_LOG):
        return _result('no_bare_infinity_in_tail', False, 'app.log missing')
    size = os.path.getsize(_LOG)
    with open(_LOG, 'rb') as f:
        f.seek(max(0, size - window_bytes))
        tail = f.read().decode('utf-8', 'replace')
    rounds = re.findall(r'Round \d+/(\S+) START', tail)
    if not rounds:
        return _result('no_bare_infinity_in_tail', True,
                       'no swarm round lines in tail (nothing to contradict)')
    bare = [r for r in rounds if r == '\u221e']
    return _result(
        'no_bare_infinity_in_tail', not bare,
        f'{len(rounds)} round lines in tail, {len(bare)} with a bare ∞ '
        f'(samples: {sorted(set(rounds))[:4]})')


def check_audit_channel_available():
    """audit_log is the authoritative breaker signal — it must be writable."""
    from lib.log import AUDIT_LOG_FILE
    d = os.path.dirname(AUDIT_LOG_FILE)
    ok = os.path.isdir(d) and os.access(d, os.W_OK)
    n = 0
    if os.path.exists(_AUDIT):
        with open(_AUDIT, encoding='utf-8', errors='replace') as f:
            n = sum(1 for line in f if 'agent_loop_no_progress' in line)
    return _result(
        'audit_channel_ready', ok,
        f'audit dir writable={ok}; agent_loop_no_progress events so far={n} '
        f'(0 is expected until a wedged loop actually trips)')


CHECKS = [
    check_spec_defaults,
    check_breaker_wired,
    check_round_line_states_bounds,
    check_running_process_has_fix,
    check_no_bare_infinity_in_recent_log,
    check_audit_channel_available,
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            results.append(_result(fn.__name__, False, f'raised: {e}'))

    failed = [r for r in results if not r['ok']]
    if args.json:
        print(json.dumps({'ok': not failed, 'results': results}, indent=2,
                         ensure_ascii=False))
    else:
        for r in results:
            print(f'[{"PASS" if r["ok"] else "FAIL"}] {r["check"]}: {r["detail"]}')
        print()
        print(f'{len(results) - len(failed)}/{len(results)} checks passed')
        if failed:
            print('\nNOT ACCEPTED — the guards are not proven live:')
            for r in failed:
                print(f'  - {r["check"]}: {r["detail"]}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
