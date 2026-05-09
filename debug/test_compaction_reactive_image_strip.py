#!/usr/bin/env python3
"""Smoke test for reactive_compact token-over image-strip trigger.

Regression guard for the 2026-05-04 patch that widened reactive_compact's
Phase 0 trigger condition. Before the patch, Phase 0 ran only when
``wire_before > _WIRE_BYTE_SOFT_LIMIT`` — token-over-limit requests
dominated by large base64 images (which can fit under the 4MB wire
soft limit while blowing the 1M token context) never triggered image
stripping, and the request stayed wedged.

This test builds a synthetic messages list where:
  - token count > 0.95 × context_limit (triggers the new token-over branch)
  - wire bytes < _WIRE_BYTE_SOFT_LIMIT (does NOT trigger the wire branch)
  - 3 image_url blocks exist; oldest should be replaced with placeholder.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.tasks_pkg.compaction import (  # noqa: E402
    _WIRE_BYTE_SOFT_LIMIT,
    _estimate_total_tokens,
    _estimate_wire_bytes,
    _get_context_limit,
    reactive_compact,
)

PLACEHOLDER = '[image removed during emergency compaction'


def _img_block(size: int = 512) -> dict:
    # Small fake base64 payload — keeps wire size trivially under 4MB.
    data = 'A' * size
    return {'type': 'image_url',
            'image_url': {'url': f'data:image/png;base64,{data}'}}


def main() -> int:
    # Pick a small-context model so we can blow the 0.95× threshold with
    # modest text — qwen has a 128k limit in _get_context_limit.
    task = {'id': 'smoke-test-0001', 'convId': 'smoke',
            'config': {'model': 'qwen'}}
    context_limit = _get_context_limit(task)
    token_threshold = int(context_limit * 0.95)

    # 500k chars of text ≈ 125k tokens — just over the 121.6k threshold
    # for qwen's 128k context window.
    big_text = 'x' * 500_000

    messages = [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': big_text},
            _img_block(1024),  # oldest image — should get stripped
        ]},
        {'role': 'assistant', 'content': 'ok'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'follow-up 1'},
            _img_block(512),  # tail image 1 — keep
        ]},
        {'role': 'assistant', 'content': 'ok2'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'follow-up 2'},
            _img_block(512),  # tail image 2 — keep
        ]},
    ]

    tokens_before = _estimate_total_tokens(messages)
    wire_before = _estimate_wire_bytes(messages)

    assert tokens_before > token_threshold, (
        f'setup invariant violated: tokens_before={tokens_before} '
        f'must exceed threshold={token_threshold}')
    assert wire_before < _WIRE_BYTE_SOFT_LIMIT, (
        f'setup invariant violated: wire_before={wire_before} '
        f'must be under soft limit={_WIRE_BYTE_SOFT_LIMIT}')

    print(f'[setup] context_limit={context_limit} threshold={token_threshold}')
    print(f'[setup] tokens_before={tokens_before} '
          f'wire_before={wire_before} (<{_WIRE_BYTE_SOFT_LIMIT})')

    reactive_compact(messages, task=task)

    # Index-independent assertions: reactive_compact may drop/merge cold
    # turns after Phase 0, so we can't rely on specific message indices.
    def _iter_blocks(msgs):
        for m in msgs:
            c = m.get('content')
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        yield b

    blocks = list(_iter_blocks(messages))
    remaining_images = [
        b for b in blocks
        if b.get('type') == 'image_url'
    ]
    placeholder_hits = [
        b for b in blocks
        if b.get('type') == 'text' and PLACEHOLDER in (b.get('text') or '')
    ]

    # Phase 0 must have stripped at least the one oldest image and replaced
    # it with a placeholder text block — regardless of whether downstream
    # phases later dropped the containing message.
    assert placeholder_hits or (len(remaining_images) < 3), (
        f'expected Phase 0 to strip oldest image; '
        f'remaining_images={len(remaining_images)} placeholders={len(placeholder_hits)}')

    # The two tail images (in the last-2 user turns) should survive —
    # _WIRE_IMAGE_KEEP_TAIL=2 guarantees them unless a later phase dropped
    # those messages entirely, which should not happen because they are
    # the live/hot tail that force_compact preserves.
    assert len(remaining_images) >= 2, (
        f'tail images unexpectedly stripped: remaining_images={len(remaining_images)}')

    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
