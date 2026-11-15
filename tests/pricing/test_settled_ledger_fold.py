"""The settled-run fold of an API-round ledger: exact, resumable, explicitly invalidated.

``compute_api_rounds_cost`` memoizes the fold of a ledger's leading settled
rounds in the first round (``_SettledRunFold``) and resumes builtin ``sum``'s
float state there for the rounds after the run. These specs hold the resumable
column sums to the running interpreter's ``sum`` bit for bit, every fold to the
vendored pre-change fold (``tests/turn_lifecycle/_projection_oracle.py``), each
dependency of the memo (round identities, pricing revision and snapshot rates,
peak multiplier, fallbacks, cost mode, settledness) to a refold, and the work
of one frame over 2,000 settled rounds to the rounds that changed.
"""
import copy
import random
import struct
from datetime import datetime, timezone

import pytest

import lib.cost as cost_module
import lib.pricing._peak as peak_module
from lib.conversation_sync.settled_rows import settle
from lib.cost import _ColumnSums, compute_api_rounds_cost, compute_cost
from lib.pricing import PROVIDER_PRICING, get_pricing_data, set_provider_pricing
from tests.turn_lifecycle import _projection_oracle as oracle

pytestmark = pytest.mark.unit

_PROVIDER = 'fold-test-provider'
_MODEL = 'fold-test-model'


@pytest.fixture
def priced_provider(monkeypatch):
    monkeypatch.setitem(PROVIDER_PRICING, _PROVIDER, {
        _MODEL: {'input': 2.0, 'output': 8.0, 'cacheWriteMul': 1.25, 'cacheReadMul': 0.1},
        'other-model': {'input': 0.7, 'output': 2.9},
        'claude-fold-test': {'input': 3.0, 'output': 15.0, 'cacheWriteMul': 1.25,
                             'cacheReadMul': 0.1},
    })
    yield


