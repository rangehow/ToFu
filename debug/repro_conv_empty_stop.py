#!/usr/bin/env python3
"""One-shot probe: replay ONE conversation's REAL built request to the gateway.

Unlike ``debug/repro_stream_anomaly.py`` (synthetic tiny prompts), this rebuilds
the EXACT wire body the orchestrator would send for a given conversation —
``build_api_messages_from_db`` → ``build_body`` — and streams it once against the
raw gateway, printing the raw SSE outcome. The point is to answer, empirically:

    Is the gateway returning a genuine content-policy block, or a transient
    empty / stub completion (finish=stop, 0 content) on a very large prompt?

The heuristic in lib/tasks_pkg/llm_fallback.py labels a round-0 empty stop as
``content_filter`` (terminal, NO retry) whenever the stream layer did NOT flag a
``_stream_anomaly``. This probe reports the exact signals that decision keys on:
finish_reason, chunk count, whether content was whitespace-only, prompt/
completion tokens, elapsed.

Usage:
    python3 debug/repro_conv_empty_stop.py <conv_id> [--model aws.claude-opus-4.8] [--n 1]
"""
import argparse
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_provider():
    cfg_path = os.path.join(ROOT, 'data/config/server_config.json')
    with open(cfg_path) as f:
        cfg = json.load(f)
    p = cfg['providers'][0]
    keys = p.get('api_keys') or p.get('keys') or []
    if not keys:
        sys.exit('ERROR: no api_keys in server_config.json')
    k = keys[0]
    api_key = k['key'] if isinstance(k, dict) else k
    return p['base_url'].rstrip('/'), api_key, p.get('name', '?')


