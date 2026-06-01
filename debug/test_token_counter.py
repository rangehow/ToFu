"""Smoke test for lib/token_counter/ — runs all backends against real
DB conversations and reports accuracy vs. upstream ground truth.

Usage:
    python debug/test_token_counter.py
    python debug/test_token_counter.py --conv mo4fr5xeup9ogp
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.log import get_logger  # noqa: E402
from lib.token_counter import (  # noqa: E402
    count_text,
    count_tokens,
    record_usage,
)
from lib.token_counter.heuristic import cheap_estimate  # noqa: E402
from lib.token_counter.resolver import force_backend  # noqa: E402

logger = get_logger(__name__)


def _load_provider_creds():
    cfg_path = ROOT / 'data' / 'config' / 'server_config.json'
    cfg = json.loads(cfg_path.read_text())
    sk = next((p for p in cfg.get('providers', []) if p.get('id') == 'sankuai'), None)
    if not sk:
        return None, None
    keys = sk.get('api_keys') or sk.get('keys') or []
    if not keys:
        return None, None
    key = keys[0] if isinstance(keys[0], str) else keys[0].get('key')
    return sk.get('base_url'), key


def _load_conv_messages(conv_id: str):
    import psycopg2
    import psycopg2.extras
    dsn = os.environ.get(
        'TOFU_PROBE_PG_DSN',
        'host=127.0.0.1 port=15439 dbname=chatui',
    )
    conn = psycopg2.connect(dsn, connect_timeout=5)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT messages, settings FROM conversations WHERE id=%s',
                (conv_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise SystemExit(f'conv {conv_id} not found')

    raw_msgs = row['messages']
    model = (row['settings'] or {}).get('model') or 'aws.claude-opus-4.7'

    api_msgs = []
    for m in raw_msgs:
        role = m.get('role')
        if role not in ('user', 'assistant'):
            continue
        parts = []
        if isinstance(m.get('content'), str):
            parts.append(m['content'])
        if isinstance(m.get('thinking'), str):
            parts.append(m['thinking'])
        for tr in m.get('toolRounds') or []:
            for k in ('thinking', 'content', 'toolArgs', 'toolContent'):
                v = tr.get(k)
                if isinstance(v, str):
                    parts.append(v)
        for ar in m.get('apiRounds') or []:
            for k in ('content', 'thinking'):
                v = ar.get(k)
                if isinstance(v, str):
                    parts.append(v)
        if not parts:
            continue
        api_msgs.append({'role': role, 'content': '\n\n'.join(parts)})
    return api_msgs, model


def _fmt(n):
    if n is None:
        return '   n/a   '
    return f'{n:>9,}'


def test_count_text_samples():
    print('\n=== count_text samples ===')
    for s in [
        'hello world',
        '你好世界',
        '混合 text 与中文 and numbers 12345',
        'a' * 1000,
        '中' * 1000,
    ]:
        n = count_text(s, model='gpt-4o')
        preview = s[:30] + ('…' if len(s) > 30 else '')
        print(f'  len={len(s):>5}  tokens={n:>5}  preview={preview!r}')


def test_forced_modes():
    print('\n=== forced-mode resolver ===')
    msgs = [{'role': 'user', 'content': '测试一下 mode override 的行为'}]
    for mode in ('heuristic', 'tiktoken', 'anthropic_api', 'deepseek', 'hf',
                 'usage_cache', 'auto'):
        out = count_tokens(msgs, model='aws.claude-opus-4.7', mode=mode)
        print(f'  mode={mode:<16s}  tokens={out["tokens"]:>5}  '
              f'method={out["method"]}  conf={out["confidence"]}')


def test_resolver_order():
    print('\n=== resolver priority lists (model → backend order) ===')
    models = [
        'aws.claude-opus-4.7', 'gemini-3-flash-preview',
        'gpt-4o', 'deepseek-v4-pro', 'qwen3.5-plus',
        'glm-5.1', 'MiniMax-M2.7', 'Doubao-Seed-2.0-pro',
        'Llama-3', 'some-unknown-model',
    ]
    from lib.token_counter.resolver import resolve
    for m in models:
        chain = [c.name for c in resolve(m)]
        print(f'  {m:<28s} → {" → ".join(chain)}')


def test_usage_cache():
    print('\n=== usage_cache hot path ===')
    msgs = [
        {'role': 'user', 'content': '你好'},
        {'role': 'assistant', 'content': '你好！有什么可以帮你的？'},
        {'role': 'user', 'content': 'tell me about transformers.'},
    ]
    out0 = count_tokens(msgs, model='gpt-4o', conv_id='fake-conv-1')
    print(f'  before record: method={out0["method"]:<12s} tokens={out0["tokens"]}')
    # Simulate the server recording a real usage number
    record_usage('fake-conv-1', prompt_tokens=4242, model='gpt-4o',
                 message_count=3, messages=msgs)
    # Now add a new user message (append-only)
    msgs.append({'role': 'assistant', 'content': 'Transformers are...'})
    msgs.append({'role': 'user', 'content': '举个例子'})
    out1 = count_tokens(msgs, model='gpt-4o', conv_id='fake-conv-1')
    print(f'  after  record: method={out1["method"]:<12s} tokens={out1["tokens"]}  '
          f'(expect ~4242 + small delta)')
    # Changed prefix → should fall through
    msgs[0] = {'role': 'user', 'content': 'CHANGED'}
    out2 = count_tokens(msgs, model='gpt-4o', conv_id='fake-conv-1')
    print(f'  prefix changed: method={out2["method"]:<12s} tokens={out2["tokens"]}  '
          f'(usage_cache should decline)')


def test_known_failure(conv_id, bedrock_truth):
    print(f'\n=== Conversation {conv_id} ===')
    base, key = _load_provider_creds()
    print(f'provider base: {base}')
    msgs, model = _load_conv_messages(conv_id)
    print(f'model: {model}   messages: {len(msgs)}')

    t = time.time(); out = force_backend('heuristic')[0].count(msgs, model=model)
    dt_h = (time.time() - t) * 1000
    print(f'heuristic (CJK-aware): {_fmt(out)}   ({dt_h:.0f} ms)')

    t = time.time(); out_tt = force_backend('tiktoken')[0].count(msgs, model=model)
    dt_tt = (time.time() - t) * 1000
    print(f'tiktoken            : {_fmt(out_tt)}   ({dt_tt:.0f} ms)')

    t = time.time()
    out_api = force_backend('anthropic_api')[0].count(
        msgs, model=model, api_base_url=base, api_key=key)
    dt_api = (time.time() - t) * 1000
    print(f'anthropic API        : {_fmt(out_api)}   ({dt_api:.0f} ms)')

    t = time.time()
    out_auto = count_tokens(msgs, model=model, api_base_url=base, api_key=key,
                            context_limit=1_000_000)
    dt_auto = (time.time() - t) * 1000
    print(f"count_tokens (auto)  : {_fmt(out_auto['tokens'])}   "
          f"method={out_auto['method']}  conf={out_auto['confidence']}  "
          f"elapsed={out_auto['elapsed_ms']} ms")

    if out_api is not None:
        print(f'\nAccuracy vs API ground truth ({out_api:,}):')
        for lbl, n in (('heuristic', out), ('tiktoken', out_tt)):
            if n is None: continue
            print(f'  {lbl:<10s}: {n:>9,}  Δ {(n - out_api)/out_api*100:+6.1f}%')

    if bedrock_truth:
        print(f'\nBedrock actual at failure: {bedrock_truth:,}'
              '\n(pre-L1 input here is raw DB; real wire is smaller post-compaction)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--conv', default='mo4fr5xeup9ogp')
    ap.add_argument('--truth', type=int, default=1310784)
    ap.add_argument('--skip-db', action='store_true')
    args = ap.parse_args()

    test_count_text_samples()
    test_forced_modes()
    test_resolver_order()
    test_usage_cache()
    if not args.skip_db:
        test_known_failure(args.conv, args.truth)


if __name__ == '__main__':
    main()
