#!/usr/bin/env python3
"""Test RPM (rate-per-minute) quota for gpt-5.4 and gpt-5.4-mini on all available API keys.

Sends rapid lightweight requests and checks for 429 (rate limit) responses.
"""

import sys, os, time, requests, json
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from lib import LLM_API_KEYS, LLM_BASE_URL

API_URL = f'{LLM_BASE_URL}/chat/completions'
_NO_PROXY = {'no_proxy': '*'}

MODELS = ['gpt-5.4', 'gpt-5.4-mini']
# Test with ALL available keys (we want to know per-key RPM)
KEYS = LLM_API_KEYS[:2]  # first two keys

# Number of rapid requests per (key, model) pair
RPM_PROBE_COUNT = 5
# Delay between requests (seconds) — keep short to probe rate limits
DELAY_BETWEEN = 0.3


def probe_one(key_idx: int, key: str, model: str, req_num: int):
    """Send one lightweight request. Returns dict with result info."""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
    }
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Say "hi" in one word.'}],
        'max_tokens': 8,
        'temperature': 0,
        'stream': False,
    }

    t0 = time.time()
    try:
        r = requests.post(API_URL, headers=headers, json=body,
                          timeout=(10, 30), proxies=_NO_PROXY)
        latency_ms = (time.time() - t0) * 1000
        status = r.status_code

        result = {
            'key_idx': key_idx,
            'model': model,
            'req_num': req_num,
            'status': status,
            'latency_ms': round(latency_ms, 1),
        }

        if status == 200:
            data = r.json()
            usage = data.get('usage', {})
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            result['content'] = content[:50]
            result['usage'] = usage
            result['ok'] = True
        elif status == 429:
            # Rate limited!
            error_body = r.text[:300]
            result['error'] = f'RATE LIMITED (429): {error_body}'
            result['ok'] = False
            # Try to extract retry-after
            retry_after = r.headers.get('Retry-After', r.headers.get('x-ratelimit-reset-requests', ''))
            result['retry_after'] = retry_after
        elif status == 401:
            result['error'] = f'AUTH FAILED (401): {r.text[:200]}'
            result['ok'] = False
        elif status == 404:
            result['error'] = f'MODEL NOT FOUND (404): {r.text[:200]}'
            result['ok'] = False
        else:
            result['error'] = f'HTTP {status}: {r.text[:200]}'
            result['ok'] = False

        # Extract rate limit headers
        rl_headers = {}
        for h in ['x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
                   'x-ratelimit-limit-tokens', 'x-ratelimit-remaining-tokens',
                   'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens']:
            v = r.headers.get(h)
            if v:
                rl_headers[h] = v
        result['rate_limit_headers'] = rl_headers

        return result

    except requests.Timeout:
        return {
            'key_idx': key_idx, 'model': model, 'req_num': req_num,
            'status': 0, 'ok': False,
            'error': f'TIMEOUT after {(time.time()-t0)*1000:.0f}ms',
            'latency_ms': round((time.time()-t0)*1000, 1),
            'rate_limit_headers': {},
        }
    except Exception as e:
        return {
            'key_idx': key_idx, 'model': model, 'req_num': req_num,
            'status': 0, 'ok': False,
            'error': f'ERROR: {str(e)[:200]}',
            'latency_ms': round((time.time()-t0)*1000, 1),
            'rate_limit_headers': {},
        }


