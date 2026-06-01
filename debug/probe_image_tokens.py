#!/usr/bin/env python3
"""Probe: how many input tokens does the upstream charge for an image?

Sends a few images of different sizes to aws.claude-opus-4.7 via the
sankuai gateway and reports the `usage.prompt_tokens` / `usage.input_tokens`
returned by the API.

We compare:
  - Baseline: text-only request (no image)
  - Image A: the actual 442593-byte PNG that triggered the 413
              (uploads/1777431563133.png, if available)
  - Image B: a tiny 64x64 solid-color PNG (~1 KB)
  - Image C: a 1024x1024 solid-color PNG (~a few KB but big dimensions)

Then we subtract baseline tokens to isolate the per-image cost.

Usage:
    python3 debug/probe_image_tokens.py
"""
import base64
import io
import json
import os
import sys
import time

import requests

# ── Config from env vars (set TOFU_PROBE_BASE_URL / TOFU_PROBE_API_KEY) ──
BASE_URL = os.environ.get('TOFU_PROBE_BASE_URL', 'https://api.openai.com/v1')
API_KEY  = os.environ.get('TOFU_PROBE_API_KEY', '')
MODEL    = os.environ.get('TOFU_PROBE_MODEL', 'gpt-4o')
EXTRA_HEADERS = json.loads(os.environ.get('TOFU_PROBE_EXTRA_HEADERS', '{}'))


def make_png(width: int, height: int, color=(255, 0, 0)) -> bytes:
    """Build a solid-color PNG via PIL (fallback to tiny hardcoded PNG)."""
    try:
        from PIL import Image
        img = Image.new('RGB', (width, height), color)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except ImportError:
        # minimal 1x1 red PNG
        return base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z/C/'
            'HgAFhAJ/wlseKgAAAABJRU5ErkJggg=='
        )


def call(payload: dict) -> dict:
    """POST to /chat/completions, return parsed JSON (or error)."""
    url = BASE_URL.rstrip('/') + '/chat/completions'
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/json',
        **EXTRA_HEADERS,
    }
    body_bytes = json.dumps(payload).encode('utf-8')
    size_kb = len(body_bytes) / 1024
    t0 = time.time()
    try:
        resp = requests.post(url, headers=headers, data=body_bytes,
                             timeout=(60, 120))
        elapsed = time.time() - t0
        text_preview = resp.text[:500]
        try:
            data = resp.json()
        except Exception:
            data = {'_raw': text_preview}
        return {
            'status': resp.status_code,
            'elapsed': elapsed,
            'body_kb': size_kb,
            'data': data,
            'raw_preview': text_preview,
        }
    except Exception as e:
        return {
            'status': 'EXC',
            'elapsed': time.time() - t0,
            'body_kb': size_kb,
            'error': str(e),
        }


def build_msgs(image_bytes: bytes | None = None, prompt='Say only "ok".') -> list:
    """Build a minimal chat request, optionally with an image."""
    if image_bytes is None:
        return [{'role': 'user', 'content': prompt}]
    b64 = base64.b64encode(image_bytes).decode('ascii')
    return [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {
                'url': f'data:image/png;base64,{b64}',
            }},
        ],
    }]


def dump_usage(label: str, result: dict, extra: str = ''):
    print(f'── {label} ────────────────────────────────')
    print(f'  body_size    : {result.get("body_kb", 0):.1f} KB')
    print(f'  http_status  : {result.get("status")}')
    print(f'  elapsed      : {result.get("elapsed", 0):.2f}s')
    if extra:
        print(f'  note         : {extra}')
    data = result.get('data', {})
    usage = data.get('usage') or {}
    if usage:
        print(f'  usage        : {json.dumps(usage, ensure_ascii=False)}')
    else:
        err = data.get('error') or data.get('_raw') or result.get('error')
        print(f'  error/raw    : {str(err)[:300]}')
    print()


def main():
    print('=' * 60)
    print(f'Image token probe — {MODEL} @ {BASE_URL}')
    print('=' * 60)
    print()

    # Baseline (no image)
    print('[1/5] Baseline text-only request…')
    base_msgs = build_msgs(None)
    r_base = call({'model': MODEL, 'messages': base_msgs, 'max_tokens': 20})
    dump_usage('Baseline (text only)', r_base, 'no image')
    base_in = (r_base.get('data', {}).get('usage') or {}).get('prompt_tokens') or \
              (r_base.get('data', {}).get('usage') or {}).get('input_tokens') or 0

    # Tiny 64x64
    print('[2/5] Tiny 64×64 PNG (small dimensions)…')
    tiny = make_png(64, 64)
    r_tiny = call({'model': MODEL, 'messages': build_msgs(tiny),
                   'max_tokens': 20})
    dump_usage(f'Tiny 64×64 ({len(tiny)} bytes raw)', r_tiny)

    # 512x512
    print('[3/5] 512×512 PNG…')
    p512 = make_png(512, 512)
    r_512 = call({'model': MODEL, 'messages': build_msgs(p512),
                  'max_tokens': 20})
    dump_usage(f'512×512 ({len(p512)} bytes raw)', r_512)

    # 1024x1024
    print('[4/5] 1024×1024 PNG…')
    p1024 = make_png(1024, 1024, color=(0, 128, 255))
    r_1024 = call({'model': MODEL, 'messages': build_msgs(p1024),
                   'max_tokens': 20})
    dump_usage(f'1024×1024 ({len(p1024)} bytes raw)', r_1024)

    # The actual offending image if it still exists
    print('[5/5] The actual 442KB PNG from the 413 incident…')
    real_path = 'uploads/images/1777431563133.png'
    if os.path.isfile(real_path):
        with open(real_path, 'rb') as f:
            real_bytes = f.read()
        try:
            from PIL import Image
            dims = Image.open(io.BytesIO(real_bytes)).size
            dim_info = f'dims={dims[0]}×{dims[1]}'
        except Exception:
            dim_info = 'dims=?'
        r_real = call({'model': MODEL, 'messages': build_msgs(real_bytes),
                       'max_tokens': 20})
        dump_usage(f'Real uploads/1777431563133.png '
                   f'({len(real_bytes)} bytes raw, {dim_info})', r_real)
    else:
        print(f'  (skipped — {real_path} not found)')
        print()

    # Summary table
    print('=' * 60)
    print('SUMMARY — input_tokens charged per image (baseline subtracted)')
    print('=' * 60)
    for label, r, raw_sz in [
        ('tiny 64×64',    r_tiny, len(tiny)),
        ('512×512',       r_512,  len(p512)),
        ('1024×1024',     r_1024, len(p1024)),
    ]:
        u = (r.get('data', {}).get('usage') or {})
        tot = u.get('prompt_tokens') or u.get('input_tokens') or 0
        delta = tot - base_in if tot else 'n/a'
        print(f'  {label:20s}  raw={raw_sz:>7d}B  '
              f'prompt_tokens={tot}  (image cost ≈ {delta})')

    print()
    print('Compare against our estimator `_IMAGE_TOKENS_DEFAULT = 800`.')
    print('If the upstream charges much less (e.g. 300-1500) than the wire-size')
    print('byte count suggests (e.g. 442KB base64 ≈ 148K chars ≈ 37K "tokens"),')
    print('this confirms: the 413 is a gateway byte-limit issue, NOT an upstream')
    print('token-count issue.')


if __name__ == '__main__':
    main()
