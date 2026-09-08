#!/usr/bin/env python3
"""tests/test_meituan_template_sync.py — ModelPlaza → template sync spec.

Pure-unit: plaza projection, surface classification, policy exclusions,
merge semantics (curated fields survive, request_ids wires are never
re-added standalone, suspected mirrors are hinted not added), pricing-block
idempotence. No network — probe verdicts are stub dicts.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_template_sync.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts import meituan_template_sync as sync

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


def test_parse_ctx_decimal_convention():
    assert sync.parse_ctx('1024K') == 1024000
    assert sync.parse_ctx('128K') == 128000
    assert sync.parse_ctx('1M') == 1000000
    assert sync.parse_ctx('0.512K') == 512
    assert sync.parse_ctx('未知') is None
    assert sync.parse_ctx('') is None
    assert sync.parse_ctx(None) is None


def test_parse_rpm_zero_means_no_quota():
    assert sync.parse_rpm('100（美团独立集群）') == 100
    assert sync.parse_rpm('5') == 5
    assert sync.parse_rpm('0') is None
    assert sync.parse_rpm('') is None


def test_project_entry_skips_series_headers():
    assert sync.project_entry({'name': 'LongCat-2.0系列 ',
                               'modelCallName': ''}) is None
    cand = sync.project_entry({
        'name': 'LongCat-2.0', 'modelCallName': 'LongCat-2.0',
        'modelCategory': 'Chat', 'contextLength': '1024K',
        'defaultRpm': '5', 'inputPrice': '2.0000', 'outputPrice': '8.0000',
        'tags': ['复杂推理', 'Function Call']})
    assert cand['wire'] == 'LongCat-2.0'
    assert cand['ctx'] == 1024000 and cand['rpm'] == 5
    assert cand['cny_in'] == 2.0 and cand['cny_out'] == 8.0


def test_classify_surface():
    chat = lambda w: {'wire': w, 'name': w, 'cat': 'Chat', 'ctx': None,
                      'rpm': None, 'cny_in': None, 'cny_out': None, 'tags': []}
    assert sync.classify_surface(chat('glm-5.3-flash')) == 'chat'
    assert sync.classify_surface(chat('text-embedding-v4')) == 'embedding'
    assert sync.classify_surface(chat('gpt-image-1')) == 'image_gen'
    assert sync.classify_surface(chat('sora-2')) == 'skip'
    assert sync.classify_surface(chat('doubao-seedance-2.0')) == 'skip'


def _tpl(recipes):
    return {'offering_recipes': recipes}


def _verdicts(**wires):
    return {w: (v, 'stub') for w, v in wires.items()}


def test_merge_keeps_curated_recipe_verbatim_and_never_readds_pool_wires():
    tpl = _tpl([
        {'model_id': 'deepseek-v4-flash',
         'request_ids': ['deepseek-v4-flash', 'deepseek-v4-flash-tencent'],
         'capabilities': ['text', 'thinking', 'cheap'], 'rpm': 60,
         'cost': 0.0002},
    ])
    cands = {
        'deepseek-v4-flash': {'wire': 'deepseek-v4-flash', 'name': 'V4F',
                              'cat': 'Chat', 'ctx': 128000, 'rpm': 30,
                              'cny_in': 1.0, 'cny_out': 2.0, 'tags': []},
        'deepseek-v4-flash-tencent': {'wire': 'deepseek-v4-flash-tencent',
                                      'name': 'V4F-T', 'cat': 'Chat',
                                      'ctx': 128000, 'rpm': 30,
                                      'cny_in': 1.0, 'cny_out': 2.0,
                                      'tags': []},
    }
    verdicts = _verdicts(**{'deepseek-v4-flash': 'ok',
                            'deepseek-v4-flash-tencent': 'rate_limited'})
    merged = sync.merge(tpl, cands, verdicts)
    assert merged['kept'] == tpl['offering_recipes']  # verbatim, curated rpm 60
    assert merged['added'] == []       # pool wire must not become a recipe
    assert merged['dropped'] == []


def test_merge_drops_probe_dead_and_policy_excluded():
    tpl = _tpl([
        {'model_id': 'dead-model', 'capabilities': ['text'], 'rpm': 60,
         'cost': 0.001},
        {'model_id': 'gpt-5.1', 'capabilities': ['text'], 'rpm': 30,
         'cost': 0.01},
    ])
    verdicts = _verdicts(**{'dead-model': 'not_found', 'gpt-5.1': 'ok'})
    merged = sync.merge(tpl, {}, verdicts)
    assert merged['kept'] == []
    dropped = dict(merged['dropped'])
    assert 'not_found' in dropped['dead-model']
    assert dropped['gpt-5.1'] == 'policy-excluded'  # probe ok, policy wins


def test_merge_keeps_transient_failures_as_unverified_never_drops():
    """A read timeout / one empty 200 is not proof of death — the recipe
    stays and is flagged; only definitive verdicts (not_found, bad_request,
    unauthorized) drop."""
    tpl = _tpl([
        {'model_id': 'grok-4.6', 'capabilities': ['text'], 'rpm': 30,
         'cost': 0.009},
        {'model_id': 'flappy', 'request_ids': ['flappy', 'flappy-mirror'],
         'capabilities': ['text'], 'rpm': 60, 'cost': 0.001},
    ])
    verdicts = _verdicts(**{'grok-4.6': 'unavailable',
                            'flappy': 'not_found',
                            'flappy-mirror': 'unavailable'})
    merged = sync.merge(tpl, {}, verdicts)
    assert [r['model_id'] for r in merged['kept']] == ['grok-4.6', 'flappy']
    assert merged['dropped'] == []
    assert {m for m, _ in merged['unverified']} == {'grok-4.6', 'flappy'}


def test_merge_adds_new_served_with_canonical_caps_and_pricing():
    cand = {'wire': 'glm-5.3-flash', 'name': 'GLM-5.3-Flash', 'cat': 'Chat',
            'ctx': 1000000, 'rpm': None, 'cny_in': 5.0, 'cny_out': 25.0,
            'tags': ['复杂推理', '多模态']}
    verdicts = _verdicts(**{'glm-5.3-flash': 'ok'})
    merged = sync.merge(_tpl([]), {'glm-5.3-flash': cand}, verdicts)
    assert len(merged['added']) == 1
    r = merged['added'][0]
    assert r['model_id'] == 'glm-5.3-flash'
    assert r['capabilities'][0] == 'text'
    assert 'vision' in r['capabilities'] and 'thinking' in r['capabilities']
    assert r['context_window'] == 1000000
    assert r['context_window_source'] == 'meituan_plaza'
    row = merged['pricing_rows']['glm-5.3-flash']
    assert row['input'] == round(5.0 / 7.2, 4)
    assert row['output'] == round(25.0 / 7.2, 4)


def test_mirror_matching_normalized_with_digit_guard():
    assert sync._is_mirror('aws.claude-haiku-4.5', 'claude-haiku-4.5')
    assert sync._is_mirror('glm-52-meituan', 'glm-5.2')
    assert sync._is_mirror('kimi-k2.6-baidu', 'kimi-k2.6')
    assert not sync._is_mirror('glm-5.3-flash', 'glm-5')      # distinct model
    assert not sync._is_mirror('azure.claude-fable-5.1', 'claude-fable-5')
    assert not sync._is_mirror('minimax-m2.5-tencent', 'MiniMax-M2.1')
    assert sync._is_mirror('glm-5.3-flash', 'glm-5.3')        # containment hint
    assert sync._is_mirror('minimax-m2.5-tencent', 'MiniMax-M2.5')

def test_merge_hints_mirror_instead_of_adding():
    tpl = _tpl([{'model_id': 'claude-opus-4.7', 'capabilities': ['text'],
                 'rpm': 30, 'cost': 0.045}])
    cand = {'wire': 'vertex.claude-opus-4.7', 'name': 'X', 'cat': 'Chat',
            'ctx': None, 'rpm': None, 'cny_in': None, 'cny_out': None,
            'tags': []}
    verdicts = _verdicts(**{'claude-opus-4.7': 'ok',
                            'vertex.claude-opus-4.7': 'ok'})
    merged = sync.merge(tpl, {'vertex.claude-opus-4.7': cand}, verdicts)
    assert merged['added'] == []
    assert any('vertex.claude-opus-4.7' in h for h in merged['mirror_hints'])


def test_excluded_never_added_even_when_served():
    cand = {'wire': 'gpt-5.1', 'name': 'GPT-5.1', 'cat': 'Chat',
            'ctx': 400000, 'rpm': 30, 'cny_in': 10.0, 'cny_out': 40.0,
            'tags': []}
    merged = sync.merge(_tpl([]), {'gpt-5.1': cand},
                        _verdicts(**{'gpt-5.1': 'ok'}))
    assert merged['added'] == []


_PRICING_SRC = '''import json

MODEL_PRICING = {
    'deepseek-v4-flash': {'input': 0.14, 'output': 0.28, 'cacheWriteMul': 1.0, 'cacheReadMul': 0.2, 'name': 'DeepSeek V4 Flash'},
}

OTHER_TABLE = {
}
'''


def test_pricing_block_idempotent_and_never_duplicates_hand_rows():
    rows = {
        'deepseek-v4-flash': {'input': 9.9, 'output': 9.9, 'name': 'dup'},
        'glm-5.3-flash': {'input': 0.6944, 'output': 3.4722,
                          'name': 'GLM-5.3-Flash'},
    }
    once = sync.update_pricing_text(_PRICING_SRC, rows, {})
    # hand-written row wins; only the genuinely new id is staged
    assert once.count("'deepseek-v4-flash':") == 1
    assert "'glm-5.3-flash':" in once
    twice = sync.update_pricing_text(once, rows, {})
    assert twice == once  # regeneration is wholesale but stable
    namespace: dict = {}
    exec(once, namespace)  # stays valid Python
    assert namespace['MODEL_PRICING']['glm-5.3-flash']['input'] == 0.6944


def test_compact_snapshot_projection(tmp_path):
    p = tmp_path / 'compact.json'
    p.write_text('[{"n": "LongCat-2.0", "w": "LongCat-2.0", "cat": "Chat",'
                 ' "ctx": "1024K", "in": "2.0000", "out": "8.0000",'
                 ' "rpm": "5", "tags": ["复杂推理"]}]', encoding='utf-8')
    entries = sync.load_snapshot(str(p))
    cand = sync.project_entry(entries[0])
    assert cand['wire'] == 'LongCat-2.0' and cand['ctx'] == 1024000


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
