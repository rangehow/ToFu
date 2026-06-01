#!/usr/bin/env python3
"""Post-process SWE-bench details/*.json to recompute cache_read_tokens and
cost_usd from raw_output's apiRounds, using the dual-convention extraction
(Anthropic cache_read_tokens + OpenAI prompt_tokens_details.cached_tokens).

The original runner only read the Anthropic fields, so MiniMax/GLM got
cache_read=0 on every round while paying full input price. This script
re-derives correct numbers in-place.

It ONLY touches rows where the raw_output is parseable — truncated
raw_output (the runner caps it at 50 KB) is left untouched and a count
is reported at the end so you know the upper bound on unfixed rows.

Usage::
    # Dry run — show what would change
    python3 debug/swebench_recompute_cache_costs.py \\
        --workdir swebench_rerun_workdir --dry-run

    # Apply fix
    python3 debug/swebench_recompute_cache_costs.py \\
        --workdir swebench_rerun_workdir
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional


# Pricing (matches ModelConfig table in swebench_runner.py)
PRICING = {
    'tofu-opus':    dict(pi=0.015, po=0.075, pcr=0.0015, pcw=0.01875),
    'tofu-minimax': dict(pi=0.001, po=0.002, pcr=0.0002, pcw=0.001),
    'tofu-glm':     dict(pi=0.002, po=0.008, pcr=0.0004, pcw=0.002),
    'cc-opus':      dict(pi=0.015, po=0.075, pcr=0.0015, pcw=0.01875),
    'cc-minimax':   dict(pi=0.001, po=0.002, pcr=0.0002, pcw=0.001),
    'cc-glm':       dict(pi=0.002, po=0.008, pcr=0.0004, pcw=0.002),
    'tofu-opus-notool':    dict(pi=0.015, po=0.075, pcr=0.0015, pcw=0.01875),
    'tofu-minimax-notool': dict(pi=0.001, po=0.002, pcr=0.0002, pcw=0.001),
    'tofu-glm-notool':     dict(pi=0.002, po=0.008, pcr=0.0004, pcw=0.002),
}


def _cost(in_tok, out_tok, cr, cw, p):
    return round(
        in_tok * p['pi'] / 1000
        + out_tok * p['po'] / 1000
        + cr * p['pcr'] / 1000
        + cw * p['pcw'] / 1000,
        6,
    )


def _try_parse_raw(raw: str) -> Optional[list]:
    """Return apiRounds list, or None if raw is unusable (truncated / empty)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data.get('apiRounds') or []


def _fallback_parse_rounds(raw: str) -> list[dict]:
    """When raw_output is truncated, try to pull out per-round usage objects
    via regex. Returns a list of partial usage dicts. Best-effort."""
    rounds = []
    # Find every usage {...} block — a balanced extractor.
    i = 0
    while True:
        m = re.search(r'"usage"\s*:\s*\{', raw[i:])
        if not m:
            break
        start = i + m.end() - 1  # position of '{'
        # Balance braces
        depth = 0
        j = start
        while j < len(raw):
            c = raw[j]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            break  # truncated
        snippet = raw[start:j + 1]
        try:
            u = json.loads(snippet)
            rounds.append({'usage': u})
        except json.JSONDecodeError:
            pass
        i = j + 1
    return rounds


def _harvest(apirounds: list) -> dict:
    """Sum per-round usage with dual-convention handling."""
    in_tok = out_tok = cr = cw = 0
    for rd in apirounds:
        ru = (rd.get('usage') or {}) if isinstance(rd, dict) else {}
        _pt = ru.get('prompt_tokens', 0) or 0
        _compl = ru.get('completion_tokens', 0) or 0
        _cr_a = ru.get('cache_read_tokens', 0) or 0
        _cw_a = ru.get('cache_write_tokens', 0) or 0
        _det = ru.get('prompt_tokens_details') or {}
        _cr_o = (_det.get('cached_tokens', 0) or 0) if isinstance(_det, dict) else 0

        _cr_combined = max(_cr_a, _cr_o)
        if _cr_o > 0 and _cr_a == 0:
            _uncached_prompt = max(_pt - _cr_o, 0)
        else:
            _uncached_prompt = _pt

        in_tok  += _uncached_prompt
        out_tok += _compl
        cr      += _cr_combined
        cw      += _cw_a
    return dict(input_tokens=in_tok, output_tokens=out_tok,
                cache_read_tokens=cr, cache_write_tokens=cw,
                num_turns=len(apirounds))


