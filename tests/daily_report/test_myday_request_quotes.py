"""The MyDay scan folds request snapshots, not the Turn's price tier."""

import datetime as dt
from itertools import permutations

import pytest

from lib.conversations.repository import ConversationTurnUsage
from lib.cost import compute_cost
from lib.daily_report.cost import _scan_costs_in_range
from lib.daily_report.token_costs import DayTokenCosts

pytestmark = pytest.mark.unit
_DATE = dt.date(2026, 11, 18)
_START = int(dt.datetime.combine(_DATE, dt.time.min).timestamp() * 1000)
_END = int(dt.datetime.combine(_DATE + dt.timedelta(days=1), dt.time.min).timestamp() * 1000)


def _scan(monkeypatch, *conversations):
    def scan(**kwargs):
        assert kwargs['user_id'] == 19
        assert (kwargs['timestamp_gte'], kwargs['timestamp_lt']) == (_START, _END)
        return len(conversations), iter(conversations)

    monkeypatch.setattr('lib.conversations.repository.scan_conversation_turn_usage', scan)
    token_costs = DayTokenCosts()
    days = _scan_costs_in_range(_START, _END, owner_user_id=19, token_costs=token_costs)
    return days.get(_DATE.day), token_costs.finish(_DATE)


def _conversation(conv_id, *rows):
    return ConversationTurnUsage({'id': conv_id, 'created_at': _START}, len(rows), '', list(rows))


def _turn(usage, rounds, timestamp=_START):
    return {'index': 0, 'timestamp': timestamp, 'usage': usage, 'apiRounds': rounds}


def _receipt(identity, usage, *, rate=2, model='test-sol', provider='provider-a', **rates):
    pricing = dict(input=rate, output=10, currency='CNY', **rates)
    quote = compute_cost(usage, model, provider, pricing_override=pricing)
    return {'usageReceiptId': identity, 'model': model, 'providerId': provider, 'cost': quote}


def test_same_turn_two_150k_calls_use_their_own_tier(monkeypatch):
    from lib.pricing import MODEL_PRICING

    monkeypatch.setitem(MODEL_PRICING, 'myday-tier-test', {
        'currency': 'CNY', 'contextTiers': [
            {'id': 'standard', 'maxPromptTokens': 272000, 'input': 2, 'output': 10},
            {'id': 'long', 'maxPromptTokens': 1000000, 'input': 4, 'output': 20},
        ],
    })
    usage = {'prompt_tokens': 150000}
    model = 'myday-tier-test'
    first = compute_cost(usage, model)
    flattened = {'prompt_tokens': 300000}
    assert first['pricingSnapshot']['tierId'] == 'standard'
    assert compute_cost(flattened, model)['costCny'] == 1.20
    turn = dict(_turn(flattened, [dict(usageReceiptId=str(i), cost=first) for i in range(2)]), model=model)
    day, detail = _scan(monkeypatch, _conversation('source', turn))
    assert day['cost'] == detail['total_cny'] == 0.60
    assert detail['buckets']['input']['tokens'] == 300000


def test_five_receipts_preserve_observed_mix(monkeypatch):
    # Synthetic division of the audited 747096 / 788 aggregate, not a
    # reconstruction of its unavailable exact individual request evidence.
    cold = (134744, 0, 0, 0, 0)
    read = (0, 150000, 151000, 155000, 156352)
    output = (100, 135, 155, 188, 210)
    rounds = [_receipt(str(i), {'prompt_tokens': c + r, 'completion_tokens': o,
                              'prompt_tokens_details': {'cached_tokens': r}})
              for i, (c, r, o) in enumerate(zip(cold, read, output))]
    day, detail = _scan(monkeypatch, _conversation('source', _turn(
        {'prompt_tokens': 747096, 'completion_tokens': 788}, rounds)))
    assert day['cost'] == detail['total_cny'] == pytest.approx(0.399838)
    assert [detail['buckets'][bucket]['tokens'] for bucket in ('input', 'cache_read', 'output')] == [134744, 612352, 788]
    assert day['conversations']['source']['tokens'] == 747884


