"""Guards for debug/cache_waste_report.py — the reproducible cache-waste table.

WHY THIS EXISTS
===============
The 2026-07-29 cache-cost audit produced its distribution from throwaway shell
one-liners and was wrong twice before it was right. Two of those errors were
structural, and this file pins both so the next person cannot repeat them:

  * **Falsy-zero percentile.** The original printer used ``if p50 else '-'``,
    so a legitimate ``gap_s == 0.0`` rendered as "missing". That is what hid
    239 cold-start rounds inside another bucket until the arithmetic stopped
    adding up. ``percentile()`` must return ``0.0`` as a value and ``None``
    ONLY for an empty input.

  * **Non-waste folded into the recoverable denominator.** A TTL expiry rebuilt
    an entry that expired on its own schedule; a cold start had no predecessor
    to read back. Neither is money anyone could have saved. Counting them
    inflates the number people then go chasing — the exact distortion the
    ttl_expiry classifier fix (b402b696) removed from the live path.

Also guarded: rates are DERIVED from lib.cost.compute_cost rather than
hand-written, because a hand-derived constant was 27x off in the original audit.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, '..', 'scripts', 'cache_waste_report.py')


def _load():
    spec = importlib.util.spec_from_file_location('cache_waste_report', _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cwr = _load()


def _rec(bucket, *, call, gap, write, read=0):
    return {'bucket': bucket, 'call': call, 'gap_s': gap,
            'cache_write': write, 'cache_read': read}


class TestPercentileTreatsZeroAsANumber:
    """The falsy-zero trap. gap_s == 0.0 is a real measurement."""

    def test_zero_is_returned_not_swallowed(self):
        assert cwr.percentile([0.0, 0.0, 0.0], 50) == 0.0

    def test_zero_is_not_none(self):
        """The distinction that matters: 0.0 is data, None is absence."""
        assert cwr.percentile([0.0], 50) is not None

    def test_none_only_for_empty_input(self):
        assert cwr.percentile([], 50) is None

    def test_ordinary_percentiles_still_correct(self):
        vals = [1.0, 2.0, 3.0, 4.0, 100.0]
        assert cwr.percentile(vals, 50) == 3.0
        assert cwr.percentile(vals, 90) == 100.0

    def test_mixed_zero_and_nonzero_sorts_numerically(self):
        """A bucket mixing cold (0s) and warm rounds must not lose the zeros."""
        assert cwr.percentile([0.0, 0.0, 10.0, 20.0], 50) == 10.0


class TestColdStartIsNotWaste:
    """A round with no predecessor had nothing to read back."""

    def test_cold_start_gets_its_own_bucket(self):
        assert cwr.bucket_of(_rec('no_break', call=1, gap=0.0, write=99999)) \
            == cwr.COLD_START_LABEL

    def test_cold_start_is_excluded_from_recoverable(self):
        """The load-bearing assertion: cold-start tokens are reported, but
        contribute ZERO to the recoverable total."""
        rep = cwr.build_report(
            [_rec('no_break', call=1, gap=0.0, write=1_000_000)],
            min_write=20000, w_rate=0.001, r_rate=0.0001)
        assert rep['true_recoverable_cny'] == 0
        row = rep['rows'][0]
        assert row['bucket'] == cwr.COLD_START_LABEL
        assert row['is_waste'] is False
        assert row['n'] == 1 and row['wasted_tokens'] == 1_000_000
        assert row['paid_cny'] > 0, 'the spend still has to be visible'

    def test_ttl_expiry_is_excluded_from_recoverable(self):
        rep = cwr.build_report(
            [_rec('ttl_expiry', call=9, gap=400.0, write=1_000_000)],
            min_write=20000, w_rate=0.001, r_rate=0.0001)
        assert rep['true_recoverable_cny'] == 0
        assert rep['rows'][0]['is_waste'] is False

    def test_a_real_miss_still_counts_as_recoverable(self):
        """COMPLEMENT — excluding must not swallow genuine waste."""
        rep = cwr.build_report(
            [_rec('upstream_identical', call=9, gap=38.0, write=1_000_000)],
            min_write=20000, w_rate=0.001, r_rate=0.0001)
        assert rep['true_recoverable_cny'] > 0
        assert rep['rows'][0]['is_waste'] is True
        assert rep['rows'][0]['share_of_recoverable'] == pytest.approx(100.0)

    def test_share_denominator_omits_the_excluded_classes(self):
        """The number people actually quote. One real miss + one cold start +
        one TTL of equal size: the real miss must be 100% of recoverable, not
        33% — otherwise the excluded rounds are still silently in the pot."""
        rep = cwr.build_report(
            [_rec('upstream_identical', call=9, gap=38.0, write=1_000_000),
             _rec('no_break', call=1, gap=0.0, write=1_000_000),
             _rec('ttl_expiry', call=9, gap=400.0, write=1_000_000)],
            min_write=20000, w_rate=0.001, r_rate=0.0001)
        real = [r for r in rep['rows'] if r['bucket'] == 'upstream_identical'][0]
        assert real['share_of_recoverable'] == pytest.approx(100.0)

    def test_a_later_round_at_zero_gap_is_not_a_cold_start(self):
        """COMPLEMENT — cold start requires BOTH no predecessor and no gap.
        A call>1 round is a real miss even if the gap rounds to zero, so the
        exclusion cannot be used to launder mid-conversation misses."""
        assert cwr.bucket_of(
            _rec('upstream_identical', call=7, gap=0.0, write=99999)) \
            == 'upstream_identical'


class TestRatesComeFromTheProduct:
    def test_rates_are_derived_from_lib_cost(self):
        """No hand-written CNY constant: the write rate must match what the
        billing engine itself charges for the same tokens."""
        from lib.cost import compute_cost
        w, r = cwr.derive_rates('claude-opus-5')
        probe = compute_cost(
            {'prompt_tokens': 0, 'completion_tokens': 0,
             'cache_creation_input_tokens': 1_000_000}, 'claude-opus-5', None)
        assert w == pytest.approx(float(probe['costCny']) / 1_000_000)
        assert 0 < r < w, 'reading cache must be cheaper than writing it'

    def test_unknown_model_fails_loudly(self):
        """A silent fallback rate is how a 27x error survives review."""
        with pytest.raises(SystemExit):
            cwr.derive_rates('definitely-not-a-real-model-xyz')


class TestMinWriteFilter:
    def test_small_writes_are_not_counted(self):
        rep = cwr.build_report(
            [_rec('upstream_identical', call=9, gap=38.0, write=100)],
            min_write=20000, w_rate=0.001, r_rate=0.0001)
        assert rep['zero_readback_rounds'] == 0

    def test_rounds_that_read_back_are_not_counted(self):
        rep = cwr.build_report(
            [_rec('no_break', call=9, gap=38.0, write=1_000_000, read=500_000)],
            min_write=20000, w_rate=0.001, r_rate=0.0001)
        assert rep['zero_readback_rounds'] == 0
