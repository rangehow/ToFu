#!/usr/bin/env python3
"""debug/cache_acceptance_check.py — prefix-cache deploy-acceptance verdict.

Closes the loop the objective's acceptance criterion (2) requires: the three
content-freeze commits (ab161bf str↔block, 1274cee raw↔stripped, 0a9f6af
prefill-skip) are committed-but-not-necessarily-deployed. "Flawless prefix
caching" can only be asserted from POST-RESTART real traffic showing BOTH
already-cached-turn miss classes at (near) zero:

  * ``WIRE PREFIX CHANGED … inside_prior_cached_prefix=True``  (canonical-visible)
  * ``WIRE BYTES DIVERGED … field=[…]``                        (canonical-invisible)

This script is READ-ONLY (parses logs/app.log). It emits a machine-greppable
verdict line so a timer/agent can decide READY vs WAIT without guessing:

  ACCEPTANCE: <READY|WAIT|FAIL> boot=<ts> samples=<n> prefix_changed=<n>
              bytes_diverged=<n> reason=<...>

Gate (ALL must hold for READY):
  1. The PROCESS ACTUALLY SERVING 15000 started AFTER FIX_COMMIT_TS. This is
     the TRUE deploy signal — NOT a log ``server.boot`` banner. Learned the
     hard way (2026-07-17): the main server PID 1952548 had been up since
     18:55 (pre-fix) while dozens of ephemeral sibling/probe instances on other
     ports emitted their OWN 20:xx boot banners into the shared app.log. Keying
     on the latest banner falsely reported "deployed" and produced a bogus FAIL
     even though the live server ran pre-fix code. So we read the real serving
     process's start time from ``ps`` and use its boot banner as the log cursor.
  2. At least MIN_SAMPLES cache-bearing requests (``CacheStats`` lines) exist
     AFTER that process started (enough traffic to be meaningful).
  3. Both miss-class counts on already-cached turns are <= TOLERANCE.

Exit code 0 + ``ACCEPTANCE: READY`` when the caching is verified flawless;
exit 0 + ``ACCEPTANCE: WAIT`` when not enough post-restart traffic yet (keep
polling); exit 0 + ``ACCEPTANCE: FAIL`` when the sample is sufficient but a
miss class still fires (the fix is deployed but NOT working → investigate).

Usage:
    python3 debug/cache_acceptance_check.py [--log PATH] [--min-samples N]
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Latest content-freeze commit timestamp (0a9f6af, 2026-07-17 19:42:09 +0800).
# A boot must postdate this for the fixes to be live. Override via env.
FIX_COMMIT_TS = os.environ.get('FIX_COMMIT_TS', '2026-07-17 19:42:09')
DEFAULT_LOG = os.environ.get('TOFU_APP_LOG', 'logs/app.log')
MIN_SAMPLES = int(os.environ.get('CACHE_ACCEPT_MIN_SAMPLES', '150'))
TOLERANCE = int(os.environ.get('CACHE_ACCEPT_TOLERANCE', '0'))

_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_BOOT_RE = re.compile(r'server\.boot .*starting up')
_PREFIX_CHANGED_RE = re.compile(r'WIRE PREFIX CHANGED')
_INSIDE_TRUE_RE = re.compile(r'inside_prior_cached_prefix=True')
_BYTES_DIVERGED_RE = re.compile(r'WIRE BYTES DIVERGED')
_CACHESTATS_RE = re.compile(r'CacheStats')


def _ts_of(line: str) -> str | None:
    m = _TS_RE.match(line)
    return m.group(1) if m else None


def _serving_pid_start() -> str | None:
    """Return the START time (``YYYY-MM-DD HH:MM:SS``) of the process actually
    serving via ``server.py``, or None if it can't be determined.

    This is the TRUE deploy signal — a log boot banner can come from an
    ephemeral sibling/probe instance on another port and does NOT mean the live
    15000 server reloaded. We read the real long-lived ``python server.py``
    process's lstart from ``ps``.
    """
    import subprocess
    import time as _t
    try:
        out = subprocess.run(
            ['ps', '-eo', 'lstart,cmd'], capture_output=True, text=True,
            timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    newest = None
    for ln in out.splitlines():
        if 'server.py' not in ln or 'grep' in ln:
            continue
        # lstart is the first 5 whitespace fields: 'Fri Jul 17 18:55:53 2026'
        parts = ln.split()
        if len(parts) < 5:
            continue
        try:
            st = _t.strptime(' '.join(parts[:5]), '%a %b %d %H:%M:%S %Y')
            ts = _t.strftime('%Y-%m-%d %H:%M:%S', st)
        except ValueError:
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


def analyze(log_path: str, min_samples: int) -> dict:
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except OSError as e:
        return {'verdict': 'WAIT', 'reason': f'log unreadable: {e}',
                'boot': '', 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0}

    # Gate 1 (TRUE deploy signal): the process ACTUALLY serving must have
    # started after the fix commit. A log boot banner is NOT sufficient — it
    # can come from an ephemeral sibling/probe instance on another port.
    boot_ts = _serving_pid_start()
    if boot_ts is None:
        # Fallback: no ps access — degrade to the latest banner but SAY SO.
        for i, ln in enumerate(lines):
            if _BOOT_RE.search(ln):
                boot_ts = _ts_of(ln) or boot_ts
        if not boot_ts:
            return {'verdict': 'WAIT', 'reason': 'no serving PID and no boot '
                    'banner found', 'boot': '', 'samples': 0,
                    'prefix_changed': 0, 'bytes_diverged': 0}

    if boot_ts <= FIX_COMMIT_TS:
        return {'verdict': 'WAIT',
                'reason': f'serving process started {boot_ts}, predates fix '
                          f'commit {FIX_COMMIT_TS} — live server still runs '
                          'pre-fix code; a real 15000 restart is required',
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0}

    # Cursor: count only log lines AT/AFTER the serving process's start time.
    boot_idx = 0
    for i, ln in enumerate(lines):
        ts = _ts_of(ln)
        if ts and ts >= boot_ts:
            boot_idx = i
            break

    # Count post-deploy signals.
    post = lines[boot_idx:]
    samples = sum(1 for ln in post if _CACHESTATS_RE.search(ln))
    prefix_changed = sum(1 for ln in post
                         if _PREFIX_CHANGED_RE.search(ln)
                         and _INSIDE_TRUE_RE.search(ln))
    bytes_diverged = sum(1 for ln in post if _BYTES_DIVERGED_RE.search(ln))

    # Gate 2: enough traffic?
    if samples < min_samples:
        verdict = 'WAIT'
        reason = (f'only {samples}/{min_samples} post-restart cache requests — '
                  'keep sampling')
    # Gate 3: both classes at/under tolerance?
    elif prefix_changed <= TOLERANCE and bytes_diverged <= TOLERANCE:
        verdict = 'READY'
        reason = ('flawless: both already-cached-turn miss classes at/under '
                  f'tolerance {TOLERANCE}')
    else:
        verdict = 'FAIL'
        reason = (f'deployed but STILL firing: prefix_changed={prefix_changed} '
                  f'bytes_diverged={bytes_diverged} (>tolerance {TOLERANCE}) — '
                  'a drift face remains; use the field-level tracer '
                  '(field=[...]) to name it')

    return {'verdict': verdict, 'reason': reason, 'boot': boot_ts,
            'samples': samples, 'prefix_changed': prefix_changed,
            'bytes_diverged': bytes_diverged}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=DEFAULT_LOG)
    ap.add_argument('--min-samples', type=int, default=MIN_SAMPLES)
    args = ap.parse_args()

    r = analyze(args.log, args.min_samples)
    print(f"ACCEPTANCE: {r['verdict']} boot={r['boot']!r} "
          f"samples={r['samples']} prefix_changed={r['prefix_changed']} "
          f"bytes_diverged={r['bytes_diverged']} reason={r['reason']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