def test_cloned_and_retried_receipts_are_not_new_requests(monkeypatch):
    first = _receipt('first', {'prompt_tokens': 150000})
    new = _receipt('new', {'input_tokens': 5000, 'cache_read_input_tokens': 2000,
                           'cache_creation_input_tokens': 1000,
                           'cache_creation': {'ephemeral_1h_input_tokens': 1000}},
                   rate=10, model='another-model', provider='provider-b',
                   cacheWriteMul=1.25, cacheWrite1hMul=2)
    old_turn = _turn({'prompt_tokens': 150000}, [first, first])
    new_turn = _turn({'input_tokens': 5000}, [new])
    outside = _turn({'input_tokens': 5000}, [_receipt('outside', {'input_tokens': 5000})], _END)
    day, detail = _scan(monkeypatch, _conversation('a-source', old_turn),
                        _conversation('clone', old_turn, new_turn, outside))
    assert day['cost'] == detail['total_cny'] == 0.372
    assert detail['buckets']['input']['tokens'] == 155000
    assert detail['buckets']['cache_read']['tokens'] == 2000
    assert detail['buckets']['cache_write'] == {'tokens': 1000, 'cost_cny': 0.02}
    assert day['conversations']['clone']['tokens'] == 8000


def test_new_python_receipt_carries_request_time_and_route():
    from lib.tasks_pkg.llm_fallback._usage import record_scope_denied_usage
    from lib.tasks_pkg.round_costs import stamp_round_costs

    usage = {'prompt_tokens': 150000, '_dispatch': {
        'provider_id': 'original-provider', 'attempt_started_at_unix_ns': _START * 1_000_000}}
    rounds = []
    record_scope_denied_usage(usage, 'original-model', 0, {}, rounds)
    stamp_round_costs({}, rounds, model='terminal-model')
    assert rounds[0]['usageReceiptId']
    assert rounds[0]['requestTimestampMs'] == _START
    assert rounds[0]['cost']['pricingSnapshot']['model'] == 'original-model'
    assert rounds[0]['cost']['pricingSnapshot']['provider'] == 'original-provider'


def test_unpriced_round_is_unknown_not_an_aggregate_estimate(monkeypatch):
    day, detail = _scan(monkeypatch, _conversation('source', _turn(
        {'prompt_tokens': 600000}, [{'usageReceiptId': 'unknown', 'cost': None}])))
    assert day['conversations']['source']['tokens'] == 600000
    assert detail['partial']
    assert detail['total_cny'] == 0


def test_partial_receipts_keep_reported_tokens_but_do_not_pin_history(monkeypatch):
    from lib.daily_report import cost

    stamp = int(dt.datetime(2020, 2, 3, 12).timestamp() * 1000)
    tiny = _receipt('tiny', {'prompt_tokens': 1}, rate=0.4)
    # Original components, not the rounded four-decimal receipt total.
    tiny['cost']['inputCostCny'] = 0.0000004
    assert tiny['cost']['costCny'] == 0
    unknown = {'usageReceiptId': 'partially-observed', 'usage': {'input_tokens': 123}}
    row = _turn({'input_tokens': 124}, [tiny, unknown], stamp)
    saved = {}
    monkeypatch.setattr(cost, '_load_cached_day_costs', lambda *a, **k: {})
    monkeypatch.setattr(cost, '_persist_day_cost', lambda date, data, **k: saved.update({date: data}))
    monkeypatch.setattr('lib.conversations.repository.scan_conversation_turn_usage',
                        lambda **k: (1, iter([_conversation('partial', row)])))
    start = int(dt.datetime(2020, 2, 3).timestamp() * 1000)
    unsettled = set()
    collector = DayTokenCosts()
    days = cost._scan_costs_in_range(start, start + 86_400_000, owner_user_id=19,
                                   token_costs=collector, unsettled_days=unsettled)
    assert unsettled == {3}
    assert collector.partial and collector.buckets['input']['tokens'] == 124
    assert collector.buckets['input']['cost_cny'] == 0.0000004
    assert days[3]['conversations']['partial']['tokens'] == 124
    cost._get_monthly_costs(2020, 2, owner_user_id=19)
    assert '2020-02-03' not in saved
    assert '2020-02-04' in saved


