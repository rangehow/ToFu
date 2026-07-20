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
import sys
from collections import Counter


_BOOT_MARKERS = (
    'Ready — handing off to Hypercorn.',
    'Ready - handing off to Hypercorn.',   # ascii-dash fallback
)


def _last_boot_ts(path: str) -> str:
    """Timestamp (leading 19 chars 'YYYY-MM-DD HH:MM:SS') of the LAST boot."""
    last = ''
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if any(m in line for m in _BOOT_MARKERS):
                last = line[:19]
    return last


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
    tot_w = sum(int(r.get('cache_write', 0)) for r in rows)
    brk_w = sum(int(r.get('cache_write', 0))
                for r in rows if r.get('bucket') != 'no_break')
    return {
        'n': n, 'buckets': dict(buckets),
        'ttl_any': ttl_any, 'ttl_sole': ttl_sole, 'mid': mid,
        'tot_w': tot_w, 'brk_w': brk_w,
        'brk_pct': (100 * brk_w // tot_w) if tot_w else 0,
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
          f'         [the direct cost metric]')
    print(f'\nPOST-restart bucket breakdown: {mq["buckets"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
