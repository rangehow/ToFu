#!/usr/bin/env python3
"""Cache-cost pre/post-restart comparator — the FINAL acceptance measurement.

The owner's acceptance line for the 2026-07-20 prompt-cache fixes is REAL cost
reduction proven on POST-RESTART traffic, not offline unit tests. This script
reads ``logs/app.log`` (or a path arg), splits every ``[CacheRoundRecord]`` JSON
line at the LAST server boot marker, and prints the three hard numbers the owner
asked for, PRE vs POST restart:

  (1) ttl_flip round count      — the chokepoint _task_id stamp (a34beae) should
                                   drive the <ttl-flip> re-key toward 0.
  (2) cache_mid_out_of_window   — the byte-identity gate (18c04a6) + drop-default
      bucket count                (6bcac3e) should drive this toward 0.
  (3) break-write % of all write— body_change/upstream/etc. cache_write tokens as
                                   a fraction of ALL cache_write — the direct
                                   "cost really dropped" metric.

It also breaks POST-restart down by bucket so any residual is attributable.

The boot split is essential: the four fixes only take effect in a process
started AFTER they landed, so mixing pre-fix rounds into the count understates
the improvement. A record counts as POST-restart iff its timestamp is >= the
last ``Ready — handing off to Hypercorn.`` line's timestamp.

Usage:
    python debug/cache_cost_prepost_restart.py [path/to/app.log]
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter


_BOOT_MARKERS = (
    'Ready — handing off to Hypercorn.',
    'Ready - handing off to Hypercorn.',   # ascii-dash fallback
)

# Minimum steady-state rounds (post round-1 / turn-boundary exclusion) below
# which ttl_flip=0 / mid_oow=0 are NOT trustworthy — they could just mean
# 'traffic too short to have triggered the miss', not 'the fix works'. Override
# with env CACHE_ACCEPT_MIN_STEADY.
_MIN_STEADY = int(os.environ.get('CACHE_ACCEPT_MIN_STEADY', '200'))

# The boot self-report server.py emits (in-memory resolved mode), e.g.
# '[CacheMidMode] TOFU_CACHE_MID_MODE=drop pid=123 bootId=abc (in-memory)'.
_MIDMODE_RE = re.compile(r'\[CacheMidMode\] TOFU_CACHE_MID_MODE=(\w+)')


def _last_boot_ts(path: str) -> str:
    """Timestamp (leading 19 chars 'YYYY-MM-DD HH:MM:SS') of the LAST boot."""
    last = ''
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if any(m in line for m in _BOOT_MARKERS):
                last = line[:19]
    return last


def _boot_mid_mode(path: str, boot_ts: str) -> str:
    """The resolved TOFU_CACHE_MID_MODE the SERVING process self-reported at/after
    the last boot (server.py's [CacheMidMode] line). Empty if not found (an OLD
    build that predates the self-report, or a log without it)."""
    mode = ''
    if not boot_ts:
        return mode
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if line[:19] < boot_ts:
                continue
            m = _MIDMODE_RE.search(line)
            if m:
                mode = m.group(1)
    return mode


def _load_records(path: str):
    out = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            i = line.find('[CacheRoundRecord] ')
            if i < 0:
                continue
            ts = line[:19]
            try:
                rec = json.loads(line[i + len('[CacheRoundRecord] '):])
            except (ValueError, IndexError):
                continue
            rec['_ts'] = ts
            out.append(rec)
    return out


def _metrics(rows: list) -> dict:
    n = len(rows)
    buckets = Counter(r.get('bucket', '?') for r in rows)
    ttl_any = sum(1 for r in rows if r.get('ttl_flip'))
    # sole-culprit ttl-flip: the pure re-key rounds (needs the culprits field,
    # only present post-fix — absent on old records, so this is post-only).
    ttl_sole = sum(1 for r in rows
                   if r.get('culprits')
                   and all(c == '<ttl-flip>' for c in r['culprits']))
    mid = buckets.get('cache_mid_out_of_window', 0)
    tbr = buckets.get('turn_boundary_rebill', 0)
    # Rounds whose record shows a mid stepping-stone was PLACED. The record's
    # marker signature carries body_msg_blocks (body markers excl. system/head);
    # >=2 body markers ⇒ a mid was armed alongside the tail. drop-mode places
    # NONE, so post-fix this must be 0 — a POSITIVE confirmation independent of
    # whether any round happened to collapse. Falls back to 0 when the field is
    # absent (older records) — reported honestly as 'unknown' by the caller.
    mid_placed = 0
    mid_field_seen = 0
    for r in rows:
        bmb = r.get('body_msg_blocks')
        if isinstance(bmb, list):
            mid_field_seen += 1
            if len(bmb) >= 2:
                mid_placed += 1
    tot_w = sum(int(r.get('cache_write', 0)) for r in rows)
    brk_w = sum(int(r.get('cache_write', 0))
                for r in rows if r.get('bucket') != 'no_break')
    # ── CLEAN-BASIS break-write (the owner's anti-pollution guard) ──
    # A conversation's round-1 carries a LEGITIMATE first write (no prior prefix
    # to read back) — counting it as a 'break' inflates the ratio purely from
    # conversation-length structure, unrelated to cache invalidation. So the
    # honest cost metric excludes round-1 (call<=1) AND the turn_boundary_rebill
    # bucket (the new-turn round-1 boundary re-bill, which is that same
    # structural first-write across a turn boundary). Denominator excludes those
    # rounds' writes too, so it is a like-for-like ratio over STEADY-STATE
    # rounds only.
    def _r1(r):
        return int(r.get('call', 0) or 0) <= 1 or r.get('bucket') == 'turn_boundary_rebill'
    steady = [r for r in rows if not _r1(r)]
    tot_w_clean = sum(int(r.get('cache_write', 0)) for r in steady)
    brk_w_clean = sum(int(r.get('cache_write', 0))
                      for r in steady if r.get('bucket') != 'no_break')
    return {
        'n': n, 'buckets': dict(buckets),
        'ttl_any': ttl_any, 'ttl_sole': ttl_sole, 'mid': mid, 'tbr': tbr,
        'mid_placed': mid_placed, 'mid_field_seen': mid_field_seen,
        'tot_w': tot_w, 'brk_w': brk_w,
        'brk_pct': (100 * brk_w // tot_w) if tot_w else 0,
        'n_steady': len(steady),
        'brk_pct_clean': (100 * brk_w_clean // tot_w_clean) if tot_w_clean else 0,
        'span': (rows[0]['_ts'], rows[-1]['_ts']) if rows else ('', ''),
    }


def main(argv):
    path = argv[1] if len(argv) > 1 else 'logs/app.log'
    boot = _last_boot_ts(path)
    rows = _load_records(path)
    if not rows:
        print(f'No [CacheRoundRecord] lines in {path}')
        return 1
    if not boot:
        print('WARNING: no boot marker found — cannot split; treating ALL as one slice.')
        pre, post = rows, []
    else:
        pre = [r for r in rows if r['_ts'] < boot]
        post = [r for r in rows if r['_ts'] >= boot]

    mp, mq = _metrics(pre), _metrics(post)
    print(f'log={path}')
    print(f'last boot marker = {boot or "(none)"}')
    print(f'PRE-restart : n={mp["n"]:5}  span {mp["span"][0]} .. {mp["span"][1]}')
    print(f'POST-restart: n={mq["n"]:5}  span {mq["span"][0]} .. {mq["span"][1]}')
    if mq['n'] == 0:
        print('\n⏳ NO post-restart records yet — restart + generate traffic, then re-run.')
        return 0

    def _rate(m, key):
        return (100 * m[key] // m['n']) if m['n'] else 0

    print('\n                         PRE            POST')
    print(f'(1) ttl_flip rounds      {mp["ttl_any"]:5} ({_rate(mp,"ttl_any"):2}%)   '
          f'{mq["ttl_any"]:5} ({_rate(mq,"ttl_any"):2}%)   [target→0: chokepoint stamp]')
    print(f'    ttl_flip SOLE-culprit {mp["ttl_sole"]:4}         {mq["ttl_sole"]:5}'
          f'         [pure re-key; culprits field is post-fix only]')
    print(f'(2) cache_mid_out_of_win {mp["mid"]:5} ({_rate(mp,"mid"):2}%)   '
          f'{mq["mid"]:5} ({_rate(mq,"mid"):2}%)   [target→0: gate + drop]')
    print(f'(3) break-write %% of all {mp["brk_pct"]:4}%%        {mq["brk_pct"]:4}%%'
          f'         [RAW — polluted by round-1 first-writes]')
    print(f'(3c) break-write %% CLEAN {mp["brk_pct_clean"]:4}%%        {mq["brk_pct_clean"]:4}%%'
          f'         [★ excl. round-1 (call<=1) + turn_boundary_rebill]')
    print(f'     turn_boundary_rebill {mp["tbr"]:5}         {mq["tbr"]:5}'
          f'         [broken out — structural first-write, not cache invalidation]')
    print(f'     steady-state rounds  {mp["n_steady"]:5}         {mq["n_steady"]:5}'
          f'         [denominator of the clean ratio]')
    print(f'\nPOST-restart bucket breakdown: {mq["buckets"]}')

    # ── GUARD 1: minimum-sample floor (anti-false-negative on the 0s) ──
    print('\n── acceptance guards ──')
    if mq['n_steady'] < _MIN_STEADY:
        print(f'⚠ SAMPLE TOO SMALL: {mq["n_steady"]} steady-state rounds '
              f'< {_MIN_STEADY} floor. ttl_flip={mq["ttl_any"]} / '
              f'mid_oow={mq["mid"]} are NOT yet trustworthy — a 0 here may just '
              f'mean traffic was too short to trigger the miss, not that the fix '
              f'works. Keep generating multi-round traffic and re-run.')
    else:
        print(f'✓ sample sufficient: {mq["n_steady"]} steady-state rounds '
              f'>= {_MIN_STEADY} floor — the 0-counts are meaningful.')

    # ── GUARD 2: positive drop confirmation (not just mid_oow=0 by absence) ──
    boot_mode = _boot_mid_mode(path, boot)
    if boot_mode:
        print(f'✓ boot self-report: running TOFU_CACHE_MID_MODE={boot_mode} '
              f'(in-memory) — proves WHICH layout the serving process loaded.')
        if boot_mode != 'drop':
            print(f'  ⚠ mode is NOT drop — the drop-default fix is not the '
                  f'active layout; mid_oow=0 would NOT be attributable to it.')
    else:
        print('⚠ no [CacheMidMode] boot self-report found — cannot positively '
              'confirm drop is running (old build predating server.py self-report, '
              'or pre-restart log). Restart onto new HEAD to get it.')
    if mq['mid_field_seen'] == 0:
        print('  mid-placement evidence: UNKNOWN (records carry no '
              'body_msg_blocks field — pre-fix records).')
    else:
        print(f'  mid-placement evidence: {mq["mid_placed"]} of '
              f'{mq["mid_field_seen"]} records PLACED a mid marker '
              f'(drop-mode target = 0). >0 means some send still armed a mid '
              f'→ drop not fully in effect.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