def test_executed_offering_wins_and_unknown_provenance_is_explicit():
    from lib.tasks_pkg.round_costs import stamp_round_costs

    offering = dict(unit='per_million_tokens', currency='CNY', input=3, output=7)
    rows = [{'model': 'model-with-no-registered-price', 'usage': {
        'prompt_tokens': 150000, '_dispatch': {'provider_id': 'selected-route',
           'offering_actual_pricing': offering, 'attempt_started_at_unix_ns': _START * 1_000_000}}},
        {'model': 'model-with-no-registered-price', 'usage': {'prompt_tokens': 150000}}]
    stamp_round_costs({}, rows, model='terminal-model')
    assert rows[0]['cost']['pricingSource'] == 'route_offering'
    assert rows[0]['cost']['costCny'] == .45
    assert rows[0]['requestTimestampMs'] == _START and 'pricingEvidence' not in rows[0]
    assert rows[1]['pricingEvidence'] == ['request_clock', 'executed_offering_price']


def test_historical_scan_sums_receipt_components_before_rounding(monkeypatch):
    first = _receipt('first', {'prompt_tokens': 60}, rate=1)
    second = _receipt('second', {'prompt_tokens': 60}, rate=1)
    assert first['cost']['costCny'] + second['cost']['costCny'] == 0.0002
    row = _turn({'prompt_tokens': 120}, [first, second])
    monkeypatch.setattr('lib.conversations.repository.scan_conversation_turn_usage',
                        lambda **k: (1, iter([_conversation('past', row)])))
    incomplete = set()
    days = _scan_costs_in_range(_START, _END, owner_user_id=19, unsettled_days=incomplete)
    assert days[_DATE.day]['cost'] == 0.0001
    assert not incomplete


@pytest.mark.parametrize('reverse', [False, True])
def test_duplicate_compatible_evidence_reconciles_before_fold(monkeypatch, reverse):
    priced = _receipt('same', {'prompt_tokens': 150000})
    identity_only = {'usageReceiptId': 'same'}
    rows = [identity_only, priced]
    if reverse:
        rows.reverse()
    day, detail = _scan(monkeypatch, _conversation('same', _turn({}, rows)))
    assert day['cost'] == detail['total_cny'] == .30
    assert detail['buckets']['input']['tokens'] == 150000
    assert not detail['partial']


def test_partial_receipt_population_is_not_a_fabricated_bucket(monkeypatch):
    priced = _receipt('known', {'prompt_tokens': 1}, rate=1000)
    day, detail = _scan(monkeypatch, _conversation('partial', _turn(
        {'prompt_tokens': 124}, [priced, {'usageReceiptId': 'identity-only'}])))
    entry = day['conversations']['partial']
    assert entry['tokens'] == 124 and entry['unallocated_tokens'] == 123
    assert detail['buckets']['input']['tokens'] == 1 and detail['partial']


def test_identity_only_without_turn_aggregate_does_not_pin_zero(monkeypatch):
    row = _turn({}, [{'usageReceiptId': 'incomplete'}])
    monkeypatch.setattr('lib.conversations.repository.scan_conversation_turn_usage',
                        lambda **k: (1, iter([_conversation('receipt-only', row)])))
    unsettled = set()
    assert _scan_costs_in_range(_START, _END, owner_user_id=19,
                               unsettled_days=unsettled) == {}
    assert unsettled == {_DATE.day}


def test_terminal_uses_executed_offering_quote_and_labels_estimated_clock():
    from lib.tasks_pkg.round_costs import stamp_terminal_cost

    usage = {'prompt_tokens': 150000, '_dispatch': {
        'stream_started_at_unix_ns': _START * 1_000_000,
        'offering_actual_pricing': dict(input=3, output=7, currency='CNY',
                                        unit='per_million_tokens')}}
    event = {'model': 'gpt-6.1-sol', 'usage': usage, 'apiRounds': [{'usage': usage}]}
    stamp_terminal_cost({}, event)
    row = event['apiRounds'][0]
    assert event['cost']['costCny'] == row['cost']['costCny'] == .45
    assert row['pricingEvidence'] == ['request_clock_estimated']


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('with_ui', [False, True])
def test_sparse_turn_copies_and_ui_fields_do_not_change_accounting(monkeypatch, reverse, with_ui):
    paid = _receipt('paid', {'prompt_tokens': 1}, rate=1000)
    other = {**paid, 'cost': dict(paid['cost'])}
    if with_ui:
        other['cost']['presentationOnly'] = 'different tooltip'
    unknown = {'taskId': 'original', 'sourceRound': 12, 'usage': {'_dispatch': {'provider_id': 'A'}}}
    sparse = _conversation('sparse', _turn({}, [other, unknown], _START))
    complete = _conversation('aggregate', _turn({'prompt_tokens': 124}, [paid, unknown], _START + 1))
    third = _conversation('distinct', _turn({}, [_receipt('different', {'prompt_tokens': 7}, rate=1000)], _START + 2))
    copies = [sparse, complete, third]
    if reverse:
        copies.reverse()
    day, detail = _scan(monkeypatch, *copies)
    assert day['cost'] == detail['total_cny'] == .008
    assert sum(c['tokens'] for c in day['conversations'].values()) == 131
    assert sum(c.get('unallocated_tokens', 0) for c in day['conversations'].values()) == 123
    assert detail['buckets']['input']['tokens'] == 8 and detail['partial']


