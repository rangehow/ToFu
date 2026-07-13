#!/usr/bin/env python3
"""probe_flash_lite_availability.py — Is gemini-3.1-flash-lite-preview still alive?

Reads the LIVE provider config from ``data/config/server_config.json`` and
sends a minimal chat-completion to the gateway for EACH configured API key,
exactly the way the dispatcher would call it (Bearer key + provider
``extra_headers``). Prints a per-key verdict.

A model counts as AVAILABLE on a key when the gateway returns HTTP 200 (a
real completion) or HTTP 429 (rate-limited but the model exists on that key).
It counts as UNAVAILABLE when the gateway rejects the model itself — 400 /
404 / "model not found" / "no permission" style errors.

Usage:
    python debug/probe_flash_lite_availability.py
    python debug/probe_flash_lite_availability.py --model gemini-3.1-flash-lite-preview
    python debug/probe_flash_lite_availability.py --disable   # probe, then disable if dead

Exit code 0 = available on at least one key; 3 = dead on every key;
2 = could not run (no config / no keys).
"""

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.config_dir import config_path
from lib.json_store import update_json_atomic

DEFAULT_MODEL = 'gemini-3.1-flash-lite-preview'


def _load_provider():
    """Return the first enabled provider dict from server_config.json, or None."""
    cfg_file = config_path('server_config.json')
    if not os.path.isfile(cfg_file):
        print(f'❌ No server_config.json at {cfg_file}')
        return None
    with open(cfg_file) as f:
        cfg = json.load(f)
    for prov in cfg.get('providers', []):
        if prov.get('enabled', True) and prov.get('api_keys'):
            return prov
    print('❌ No enabled provider with api_keys found in server_config.json')
    return None


def _is_model_disabled_for_key(prov, model_id, key_idx):
    """Mirror the dispatcher: True if this (key, model) cell is turned off."""
    for m in prov.get('models', []):
        if not isinstance(m, dict) or m.get('model_id') != model_id:
            continue
        cell = (m.get('key_access') or {}).get(str(key_idx)) or {}
        if cell.get('enabled') is False:
            return True
        if model_id in set(cell.get('disabled_ids') or []):
            return True
    return False


def probe(base_url, api_key, model, extra_headers, timeout=30):
    """Send one minimal completion. Returns (available: bool, detail: str)."""
    url = f'{base_url.rstrip("/")}/chat/completions'
    headers = {'Content-Type': 'application/json',
               'Authorization': f'Bearer {api_key}'}
    headers.update(extra_headers or {})
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Reply with the single word: ok'}],
        'max_tokens': 16,
        'temperature': 0,
        'stream': False,
    }
    t0 = time.time()
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout,
                             proxies={'http': None, 'https': None})
    except Exception as e:
        return False, f'request error: {e}'
    ms = (time.time() - t0) * 1000
    if resp.status_code == 200:
        # A 200 means the model exists; the body preview is best-effort. This
        # gateway can return choices[0].message = null with
        # finish_reason='length' when the whole token budget is spent on
        # reasoning, so fall back to finish_reason (or the raw usage) rather
        # than treating a null message as "unparseable".
        preview = ''
        try:
            choice = (resp.json().get('choices') or [{}])[0]
            msg = choice.get('message') or {}
            content = (msg.get('content') or '').strip()
            preview = content[:60] if content else f'[{choice.get("finish_reason") or "no-content"}]'
        except Exception as e:
            preview = f'<200 body parse error: {e}>'
        return True, f'HTTP 200 {ms:.0f}ms "{preview}"'
    if resp.status_code == 429:
        return True, f'HTTP 429 rate-limited (model exists) {ms:.0f}ms'
    return False, f'HTTP {resp.status_code} {ms:.0f}ms: {resp.text[:240]}'


def disable_model_for_all_keys(model_id, num_keys):
    """Add ``model_id`` to every key cell's ``disabled_ids`` in the live config.

    Locked read-modify-write (same path/lock the Settings UI uses). Returns
    the number of (key, model) cells newly disabled.
    """
    cfg_file = config_path('server_config.json')
    changed = {'n': 0}

    def _mutate(cfg):
        for prov in cfg.get('providers', []):
            if not (prov.get('enabled', True) and prov.get('api_keys')):
                continue
            for m in prov.get('models', []):
                if not isinstance(m, dict) or m.get('model_id') != model_id:
                    continue
                ka = m.setdefault('key_access', {})
                for idx in range(len(prov['api_keys'])):
                    cell = ka.setdefault(str(idx), {})
                    dis = cell.setdefault('disabled_ids', [])
                    if model_id not in dis:
                        dis.append(model_id)
                        changed['n'] += 1
        return cfg

    update_json_atomic(cfg_file, _mutate, default={})
    return changed['n']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--disable', action='store_true',
                    help='If the model is dead on every key, disable it in server_config.json.')
    args = ap.parse_args()
    model = args.model

    prov = _load_provider()
    if not prov:
        sys.exit(2)

    base_url = prov['base_url']
    keys = prov['api_keys']
    extra_headers = prov.get('extra_headers') or {}
    print(f'Provider: {prov.get("name") or prov.get("id")}  base={base_url}  keys={len(keys)}')
    print(f'Probing model: {model}\n')

    any_available = False
    for idx, key in enumerate(keys):
        cfg_disabled = _is_model_disabled_for_key(prov, model, idx)
        tag = '  (config-disabled)' if cfg_disabled else ''
        avail, detail = probe(base_url, key, model, extra_headers)
        mark = '✅ AVAILABLE' if avail else '❌ UNAVAILABLE'
        print(f'  key #{idx} (...{key[-4:]}){tag}: {mark} — {detail}')
        if avail:
            any_available = True

    print()
    if any_available:
        print(f'VERDICT: {model} is STILL AVAILABLE on at least one key.')
        sys.exit(0)

    print(f'VERDICT: {model} is UNAVAILABLE on every configured key — should be disabled.')
    if args.disable:
        n = disable_model_for_all_keys(model, len(keys))
        print(f'DISABLED: added "{model}" to disabled_ids for {n} (key, model) cell(s) '
              f'in server_config.json. Run reload_config()/restart to apply.')
    else:
        print('Re-run with --disable to turn it off in server_config.json.')
    sys.exit(3)


if __name__ == '__main__':
    main()
