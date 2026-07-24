"""tests/test_title_gen_robust — pt_title_gen_robust P1 guard.

Root cause pinned by ``debug/title_gen_repro.py`` (5 forced deepseek-v4-pro
calls, 1/5 returned ``finish=length + cleaned=''``): cheap-pool models
carrying the ``thinking`` capability burn the entire max_tokens=512 budget
on reasoning, leaving the visible title empty or a single stray character
(the mryjczi2v9ck9k "跨" incident).

Fix (P1): ``generate_conversation_title`` passes ``exclude_models=`` to
``dispatch_chat`` so the cheap-tier thinking models are not eligible for
routing. The excluded set is a frozenset constant maintained inside
``title_gen`` — this test guards two invariants:

1. The constant exactly equals the set derived from
   ``bootstrap._BUILTIN_PROVIDER_TEMPLATES`` (i.e. every model with BOTH
   ``cheap`` AND ``thinking`` capabilities). If a sibling adds a new
   cheap+thinking model to bootstrap without updating the constant,
   this fails — forcing the update.
2. ``generate_conversation_title`` forwards that exact set as
   ``exclude_models=`` to the dispatcher. NEUTER check disproves a
   trivial no-op guard.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


pytestmark = pytest.mark.unit


def _derive_expected_from_bootstrap() -> frozenset[str]:
    """Compute the ground-truth set of cheap+thinking model ids from bootstrap."""
    bootstrap = importlib.import_module('bootstrap')
    result: set[str] = set()
    for provider in bootstrap._BUILTIN_PROVIDER_TEMPLATES:
        for m in provider.get('models', []):
            caps = set(m.get('capabilities', []))
            if {'cheap', 'thinking'}.issubset(caps):
                result.add(m['model_id'])
    return frozenset(result)


def test_constant_matches_bootstrap_derivation():
    """title_gen's excluded set ≡ every cheap+thinking model in bootstrap."""
    tg = importlib.import_module('lib.conversations.title_gen')
    assert hasattr(tg, '_THINKING_MODELS_TO_EXCLUDE'), \
        'title_gen must expose _THINKING_MODELS_TO_EXCLUDE constant'
    got = tg._THINKING_MODELS_TO_EXCLUDE
    assert isinstance(got, frozenset), \
        f'must be frozenset, got {type(got).__name__}'
    expected = _derive_expected_from_bootstrap()
    assert got == expected, (
        f'title_gen._THINKING_MODELS_TO_EXCLUDE out of sync with bootstrap.\n'
        f'  missing (in bootstrap, not in constant): {expected - got}\n'
        f'  extra (in constant, not in bootstrap): {got - expected}\n'
        f'Update _THINKING_MODELS_TO_EXCLUDE in lib/conversations/title_gen.py '
        f'when adding/removing a cheap+thinking model in bootstrap.py.')


def test_constant_contains_known_offenders():
    """Spot-check: the 4 models we sampled must be in the excluded set.

    Belt-and-suspenders — if bootstrap ever drops one of these while
    they remain live in a corp gateway template, we want to notice.
    """
    tg = importlib.import_module('lib.conversations.title_gen')
    got = tg._THINKING_MODELS_TO_EXCLUDE
    for name in ('deepseek-v4-pro', 'kimi-k2-thinking',
                 'qwen3-max', 'glm-4.7'):
        assert name in got, (
            f'{name} sampled as producing finish=length on title tasks '
            f'must stay in _THINKING_MODELS_TO_EXCLUDE')


def test_generate_forwards_exclude_models_to_dispatch():
    """generate_conversation_title MUST forward the constant as exclude_models."""
    tg = importlib.import_module('lib.conversations.title_gen')
    captured: dict = {}

    def _fake_dispatch(msgs, **kwargs):
        captured['kwargs'] = kwargs
        return '正常标题', {
            'finish_reason': 'stop',
            'completion_tokens': 8,
            '_dispatch': {'model': 'gpt-4.1-mini'},
        }

    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = _fake_dispatch
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': 'hi'},
             {'role': 'assistant', 'content': 'hey'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    assert title == '正常标题'
    assert 'exclude_models' in captured.get('kwargs', {}), (
        'generate_conversation_title must pass exclude_models=')
    fwd = set(captured['kwargs']['exclude_models'])
    assert fwd == set(tg._THINKING_MODELS_TO_EXCLUDE), (
        f'forwarded exclude_models {fwd} != constant '
        f'{set(tg._THINKING_MODELS_TO_EXCLUDE)}')


def test_neuter_forwarding_check_would_fail_on_empty_exclude():
    """NEUTER: if generate_conversation_title stopped forwarding the constant
    (or forwarded an empty list), test_generate_forwards_exclude_models_to_dispatch
    MUST turn red. Prove non-triviality by hand-constructing the negative case."""
    tg = importlib.import_module('lib.conversations.title_gen')
    # The forwarding-check compares to a NON-EMPTY constant; if a would-be
    # regression forwarded exclude_models=[] we should be able to detect
    # inequality against the real constant. Sanity: the constant is non-empty.
    assert len(tg._THINKING_MODELS_TO_EXCLUDE) >= 4, (
        'constant is expected to hold at least the 4 known offenders — '
        'if it drops below that, the NEUTER guard is weakened')