def test_quote_only_duplicate_is_shared_by_terminal_and_durable_projection():
    from lib.tasks_pkg.round_costs import stamp_terminal_cost
    from lib.turn_lifecycle import _task_projection

    quote = _receipt('paid', {'prompt_tokens': 150000}, rate=3)
    copy = {**quote, 'cost': {**quote['cost'], 'uiHint': 'extra only'}}
    task = {'id': 'task', 'convId': 'conv', 'config': {}, 'apiRounds': [copy, quote]}
    event = {'model': 'different-current-model', 'apiRounds': task['apiRounds']}
    stamp_terminal_cost(task, event)
    stored = _task_projection(task, {})
    assert event['cost']['costCny'] == stored['cost']['costCny'] == .45
    assert event['cost']['totalInputTokens'] == stored['cost']['totalInputTokens'] == 150000
    assert event['cost']['pricingSnapshot']['rounds'] == stored['cost']['pricingSnapshot']['rounds'] == 1
    from tests.turn_lifecycle._projection_oracle import _task_projection as oracle
    assert oracle(task, {})['cost'] == stored['cost']


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_sparse_counters_and_overlapping_residuals_are_permutation_invariant(monkeypatch, order):
    priced = _receipt('paid', {'prompt_tokens': 1, 'completion_tokens': 2}, rate=1000)
    sparse = {'usageReceiptId': 'paid', 'usage': {'output_tokens': 2}}
    altered = {**priced, 'cost': {**priced['cost'], 'cacheSavingsCny': 99}}
    x, y = ({'usageReceiptId': name} for name in ('x', 'y'))
    rows = [_conversation('first', _turn({'prompt_tokens': 124, 'completion_tokens': 2}, [sparse, x])),
            _conversation('second', _turn({'prompt_tokens': 124, 'completion_tokens': 2}, [altered, x, y])),
            _conversation('third', _turn({}, [priced]))]
    day, detail = _scan(monkeypatch, *(rows[i] for i in order))
    assert day['cost'] == detail['total_cny'] == .00102
    assert sum(c['tokens'] for c in day['conversations'].values()) == 126
    assert sum(c.get('unallocated_tokens', 0) for c in day['conversations'].values()) == 123


def test_partial_footer_uses_known_turn_total_and_clears_stale_projection():
    from lib.tasks_pkg.round_costs import stamp_terminal_cost
    from lib.turn_lifecycle import _task_projection

    paid = _receipt('paid', {'prompt_tokens': 1}, rate=1000)
    unknown = {'usageReceiptId': 'unknown'}
    task = {'id': 'task', 'convId': 'conv', 'config': {}, 'apiRounds': [paid, unknown],
            'usage': {'prompt_tokens': 124}}
    event = dict(apiRounds=task['apiRounds'], usage=task['usage'])
    stamp_terminal_cost(task, event)
    durable = _task_projection(task, {})
    assert event['cost']['totalInputTokens'] == durable['cost']['totalInputTokens'] == 124
    assert durable['cost']['costCny'] == .001
    task['apiRounds'] = [unknown]
    assert 'cost' not in _task_projection(task, durable)


def test_remapped_legacy_copy_cannot_be_reported_as_settled(monkeypatch):
    rows = []
    for name in ('legacy-task', 'remapped-task'):
        receipt = _receipt('not-persisted', {'prompt_tokens': 150000}, rate=3)
        del receipt['usageReceiptId']
        receipt.update(taskId=name, sourceRound=1)
        rows.append(_conversation(name, _turn({}, [receipt])))
    day, detail = _scan(monkeypatch, *rows)
    assert day['partial'] and detail['partial']
    unsettled = set()
    _scan_costs_in_range(_START, _END, owner_user_id=19, unsettled_days=unsettled)
    assert unsettled == {_DATE.day}


