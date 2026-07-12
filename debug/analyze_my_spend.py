"""debug/analyze_my_spend.py — READ-ONLY spend forensics against the live DB.

Aggregates every message's persisted ``usage`` dict through the canonical
cost engine (lib.cost.compute_cost) and reports:
  - per-day CNY total (last N days) with tokens + effective cache-read share
  - per-model breakdown (cost, share, cache-read %, output share)
  - top conversations by cost
  - token-type split (uncached input / cache-write / cache-read / output)
  - a cache-hit-rate read (the #1 ToFu cost lever per the skills notes)

Does NOT write anything. Run:
  python3 debug/analyze_my_spend.py [days]
"""

import datetime as _dt
import sys
from collections import defaultdict

from lib.cost import compute_cost, normalize_usage
from lib.database import DOMAIN_CHAT, get_thread_db
from lib.utils import safe_json

DEFAULT_USER_ID = 1


def _safe_ts(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    # normalise seconds → ms
    if 0 < n < 1_000_000_000_0:
        n *= 1000
    return n


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    now_ms = int(_dt.datetime.now().timestamp() * 1000)
    start_ms = now_ms - days_back * 86400_000

    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings '
        'FROM conversations WHERE user_id=? AND '
        'COALESCE(updated_at, created_at, 0) >= ? ORDER BY updated_at DESC',
        (DEFAULT_USER_ID, start_ms),
    ).fetchall()

    by_day = defaultdict(float)
    by_day_tok = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: {'cost': 0.0, 'msgs': 0, 'in': 0, 'out': 0,
                                    'cw': 0, 'cr': 0})
    by_conv = defaultdict(lambda: {'cost': 0.0, 'name': '', 'msgs': 0})
    tot = {'cost': 0.0, 'in': 0, 'out': 0, 'cw': 0, 'cr': 0, 'msgs': 0}

    for r in rows:
        msgs = safe_json(r['messages'], default=[], label='m')
        if not isinstance(msgs, list):
            continue
        settings = safe_json(r.get('settings'), default={}, label='s') or {}
        conv_model = (settings.get('model') or settings.get('preset') or '')
        cs = _safe_ts(r['created_at'] or r['updated_at'] or 0)
        ce = _safe_ts(r['updated_at'] or r['created_at'] or 0)
        n = len(msgs)
        title = (r['title'] or '')[:44] or 'Untitled'
        cid = r['id']

        for mi, m in enumerate(msgs):
            usage = m.get('usage')
            if not usage:
                continue
            ts = _safe_ts(m.get('timestamp', 0))
            if not ts:
                ts = cs + int((ce - cs) * mi / (n - 1)) if (cs and ce and n > 1) else cs
            if not ts or ts < start_ms:
                continue
            model = m.get('model') or m.get('preset') or conv_model
            prov = m.get('provider_id') or m.get('providerId') or ''
            cc = compute_cost(usage, model, prov)
            if not cc:
                continue
            cny = cc['costCny']
            u = normalize_usage(usage)
            day = _dt.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')

            by_day[day] += cny
            by_day_tok[day]['in'] += u['input']
            by_day_tok[day]['out'] += u['output']
            by_day_tok[day]['cw'] += u['cache_write']
            by_day_tok[day]['cr'] += u['cache_read']

            mkey = model or '(unknown)'
            bm = by_model[mkey]
            bm['cost'] += cny; bm['msgs'] += 1
            bm['in'] += u['input']; bm['out'] += u['output']
            bm['cw'] += u['cache_write']; bm['cr'] += u['cache_read']

            bc = by_conv[cid]
            bc['cost'] += cny; bc['name'] = title; bc['msgs'] += 1

            tot['cost'] += cny; tot['msgs'] += 1
            tot['in'] += u['input']; tot['out'] += u['output']
            tot['cw'] += u['cache_write']; tot['cr'] += u['cache_read']

    def pct(a, b):
        return (100.0 * a / b) if b else 0.0

    print('=' * 72)
    print(f'SPEND ANALYSIS — last {days_back} days — user {DEFAULT_USER_ID}')
    print(f'convs scanned: {len(rows)}   priced messages: {tot["msgs"]}')
    print('=' * 72)

    print('\n── PER DAY (CNY) ──')
    print(f'{"date":<12}{"CNY":>10}{"in(M)":>9}{"cw(M)":>9}{"cr(M)":>9}'
          f'{"out(M)":>9}{"cacheRd%":>9}')
    for day in sorted(by_day):
        t = by_day_tok[day]
        tin = t['in'] + t['cw'] + t['cr']
        print(f'{day:<12}{by_day[day]:>10.1f}{t["in"]/1e6:>9.1f}'
              f'{t["cw"]/1e6:>9.1f}{t["cr"]/1e6:>9.1f}{t["out"]/1e6:>9.1f}'
              f'{pct(t["cr"], tin):>8.0f}%')
    ndays = max(1, len(by_day))
    print(f'\n  avg/day: ¥{tot["cost"]/ndays:,.0f}   '
          f'total: ¥{tot["cost"]:,.0f}   (${tot["cost"]/7.1:,.0f})')

    print('\n── PER MODEL ──')
    print(f'{"model":<34}{"CNY":>9}{"share":>7}{"cacheRd%":>9}{"out%":>6}')
    for mk, v in sorted(by_model.items(), key=lambda x: -x[1]['cost']):
        tin = v['in'] + v['cw'] + v['cr']
        alltok = tin + v['out']
        print(f'{mk[:34]:<34}{v["cost"]:>9.0f}{pct(v["cost"], tot["cost"]):>6.0f}%'
              f'{pct(v["cr"], tin):>8.0f}%{pct(v["out"], alltok):>5.0f}%')

    print('\n── TOP 15 CONVERSATIONS BY COST ──')
    for cid, v in sorted(by_conv.items(), key=lambda x: -x[1]['cost'])[:15]:
        print(f'  ¥{v["cost"]:>8.0f}  {pct(v["cost"], tot["cost"]):>4.0f}%  '
              f'{v["msgs"]:>4}msg  {v["name"]}')

    print('\n── TOKEN-TYPE SPLIT (all-in) ──')
    tin = tot['in'] + tot['cw'] + tot['cr']
    grand = tin + tot['out']
    print(f'  uncached input : {tot["in"]/1e6:>10.1f}M  {pct(tot["in"], grand):>5.1f}%')
    print(f'  cache WRITE    : {tot["cw"]/1e6:>10.1f}M  {pct(tot["cw"], grand):>5.1f}%')
    print(f'  cache READ     : {tot["cr"]/1e6:>10.1f}M  {pct(tot["cr"], grand):>5.1f}%')
    print(f'  output         : {tot["out"]/1e6:>10.1f}M  {pct(tot["out"], grand):>5.1f}%')
    print(f'\n  CACHE-HIT RATE (cr / total input side): {pct(tot["cr"], tin):>5.1f}%')
    print(f'  (higher = better; cache_read bills at ~0.1x input)')


if __name__ == '__main__':
    main()
