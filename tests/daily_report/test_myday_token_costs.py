"""Today's calendar folds recorded usage even when charges are zero."""

import datetime as dt
from types import SimpleNamespace

import pytest

from lib.conversations.repository import ConversationTurnUsage
from lib.daily_report import cost, token_costs
from lib.pricing import MODEL_PRICING

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolate_live_usage(monkeypatch):
    monkeypatch.setattr(cost, '_live_day_cache', cost.TTLCache(ttl=0))


def test_current_day_breakdown_and_standard_model_estimates(monkeypatch):
    today = dt.date.today()
    midnight = int(dt.datetime.combine(today, dt.time.min).timestamp() * 1000)
    recorded = {'input': 1, 'output': 5, 'currency': 'CNY',
                'cacheReadMul': 0.1, 'cacheWriteMul': 1.25, 'cacheWrite1hMul': 2}
    free = {'input': 0, 'output': 0, 'currency': 'CNY'}
    target = {'name': 'Tiered target', 'currency': 'CNY', 'contextTiers': [
        {'maxPromptTokens': 1000, 'input': 2, 'output': 10},
        {'maxPromptTokens': 20000, 'input': 20, 'output': 100},
    ]}
    for name, rate in [('myday-recorded', recorded), ('myday-free', free), ('myday-target', target)]:
        monkeypatch.setitem(MODEL_PRICING, name, rate)
    monkeypatch.setattr(token_costs, 'MODEL_PRICING', {'myday-free': free, 'myday-target': target})
    monkeypatch.setattr(token_costs, 'QWEN_PRICING_CNY', {})
    monkeypatch.setattr(cost, '_load_cached_day_costs', lambda *args, **kw: {
        day: {'cost': 0, 'conversations': {}} for day in range(1, today.day)
    })

    def scan(**kwargs):
        assert kwargs['owner_user_id'] == 77
        assert kwargs['start_ms'] == midnight
        rows = [
            {'index': 0, 'timestamp': midnight, 'model': 'myday-recorded', 'usage': {
                'prompt_tokens': 6000, 'prompt_tokens_details': {'cached_tokens': 6000},
                'completion_tokens': 1000, 'reasoning_tokens': 900,
            }},
            {'index': 1, 'timestamp': midnight + 1, 'model': 'myday-recorded', 'usage': {
                'input_tokens': 0, 'output_tokens': 0, 'reasoning_tokens': 1000,
                'cache_read_input_tokens': 2000, 'cache_creation_input_tokens': 2000,
                'cache_creation': {'ephemeral_1h_input_tokens': 1000},
            }},
            {'index': 2, 'timestamp': midnight + 2, 'model': 'myday-free', 'usage': {
                'prompt_tokens': 3000, 'completion_tokens': 500,
            }},
            {'index': 3, 'timestamp': kwargs['end_ms'], 'model': 'myday-free', 'usage': {
                'prompt_tokens': 10000,
            }},
        ]
        return SimpleNamespace(candidate_count=kwargs['limit'], projections=(ConversationTurnUsage(
            metadata={'id': 'quoted', 'created_at': midnight, 'updated_at': midnight + 3},
            message_count=len(rows), title_fallback='', turns=rows,
        ),))

    monkeypatch.setattr('lib.conversations.usage_snapshot.reconcile_day_usage', scan)
    result = cost._get_monthly_costs(today.year, today.month, owner_user_id=77)
    summary = result[today.day]['token_costs']
    assert summary['date'] == today.isoformat()
    assert summary['buckets'] == {
        'input': {'tokens': 3000, 'cost_cny': 0},
        'output': {'tokens': 2500, 'cost_cny': 0.01},
        'cache_read': {'tokens': 8000, 'cost_cny': 0.0008},
        'cache_write': {'tokens': 2000, 'cost_cny': 0.00325},
    }
    assert summary['total_cny'] == pytest.approx(0.01405)
    conversation = result[today.day]['conversations']['quoted']
    assert conversation['tokens'] == sum(b['tokens'] for b in summary['buckets'].values()) == 15500
    assert summary['models'] == [
        {'id': 'myday-free', 'name': 'myday-free', 'cost_cny': 0},
        {'id': 'myday-target', 'name': 'Tiered target', 'cost_cny': 0.0376},
    ]
    assert summary['partial'] and not summary['used_fallback_rates']


@pytest.mark.parametrize(('input_rate', 'tokens_per_turn', 'turn_count'), [
    (1, 40, 300),        # Below the legacy four-decimal Turn total.
    (0.4, 1, 30000),    # Below even the six-decimal component precision.
])
def test_current_day_keeps_sub_rounding_costs_in_both_views(
        monkeypatch, input_rate, tokens_per_turn, turn_count):
    today = dt.date.today()
    midnight = int(dt.datetime.combine(today, dt.time.min).timestamp() * 1000)
    rate = {'input': input_rate, 'output': 0, 'currency': 'CNY'}
    monkeypatch.setitem(MODEL_PRICING, 'myday-small', rate)
    monkeypatch.setattr(token_costs, 'MODEL_PRICING', {'myday-small': rate})
    monkeypatch.setattr(token_costs, 'QWEN_PRICING_CNY', {})
    monkeypatch.setattr(cost, '_load_cached_day_costs', lambda *args, **kw: {
        day: {'cost': 0, 'conversations': {}} for day in range(1, today.day)
    })
    rows = [{'index': i, 'timestamp': midnight + i, 'model': 'myday-small',
             'usage': {'prompt_tokens': tokens_per_turn}} for i in range(turn_count)]
    projection = ConversationTurnUsage(
        metadata={'id': 'small', 'created_at': midnight, 'updated_at': midnight + turn_count - 1},
        message_count=len(rows), title_fallback='', turns=rows,
    )
    monkeypatch.setattr('lib.conversations.usage_snapshot.reconcile_day_usage',
                        lambda **kwargs: SimpleNamespace(candidate_count=1, projections=(projection,)))

    day = cost._get_monthly_costs(today.year, today.month, owner_user_id=77)[today.day]
    assert day['token_costs']['total_cny'] == pytest.approx(0.012)
    assert day['token_costs']['models'][0]['cost_cny'] == pytest.approx(0.012)
    assert day['cost'] == day['token_costs']['total_cny']
    assert day['conversations']['small']['cost'] == day['cost']


def test_empty_day_is_not_a_missing_projection():
    today = dt.date.today()
    summary = token_costs.DayTokenCosts().finish(today)
    assert summary['total_cny'] == 0 and summary['models'] == []
    assert all(cell == {'tokens': 0, 'cost_cny': 0} for cell in summary['buckets'].values())
    assert not summary['partial'] and not summary['used_fallback_rates']
