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
  1. The latest ``server.boot … starting up`` banner is AFTER FIX_COMMIT_TS
     (the deploy actually happened). Env FIX_COMMIT_TS overrides the default.
  2. At least MIN_SAMPLES cache-bearing requests (``CacheStats`` lines) exist
     AFTER that boot (enough traffic to be meaningful).
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


def analyze(log_path: str, min_samples: int) -> dict:
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except OSError as e:
        return {'verdict': 'WAIT', 'reason': f'log unreadable: {e}',
                'boot': '', 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0}

    # Find the LAST boot banner and its line index + timestamp.
    boot_idx = -1
    boot_ts = ''
    for i, ln in enumerate(lines):
        if _BOOT_RE.search(ln):
            boot_idx = i
            boot_ts = _ts_of(ln) or boot_ts
    if boot_idx < 0:
        return {'verdict': 'WAIT', 'reason': 'no boot banner found',
                'boot': '', 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0}

    # Gate 1: boot must postdate the fix commit.
    if boot_ts and boot_ts <= FIX_COMMIT_TS:
        return {'verdict': 'WAIT',
                'reason': f'latest boot {boot_ts} predates fix commit '
                          f'{FIX_COMMIT_TS} — not deployed yet',
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0}

    # Count post-boot signals.
    post = lines[boot_idx + 1:]
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