def process_one(fp: Path, dry_run: bool) -> tuple[str, dict]:
    """Process one details/*.json file. Returns (status, diff_dict)."""
    with open(fp) as f:
        d = json.load(f)
    inf = d.get('inference') or {}
    tool = d.get('tool', '')
    raw = inf.get('raw_output') or ''
    if not raw:
        return 'no_raw', {}
    pricing = PRICING.get(tool)
    if not pricing:
        return 'unknown_tool', {}

    apirounds = _try_parse_raw(raw)
    used_fallback = False
    if apirounds is None:
        apirounds = _fallback_parse_rounds(raw)
        if not apirounds:
            return 'truncated_unparseable', {}
        used_fallback = True

    h = _harvest(apirounds)
    new_cost = _cost(h['input_tokens'], h['output_tokens'],
                     h['cache_read_tokens'], h['cache_write_tokens'], pricing)

    old = dict(
        input_tokens=inf.get('input_tokens', 0),
        output_tokens=inf.get('output_tokens', 0),
        cache_read_tokens=inf.get('cache_read_tokens', 0),
        cache_write_tokens=inf.get('cache_write_tokens', 0),
        cost_usd=inf.get('cost_usd', 0.0),
        num_turns=inf.get('num_turns', 0),
    )
    new = dict(h, cost_usd=new_cost)

    if old == new:
        return 'unchanged', {}

    # Commit updates to file
    if not dry_run:
        inf.update(new)
        # If we used fallback parsing, rounds count may be partial;
        # only overwrite num_turns if we saw > 0 rounds in fallback.
        if used_fallback and h['num_turns'] == 0:
            inf['num_turns'] = old['num_turns']
        d['inference'] = inf
        with open(fp, 'w') as f:
            json.dump(d, f, indent=2, ensure_ascii=False)

    return ('fixed_fallback' if used_fallback else 'fixed'), {
        'file': fp.name,
        'tool': tool,
        'old': old,
        'new': new,
        'delta_cost': round(new['cost_usd'] - old['cost_usd'], 4),
        'delta_cr': new['cache_read_tokens'] - old['cache_read_tokens'],
        'delta_in': new['input_tokens'] - old['input_tokens'],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--verbose', '-v', action='store_true')
    args = ap.parse_args()

    details_dir = Path(args.workdir) / 'details'
    if not details_dir.is_dir():
        sys.exit(f'{details_dir} not found')

    files = sorted(details_dir.glob('*.json'))
    print(f'Scanning {len(files)} detail files in {details_dir}')

    by_tool = {}
    by_status = {}
    total_delta_cost = 0.0
    total_delta_cr = 0
    total_delta_in = 0

    for fp in files:
        status, diff = process_one(fp, dry_run=args.dry_run)
        by_status[status] = by_status.get(status, 0) + 1
        if diff:
            t = diff['tool']
            s = by_tool.setdefault(t, dict(n=0, dcost=0.0, dcr=0, din=0))
            s['n'] += 1
            s['dcost'] += diff['delta_cost']
            s['dcr']   += diff['delta_cr']
            s['din']   += diff['delta_in']
            total_delta_cost += diff['delta_cost']
            total_delta_cr   += diff['delta_cr']
            total_delta_in   += diff['delta_in']
            if args.verbose:
                print(f"  [{status}] {diff['file']}  Δcost={diff['delta_cost']:+.4f} "
                      f"Δcr={diff['delta_cr']:+,} Δin={diff['delta_in']:+,}")

    print()
    print('━' * 72)
    print(f'{"  STATUS COUNTS  ":_^72}')
    for st, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f'  {st:<28} {n:>6}')
    print()
    print(f'{"  PER-TOOL DELTAS  ":_^72}')
    print(f'  {"tool":<22}{"n_changed":>12}{"Δcost $":>14}{"Δcache_read":>16}{"Δinput":>14}')
    for t in sorted(by_tool):
        s = by_tool[t]
        print(f'  {t:<22}{s["n"]:>12}{s["dcost"]:>14.4f}{s["dcr"]:>16,}{s["din"]:>14,}')
    print()
    print(f'  TOTAL Δcost:       ${total_delta_cost:+,.2f}')
    print(f'  TOTAL Δcache_read: {total_delta_cr:+,}')
    print(f'  TOTAL Δinput:      {total_delta_in:+,}')
    print('━' * 72)
    if args.dry_run:
        print('\n  [DRY RUN] Re-run without --dry-run to commit changes.')


if __name__ == '__main__':
    main()
