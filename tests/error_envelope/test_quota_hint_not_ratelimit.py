#!/usr/bin/env python3
"""tests/error_envelope/test_quota_hint_not_ratelimit.py — a 402 must never be
advised to wait for a 429 window.

The quota hint used to share the rate-limit copy ("wait a few minutes for the
API rate-limit window to reset"). A provider billing stop is not a per-minute
episode: nothing recovers in minutes, so that advice sends the owner down the
wrong recovery path (2026-11-09: the credit-exhausted TAUD Worker turn).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


def test_quota_hint_names_top_up_recovery_not_a_rate_limit_window():
    from lib.error_envelope import make_envelope

    envelope = make_envelope('quota', model='kimi-k3.1-pro')
    hint = envelope['hint']
    assert 'wait a few minutes for the API rate-limit window' not in hint
    assert '稍等几分钟' not in hint
    assert 'top it up and re-enable' in hint
    assert '充值后重新启用' in hint
    assert 'rate-limit window' in hint, 'the hint explicitly says a window does NOT help'
    assert 'does NOT recover by waiting for a rate-limit window' in hint


def test_ratelimit_keeps_the_window_advice():
    from lib.error_envelope import make_envelope

    hint = make_envelope('ratelimit')['hint']
    assert 'wait a few minutes for the API rate-limit window to reset' in hint
    assert '稍等几分钟' in hint


def test_localized_quota_hints_drop_the_window_wait_everywhere():
    import json

    for code in ('zh', 'en', 'ja', 'ko', 'fr', 'de', 'ru'):
        with open(f'frontend/src/i18n/locales/{code}.json', encoding='utf-8') as handle:
            hint = json.load(handle)['err.k.quota.hint']
        for stale in ('wait a few minutes for the API rate-limit window to reset',
                      '稍等几分钟', '数分待ってから再試行', '몇 분 기다린 후 다시 시도',
                      'patienter quelques minutes', 'warten Sie einige Minuten',
                      'подождите несколько минут'):
            assert stale not in hint, f'{code}: a 402 still tells the owner to wait'