def test_key_model(key_idx: int, key: str, model: str):
    """Send RPM_PROBE_COUNT requests for one (key, model) pair sequentially."""
    results = []
    masked_key = f'{key[:8]}...{key[-4:]}' if len(key) > 12 else '***'
    print(f'\n  🔑 Key {key_idx} ({masked_key}) × {model}')
    print(f'     Sending {RPM_PROBE_COUNT} requests with {DELAY_BETWEEN}s delay...')

    for i in range(RPM_PROBE_COUNT):
        r = probe_one(key_idx, key, model, i + 1)
        results.append(r)

        status_icon = '✅' if r['ok'] else '❌'
        rl = r.get('rate_limit_headers', {})
        rl_info = ''
        if rl.get('x-ratelimit-limit-requests'):
            rl_info = (f" | RPM limit={rl.get('x-ratelimit-limit-requests', '?')}"
                       f" remaining={rl.get('x-ratelimit-remaining-requests', '?')}")
        if rl.get('x-ratelimit-limit-tokens'):
            rl_info += (f" | TPM limit={rl.get('x-ratelimit-limit-tokens', '?')}"
                        f" remaining={rl.get('x-ratelimit-remaining-tokens', '?')}")

        if r['ok']:
            print(f'     {status_icon} #{i+1} {r["status"]} {r["latency_ms"]:>7.0f}ms '
                  f'"{r.get("content", "")[:30]}"{rl_info}')
        else:
            print(f'     {status_icon} #{i+1} {r.get("error", "unknown")[:100]}{rl_info}')
            # If model not found or auth failed, skip remaining
            if r.get('status') in (401, 404):
                print(f'     ⏭️  Skipping remaining requests (status={r["status"]})')
                break

        if i < RPM_PROBE_COUNT - 1:
            time.sleep(DELAY_BETWEEN)

    return results


def main():
    print(f'═══════════════════════════════════════════════════════════')
    print(f'  RPM Quota Test for gpt-5.4 / gpt-5.4-mini')
    print(f'  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'  Endpoint: {LLM_BASE_URL}')
    print(f'  Keys: {len(KEYS)} available')
    print(f'  Models: {MODELS}')
    print(f'  Probes per (key, model): {RPM_PROBE_COUNT}')
    print(f'═══════════════════════════════════════════════════════════')

    if not KEYS:
        print('❌ No API keys configured! Set LLM_API_KEYS env var or configure in Settings.')
        sys.exit(1)

    all_results = {}
    t_start = time.time()

    for model in MODELS:
        print(f'\n{"─"*60}')
        print(f'  Model: {model}')
        print(f'{"─"*60}')
        for ki, key in enumerate(KEYS):
            results = test_key_model(ki, key, model)
            all_results[(ki, model)] = results

    # ── Summary ──
    elapsed = time.time() - t_start
    print(f'\n\n{"═"*70}')
    print(f'  📊 SUMMARY')
    print(f'{"═"*70}')
    print(f'  {"Key":>5}  {"Model":<18}  {"OK":>3}/{RPM_PROBE_COUNT}  {"429s":>4}  {"RPM Limit":>10}  {"TPM Limit":>10}  {"Avg Lat":>8}')
    print(f'  {"─"*5}  {"─"*18}  {"─"*5}  {"─"*4}  {"─"*10}  {"─"*10}  {"─"*8}')

    for model in MODELS:
        for ki in range(len(KEYS)):
            results = all_results.get((ki, model), [])
            ok_count = sum(1 for r in results if r['ok'])
            rate_429 = sum(1 for r in results if r.get('status') == 429)
            lats = [r['latency_ms'] for r in results if r['ok']]
            avg_lat = sum(lats) / len(lats) if lats else 0

            # Get RPM/TPM from last successful response headers
            rpm_limit = '—'
            tpm_limit = '—'
            for r in reversed(results):
                rl = r.get('rate_limit_headers', {})
                if rl.get('x-ratelimit-limit-requests'):
                    rpm_limit = rl['x-ratelimit-limit-requests']
                if rl.get('x-ratelimit-limit-tokens'):
                    tpm_limit = rl['x-ratelimit-limit-tokens']
                if rpm_limit != '—':
                    break

            print(f'  Key {ki}  {model:<18}  {ok_count:>3}/{len(results)}  {rate_429:>4}  {rpm_limit:>10}  {tpm_limit:>10}  {avg_lat:>7.0f}ms')

    print(f'\n  ⏱  Total time: {elapsed:.1f}s')
    print(f'{"═"*70}\n')


if __name__ == '__main__':
    main()