def _bits(value):
    """Exact comparison form: every float by its IEEE-754 bit pattern."""
    if isinstance(value, float):
        return 'f' + struct.pack('<d', value).hex()
    if isinstance(value, dict):
        return {key: _bits(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bits(item) for item in value]
    return value


def _usage(index):
    # Every seventh round is huge, so later small rounds only survive in the
    # compensation term of ``sum``: a fold that resumed from a rounded total
    # instead of the exact state would drift from the oracle.
    prompt = 10 ** 16 if index % 7 == 3 else 10_000 + index * 137
    return {'prompt_tokens': prompt, 'completion_tokens': 300 + index % 11,
            'prompt_tokens_details': {'cached_tokens': 8_000 + index * 3}}


def _round(index, *, model=_MODEL, provider=_PROVIDER, **extra):
    row = {'round': index, 'tag': 'main', 'usage': _usage(index), **extra}
    if model is not None:
        row['model'] = model
    if provider is not None:
        row['provider_id'] = provider
    return row


def _frozen_cost(index):
    # A frozen receipt deliberately differs from the live table's price.
    return compute_cost(_usage(index), model_id=_MODEL,
                        pricing_override={'input': 0.25, 'output': 1.5})


def _oracle(rows, **kwargs):
    return oracle.compute_api_rounds_cost(copy.deepcopy(rows), **kwargs)


def _fold(rows, **kwargs):
    """Fold a fresh list, as each Turn frame does: identity, not the list, matters."""
    return compute_api_rounds_cost(list(rows), **kwargs)


# ── Exactness of the resumable sums ─────────────────────────────────────────

_FINITE = (1e16, 1.0, -1e16, 0.1, 0.2, 0.3, -0.0, 0.0, 5e-324, 2.0 ** 53, 1e308,
           -1e308, 1e-16, 3.0, 123456.789, -2.5e-300)
_SPECIAL = (float('inf'), float('-inf'), float('nan'))


def _float_rows(seed, count, width):
    rng = random.Random(seed)

    def value():
        draw = rng.random()
        if draw < 0.04:
            return rng.choice(_SPECIAL)
        if draw < 0.5:
            return rng.choice(_FINITE)
        return rng.uniform(-1.0, 1.0) * 10.0 ** rng.randint(-30, 30)

    return [tuple(value() for _ in range(width)) for _ in range(count)]


@pytest.mark.parametrize('seed', range(8))
def test_column_sums_resumed_after_any_row_equal_builtin_sum_bit_for_bit(seed):
    rows = _float_rows(seed, 32, 9)
    expected = _bits([sum(column) for column in zip(*rows)])
    for split in range(len(rows) + 1):
        head = _ColumnSums()
        for row in rows[:split]:
            head.add(row)
        resumed = _ColumnSums(*head.state())
        for row in rows[split:]:
            resumed.add(row)
        assert _bits(resumed.totals()) == expected, f'split {split}'


def test_a_rounded_prefix_total_cannot_resume_sum_but_the_saved_state_does():
    column = (1e16, 1.0, 1.0)
    full = sum(column)
    assert full == 1.0000000000000002e16
    # Restarting ``sum`` from the prefix's result loses the compensation...
    assert sum((sum(column[:2]), column[2])) != full
    # ...while the saved state continues the identical arithmetic.
    head = _ColumnSums()
    head.add(column[:1])
    head.add(column[1:2])
    resumed = _ColumnSums(*head.state())
    resumed.add(column[2:])
    assert resumed.totals() == [full]


def test_column_sums_keep_the_sign_of_negative_zero_like_sum():
    sums = _ColumnSums()
    sums.add((-0.0, 1e308))
    sums.add((-0.0, 1e308))
    assert _bits(sums.totals()) == _bits([sum((-0.0, -0.0)), sum((1e308, 1e308))])


# ── Resumed folds equal the full pre-change fold ────────────────────────────

def test_every_resumed_live_fold_equals_the_pre_change_full_fold(priced_provider):
    rows = []
    for index in range(36):
        model = 'other-model' if index % 5 == 4 else _MODEL
        rows.append(settle(_round(index, model=model)))
        assert _bits(_fold(rows)) == _bits(_oracle(rows)), index


def test_every_resumed_frozen_fold_equals_the_pre_change_full_fold():
    rows = []
    for index in range(36):
        rows.append(settle(_round(index, cost=_frozen_cost(index))))
        assert _bits(_fold(rows, use_frozen_costs=True)) == _bits(
            _oracle(rows, use_frozen_costs=True)), index


def test_a_frame_over_2000_settled_rounds_folds_only_the_rounds_that_changed(
        priced_provider, monkeypatch):
    """Amortized O(1) per frame: before this change every frame repriced and summed all."""
    calls = {'priced': 0, 'summed': 0}
    real_cost = cost_module._LedgerPricing.round_cost
    real_add = cost_module._ColumnSums.add

    def counting_cost(self, *args):
        calls['priced'] += 1
        return real_cost(self, *args)

    def counting_add(self, values):
        calls['summed'] += 1
        return real_add(self, values)

    monkeypatch.setattr(cost_module._LedgerPricing, 'round_cost', counting_cost)
    monkeypatch.setattr(cost_module._ColumnSums, 'add', counting_add)
    rows = [settle(_round(index)) for index in range(2000)]
    first = _fold(rows)
    assert calls == {'priced': 2000, 'summed': 2000}
    for _frame in range(3):
        calls.update(priced=0, summed=0)
        assert _bits(_fold(rows)) == _bits(first)
        assert calls == {'priced': 0, 'summed': 0}
    rows.append(settle(_round(2000)))
    calls.update(priced=0, summed=0)
    grown = _fold(rows)
    assert calls == {'priced': 1, 'summed': 1}
    assert _bits(grown) == _bits(_oracle(rows))
    # An unsettled tail is folded afresh on every frame; the run is not.
    rows.append(_round(2001))
    calls.update(priced=0, summed=0)
    _fold(rows)
    tail = _fold(rows)
    assert calls == {'priced': 2, 'summed': 2}
    assert _bits(tail) == _bits(_oracle(rows))


# ── Every dependency of the memo refolds ────────────────────────────────────

def test_a_pricing_change_refolds_the_run(priced_provider):
    rows = [settle(_round(index)) for index in range(12)]
    before = _fold(rows)
    set_provider_pricing(_PROVIDER, _MODEL, {'input': 3.0, 'output': 9.0})
    after = _fold(rows)
    assert after != before
    assert _bits(after) == _bits(_oracle(rows))


def test_patched_snapshot_rates_refold_without_a_revision(priced_provider, monkeypatch):
    rows = [settle(_round(index)) for index in range(12)]
    before = _fold(rows)
    data = dict(get_pricing_data(), usdToCny=11.0)
    monkeypatch.setattr(cost_module, 'get_pricing_data', lambda: dict(data))
    monkeypatch.setattr(oracle, 'get_pricing_data', lambda: dict(data))
    after = _fold(rows)
    assert after['costCny'] != before['costCny']
    assert _bits(after) == _bits(_oracle(rows))


def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def test_a_peak_multiplier_change_refolds_the_run(monkeypatch):
    rows = [settle(_round(index, model='deepseek-flash', provider=None))
            for index in range(12)]
    monkeypatch.setattr(peak_module, '_utc_now', lambda: _utc(2026, 9, 11, 7, 0))
    peak = _fold(rows)
    assert _bits(peak) == _bits(_oracle(rows))
    monkeypatch.setattr(peak_module, '_utc_now', lambda: _utc(2026, 9, 11, 12, 0))
    off_peak = _fold(rows)
    assert off_peak['costUsd'] < peak['costUsd']
    assert _bits(off_peak) == _bits(_oracle(rows))


def test_a_replaced_round_refolds_the_run(priced_provider):
    rows = [settle(_round(index)) for index in range(12)]
    before = _fold(rows)
    rows[5] = settle({**rows[5], 'usage': _usage(105)})
    after = _fold(rows)
    assert after != before
    assert _bits(after) == _bits(_oracle(rows))


def test_an_equal_but_distinct_round_is_not_trusted_by_value(priced_provider):
    """``True == 1``: a value-equal twin that prices differently must refold."""

    def one_hour_write(share):
        return {'round': 2, 'model': 'claude-fold-test', 'provider_id': _PROVIDER,
                'usage': {'input_tokens': 1_000, 'output_tokens': 10,
                          'cache_creation_input_tokens': 4_000,
                          'cache_creation': {'ephemeral_1h_input_tokens': share}}}

    rows = [settle(_round(index)) for index in range(6)]
    rows[2] = settle(one_hour_write(1))
    before = _fold(rows)
    rows[2] = settle(one_hour_write(True))
    assert rows[2] == one_hour_write(1)
    after = _fold(rows)
    assert after != before
    assert _bits(after) == _bits(_oracle(rows))


def test_appended_rounds_resume_and_a_shorter_or_reordered_ledger_refolds(priced_provider):
    rows = [settle(_round(index)) for index in range(12)]
    _fold(rows)
    rows += [settle(_round(index)) for index in range(12, 15)]
    assert _bits(_fold(rows)) == _bits(_oracle(rows))
    shorter = rows[:9]
    assert _bits(_fold(shorter)) == _bits(_oracle(shorter))
    _fold(rows)
    reordered = [rows[0], rows[2], rows[1], *rows[3:]]
    assert _bits(_fold(reordered)) == _bits(_oracle(reordered))
    dropped_front = rows[1:]
    assert _bits(_fold(dropped_front)) == _bits(_oracle(dropped_front))


def test_an_unsettled_last_round_is_folded_afresh_after_in_place_edits(priced_provider):
    rows = [settle(_round(index)) for index in range(12)]
    live = _round(12)
    rows.append(live)
    before = _fold(rows)
    assert _bits(before) == _bits(_oracle(rows))
    live['usage'] = _usage(99)
    live['model'] = 'other-model'
    after = _fold(rows)
    assert after != before
    assert _bits(after) == _bits(_oracle(rows))


def test_a_live_round_ends_the_run_even_with_settled_rounds_after_it(priced_provider):
    rows = [settle(_round(index)) for index in range(6)]
    live = _round(6)
    rows += [live, *(settle(_round(index)) for index in range(7, 12))]
    _fold(rows)
    live['usage'] = _usage(70)
    assert _bits(_fold(rows)) == _bits(_oracle(rows))
    rows.append(settle(_round(12)))
    assert _bits(_fold(rows)) == _bits(_oracle(rows))


def test_frozen_and_live_cost_folds_never_resume_each_other(priced_provider):
    rows = [settle(_round(index, cost=_frozen_cost(index))) for index in range(12)]
    live = _fold(rows)
    frozen = _fold(rows, use_frozen_costs=True)
    assert frozen != live
    assert _bits(frozen) == _bits(_oracle(rows, use_frozen_costs=True))
    assert _bits(_fold(rows)) == _bits(live) == _bits(_oracle(rows))
    rows.append(settle(_round(12, cost=_frozen_cost(12))))
    assert _bits(_fold(rows, use_frozen_costs=True)) == _bits(
        _oracle(rows, use_frozen_costs=True))


def test_a_fallback_change_refolds_rounds_without_their_own_route(priced_provider):
    rows = [settle(_round(index, model=None, provider=None)) for index in range(12)]
    first = _fold(rows, fallback_model_id=_MODEL, fallback_provider_id=_PROVIDER)
    second = _fold(rows, fallback_model_id='other-model', fallback_provider_id=_PROVIDER)
    assert first != second
    assert _bits(second) == _bits(_oracle(
        rows, fallback_model_id='other-model', fallback_provider_id=_PROVIDER))
    assert _bits(_fold(rows, fallback_model_id=_MODEL, fallback_provider_id=None)) == _bits(
        _oracle(rows, fallback_model_id=_MODEL, fallback_provider_id=None))


def test_a_round_replaced_during_a_fold_cannot_plant_a_stale_run(priced_provider, monkeypatch):
    """A concurrent copy-on-write writer must not pair new identities with old sums."""
    rows = [settle(_round(index)) for index in range(12)]
    replacement = settle({**rows[1], 'usage': _usage(101)})
    real_cost = cost_module._LedgerPricing.round_cost

    def writer_during_fold(self, round_entry, *args):
        if round_entry is rows[-1]:
            rows[1] = replacement
        return real_cost(self, round_entry, *args)

    monkeypatch.setattr(cost_module._LedgerPricing, 'round_cost', writer_during_fold)
    compute_api_rounds_cost(rows)
    monkeypatch.setattr(cost_module._LedgerPricing, 'round_cost', real_cost)
    assert rows[1] is replacement
    assert _bits(_fold(rows)) == _bits(_oracle(rows))