@pytest.mark.parametrize('full,sparse', [
    ({'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 90}}, {'prompt_tokens': 100}),
    ({'input_tokens': 10, 'cache_read_input_tokens': 80, 'cache_creation_input_tokens': 10}, {'input_tokens': 10}),
])
@pytest.mark.parametrize('reverse', [False, True])
def test_sparse_cache_conventions_preserve_witnessed_cold(monkeypatch, full, sparse, reverse):
    rows = [_receipt('same', full, rate=1000), {'usageReceiptId': 'same', 'usage': sparse}]
    if reverse:
        rows.reverse()
    day, detail = _scan(monkeypatch, _conversation('cache', _turn({}, rows)))
    assert detail['buckets']['input']['tokens'] == 10
    assert day['conversations']['cache']['tokens'] == 100
    assert not detail['partial']


def test_legacy_charge_conflict_marks_valid_subtotal_partial():
    from lib.request_quotes import fold_stored_quotes

    legacy = []
    for rate in (2, 3):
        row = _receipt('unused', {'prompt_tokens': 150000}, rate=rate)
        del row['usageReceiptId']
        legacy.append(dict(row, taskId='legacy', sourceRound=1))
    third = _receipt('good', {'prompt_tokens': 150000}, rate=4)
    for rows in permutations([*legacy, third]):
        result = fold_stored_quotes(rows)
        assert result['costCny'] == .60 and result['pricingSnapshot']['partial']


@pytest.mark.parametrize('reverse', [False, True])
def test_anonymous_residual_copies_are_not_assumed_disjoint(monkeypatch, reverse):
    paid = _receipt('paid', {'prompt_tokens': 1}, rate=1000)
    copies = [_conversation(name, _turn({'prompt_tokens': 124}, [paid, {}])) for name in ('a', 'b')]
    if reverse:
        copies.reverse()
    day, detail = _scan(monkeypatch, *copies)
    assert detail['partial'] and day['partial']
    assert sum(c['tokens'] for c in day['conversations'].values()) == 124
    assert sum(c.get('unallocated_tokens', 0) for c in day['conversations'].values()) == 123


@pytest.mark.parametrize('prompt,cached', [(11, 79), (100, 90), (61, 17), (100, 0)])
def test_sparse_wire_copies_never_reprice_a_frozen_quote(prompt, cached):
    from lib.tasks_pkg.round_costs import stamp_terminal_cost
    from lib.turn_lifecycle import _task_projection

    full = _receipt('r', {'prompt_tokens': prompt, 'cache_read_tokens': cached}, rate=1000)
    sparse = {'usageReceiptId': 'r', 'usage': {'prompt_tokens': prompt, 'input_tokens_details': {}}}
    cosmetic = {**full, 'cost': {**full['cost'], 'uiHint': 'different'}}
    for order in permutations([sparse, full, cosmetic]):
        rows = list(order)
        task = {'id': 'task', 'convId': 'conv', 'config': {}, 'apiRounds': rows}
        event = {'apiRounds': rows}
        stamp_terminal_cost(task, event)
        durable = _task_projection(task, {})
        assert event['cost'] == durable['cost']
        assert durable['cost']['costCny'] == full['cost']['costCny']
        assert durable['cost']['totalInputTokens'] == full['cost']['totalInputTokens']


@pytest.mark.parametrize('anonymous_count', [0, 27, 139])
def test_anonymous_observations_stay_in_ambiguous_coverage(monkeypatch, anonymous_count):
    paid = _receipt('r', {'prompt_tokens': 1}, rate=1000)
    anonymous = {'usage': {'input_tokens': anonymous_count}}
    rows = [_conversation(name, _turn({'prompt_tokens': 140}, [paid, anonymous])) for name in ('a','b')]
    for order in permutations(rows):
        day, detail = _scan(monkeypatch, *order)
        assert detail['partial'] and detail['buckets']['input']['tokens'] == 1
        assert sum(c['tokens'] for c in day['conversations'].values()) == 140


def test_empty_anonymous_placeholder_cannot_collapse_disjoint_residuals(monkeypatch):
    paid = _receipt('r', {'prompt_tokens': 1}, rate=1000)
    rows = [_conversation('empty', _turn({'prompt_tokens': 1}, [paid, {}])),
            _conversation('x', _turn({'prompt_tokens': 139}, [{'usageReceiptId': 'x'}])),
            _conversation('y', _turn({'prompt_tokens': 195}, [{'usageReceiptId': 'y'}]))]
    for order in permutations(rows):
        day, _ = _scan(monkeypatch, *order)
        assert sum(c['tokens'] for c in day['conversations'].values()) == 335


def test_unpriced_sparse_copies_get_only_one_quote_after_join():
    from lib.tasks_pkg.round_costs import stamp_terminal_cost

    for prompt, read, write in ((11, 79, 0), (127, 99, 7), (10, 0, 0)):
        route = {'offering_actual_pricing': dict(input=3, output=7, currency='CNY',
                                                unit='per_million_tokens')}
        pieces = ({'prompt_tokens': prompt, '_dispatch': route}, {'cache_read_tokens': read},
                  {'cache_write_tokens': write, 'completion_tokens': 4})
        expected = compute_cost(dict(prompt_tokens=prompt, cache_read_tokens=read,
                                     cache_write_tokens=write, completion_tokens=4),
                                pricing_override=route['offering_actual_pricing'],
                                unrounded_cny_components=True)
        for order in permutations(pieces):
            event = {'apiRounds': [{'usageReceiptId': 'split', 'usage': usage} for usage in order]}
            stamp_terminal_cost({}, event)
            [quote] = [row['cost'] for row in event['apiRounds'] if row.get('cost')]
            assert quote['costCny'] == expected['costCny']
            assert quote['totalInputTokens'] == expected['totalInputTokens']
            assert event['cost']['pricingSnapshot']['rounds'] == 1


@pytest.mark.parametrize('x,y,anonymous', [(139,195,27), (12,81,17), (490,3,700)])
def test_ambiguous_evidence_never_erases_disjoint_named_usage(monkeypatch, x, y, anonymous):
    paid = _receipt('paid', {'prompt_tokens': 1}, rate=1000)
    for extra_paid, extra_named in (([], []), ([{'usage': {'input_tokens': anonymous}}], []), ([], [{}])):
        rows = [_conversation('paid', _turn({'prompt_tokens': 1}, [paid, *extra_paid])),
                _conversation('x', _turn({'prompt_tokens': x}, [{'usageReceiptId': 'x'}, *extra_named])),
                _conversation('y', _turn({'prompt_tokens': y}, [{'usageReceiptId': 'y'}]))]
        for order in permutations(rows):
            day, detail = _scan(monkeypatch, *order)
            assert sum(c['tokens'] for c in day['conversations'].values()) >= x + y + 1
            assert detail['partial'] and detail['total_cny'] == .001


def test_invalid_quote_does_not_hide_anonymous_observation(monkeypatch):
    row = {'cost': {'costCny': .4}, 'usage': {'input_tokens': 139}}
    day, detail = _scan(monkeypatch, _conversation('anon', _turn({}, [row])))
    assert day['conversations']['anon']['unallocated_tokens'] == 139
    assert detail['partial'] and detail['total_cny'] == 0


@pytest.mark.parametrize('cold,read', [(11,79), (4,5), (70,170)])
def test_hybrid_frozen_evidence_accepts_alias_but_not_contradiction(cold, read):
    from lib.request_quotes import fold_stored_quotes

    full = _receipt('r', {'prompt_tokens': cold, 'cache_read_tokens': read}, rate=1000)
    for value, partial in ((cold + read, False), (0, True), (cold + read + 1, True)):
        sparse = {'usageReceiptId': 'r', 'usage': {'prompt_tokens': value}}
        for rows in permutations([full, sparse]):
            result = fold_stored_quotes(rows)
            assert result['totalInputTokens'] == cold + read
            assert result['pricingSnapshot'].get('partial', False) is partial


def test_residual_contradiction_survives_compatible_sparse_alias_in_every_order():
    from lib.request_quotes import fold_stored_quotes

    frozen = _receipt('same', {'input_tokens': 11, 'cache_read_input_tokens': 79}, rate=1000)
    contradiction = {'usageReceiptId': 'same', 'usage': {'input_tokens': 90, 'cache_read_input_tokens': 79}}
    compatible = {'usageReceiptId': 'same', 'usage': {'input_tokens': 11}}
    for rows in permutations([frozen, contradiction, compatible]):
        result = fold_stored_quotes(rows)
        assert result['pricingSnapshot']['partial']
        assert result['inputTokens'] == 11 and result['totalInputTokens'] == 90