def one_call(base_url, api_key, body, timeout=(60, 600)):
    url = base_url + '/chat/completions'
    headers = {
        'Authorization': 'Bearer ' + api_key,
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
    }
    t0 = time.time()
    chunks = 0
    saw_done = False
    saw_finish = False
    finish_reason = None
    content_chars = 0
    content_ws_only = True   # True while every content delta seen is whitespace
    thinking_chars = 0
    parse_errors = 0
    trace = ''
    err = ''
    tail_lines = []          # last ~40 raw SSE lines for forensics
    usage_final = {}
    try:
        resp = requests.post(url, headers=headers, json=body,
                             stream=True, timeout=timeout)
        trace = resp.headers.get('M-TraceId', '')
        if resp.status_code != 200:
            return {
                'verdict': 'http_error', 'status': resp.status_code,
                'body': resp.text[:600], 'trace': trace,
                'elapsed': round(time.time() - t0, 2),
            }
        for line in resp.iter_lines(decode_unicode=True):
            if line is None:
                continue
            tail_lines.append(line)
            if len(tail_lines) > 40:
                tail_lines.pop(0)
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                saw_done = True
                break
            if not data:
                continue
            chunks += 1
            try:
                ch = json.loads(data)
            except Exception:
                parse_errors += 1
                continue
            if ch.get('usage'):
                usage_final = ch['usage']
            choices = ch.get('choices') or []
            if choices:
                delta = choices[0].get('delta', {})
                fr = choices[0].get('finish_reason')
                if fr:
                    saw_finish = True
                    finish_reason = fr
                c = delta.get('content') or ''
                if c:
                    content_chars += len(c)
                    if c.strip():
                        content_ws_only = False
                thinking_chars += len(
                    delta.get('reasoning_content') or delta.get('thinking') or '')
        resp.close()
    except requests.Timeout as e:
        err = f'Timeout: {e}'
    except requests.RequestException as e:
        err = f'RequestException: {e}'
    except Exception as e:
        err = f'Exception: {type(e).__name__}: {e}'

    elapsed = round(time.time() - t0, 2)
    # Mirror lib/llm/_sse_core.py:813 empty_stop / stream_anomaly detection.
    empty_stop = bool(saw_finish and finish_reason == 'stop'
                      and content_chars == 0 and chunks > 0)
    verdict = 'ok'
    if err:
        verdict = 'transport_error'
    elif not saw_done:
        verdict = 'missing_done'
    elif not saw_finish and chunks > 0:
        verdict = 'missing_finish_reason'
    elif empty_stop:
        verdict = 'empty_stop'
    elif finish_reason == 'stop' and content_chars > 0 and content_ws_only:
        # content arrived but is ALL whitespace — the exact case that slips
        # past _sse_core's `not content` guard (content is truthy) yet fails
        # llm_fallback's `.strip()` check → mislabeled content_filter, no retry.
        verdict = 'whitespace_only_stop'
    return {
        'verdict': verdict,
        'finish_reason': finish_reason,
        'chunks': chunks,
        'content_chars': content_chars,
        'content_ws_only': content_ws_only if content_chars else None,
        'thinking_chars': thinking_chars,
        'saw_done': saw_done,
        'empty_stop_flag': empty_stop,
        'prompt_tokens': usage_final.get('prompt_tokens'),
        'completion_tokens': usage_final.get('completion_tokens'),
        'usage': usage_final,
        'trace': trace,
        'parse_errors': parse_errors,
        'elapsed': elapsed,
        'err': err,
        'tail_lines': tail_lines,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('conv_id')
    ap.add_argument('--model', default='aws.claude-opus-4.8')
    ap.add_argument('--n', type=int, default=1)
    ap.add_argument('--system', default='', help='system prompt override')
    args = ap.parse_args()

    # Build the SAME messages the orchestrator would send for this conv.
    from lib.llm.body import build_body
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db

    msgs = build_api_messages_from_db(args.conv_id, {'systemPrompt': args.system})
    if msgs is None:
        sys.exit(f'ERROR: conv {args.conv_id} not found / unbuildable')

    body = build_body(
        args.model, msgs,
        max_tokens=128000, temperature=1.0,
        thinking_enabled=True, preset='opus', thinking_depth='medium',
        tools=None, stream=True, provider_id='sankuai',
    )
    n_msgs = len(body['messages'])
    approx_chars = sum(
        len(m.get('content')) if isinstance(m.get('content'), str) else len(json.dumps(m.get('content', '')))
        for m in body['messages'])
    base_url, api_key, provider = _load_provider()
    print(f'Conv:      {args.conv_id}')
    print(f'Provider:  {provider}  ({base_url})')
    print(f'Model:     {args.model}')
    print(f'Built req: {n_msgs} messages, ~{approx_chars:,} chars (~{approx_chars//3:,} tok est), '
          f'max_tokens={body.get("max_tokens")}, thinking={body.get("thinking")}')
    print('=' * 88)

    for i in range(args.n):
        r = one_call(base_url, api_key, body)
        v = r.get('verdict')
        flag = '✓' if v == 'ok' else '⚠'
        print(f'[{i}] {flag} verdict={v}  finish={r.get("finish_reason")}  '
              f'chunks={r.get("chunks")}  content={r.get("content_chars")} '
              f'(ws_only={r.get("content_ws_only")})  thinking={r.get("thinking_chars")}  '
              f'saw_done={r.get("saw_done")}  empty_stop={r.get("empty_stop_flag")}')
        print(f'    prompt_tokens={r.get("prompt_tokens")}  completion_tokens={r.get("completion_tokens")}  '
              f'elapsed={r.get("elapsed")}s  trace={r.get("trace")}')
        if r.get('err'):
            print(f'    ERR: {r["err"]}')
        if r.get('status'):
            print(f'    HTTP {r.get("status")}: {r.get("body")}')
        if v != 'ok':
            print('    --- last SSE lines ---')
            for ln in (r.get('tail_lines') or [])[-12:]:
                print('      ' + ln[:200])
        if i < args.n - 1:
            time.sleep(2)

    out = os.path.join(ROOT, 'logs/repro_conv_empty_stop.json')
    with open(out, 'w') as f:
        # drop tail_lines from the last result into the dump too
        json.dump({'conv': args.conv_id, 'model': args.model,
                   'built_messages': n_msgs, 'approx_chars': approx_chars,
                   'last_result': r}, f, indent=2, ensure_ascii=False)
    print(f'\nFull dump: {out}')


if __name__ == '__main__':
    main()
