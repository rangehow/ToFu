"""tests/test_cache_accounting_convention.py — cache-token accounting correctness.

Covers FOUR defects found by auditing real gateway payloads pulled from
``task_events`` (4000 persisted ``round_usage`` rows, 4 models, 2026-07-25):

1. **The ``<=`` convention cliff (BILLING).** ``compute_cost`` decided the
   vendor convention with ``if inp <= cache_write + cache_read``. On the
   OpenAI-compat wire ``prompt_tokens`` ALREADY INCLUDES the cached tokens, so
   a round that reads back its ENTIRE prompt (``cached == prompt_tokens``)
   satisfies ``<=`` and is misread as Anthropic — the cache is then ADDED on
   top of a total that already contained it and the whole prefix is re-priced
   at the full uncached rate. Measured blast radius: prompt=82843 /
   cached=82843 jumps $0.0439 → $0.4581 (**10.4x**). Production has already
   been observed at ``margin=1`` (prompt=428603, cached=428602 on
   aws.claude-opus-4.7) — one token from a 10x over-charge.

2. **The hit% double-count (MONITORING).** ``log_round_cache_stats`` computed
   ``total_input = prompt_tokens + cache_write + cache_read`` unconditionally.
   Under the OpenAI convention that counts the cached tokens TWICE, halving
   every reported hit rate — which is why the live logs cluster on a physically
   impossible ``hit=50%`` (a 99.998% hit reported as 50%).

3. **Cold rounds were invisible (MONITORING).** The same function returned
   early when ``not cache_write and not cache_read`` — so a round that missed
   the cache ENTIRELY, i.e. the single most expensive kind of round, emitted no
   line at all. Two 20+ round Opus-5 sessions burned a full context each with
   zero ``[CacheStats]`` evidence.

4. **Write-gated logic is inert on this gateway (DEAD CODE).** The sankuai
   AIGC gateway reports ``cache_write_tokens`` but pins it to 0 on every model
   (231/231 rounds). Every predicate gated on a large ``cache_write`` therefore
   can never fire here. These tests PIN that fact so nobody tunes a threshold
   on a branch that is structurally unreachable in this deployment.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_cache_accounting_convention.py -v
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cost import (  # noqa: E402
    compute_cost,
    normalize_usage,
    split_input_tokens,
    usage_cache_convention,
)

pytestmark = pytest.mark.unit


# ── Real payload shapes, copied verbatim from persisted task_events ──

def _openai_wire(prompt_tokens: int, cached: int) -> dict:
    """The sankuai AIGC gateway's OpenAI-compat usage shape.

    ``prompt_tokens`` is the TOTAL (cache included); the hit is reported in the
    nested ``prompt_tokens_details.cached_tokens``. ``cache_write_tokens`` is
    present but always 0 — the gateway does not meter cache creation.
    """
    return {
        'prompt_tokens': prompt_tokens,
        'completion_tokens': 100,
        'cache_read_tokens': cached,
        'cache_write_tokens': 0,
        'cached_tokens': cached,
        'effectiveCachedTokens': cached,
        'input_tokens': 0,
        'output_tokens': 0,
        'prompt_tokens_details': {'cached_tokens': cached},
        'total_tokens': prompt_tokens + 100,
    }


def _anthropic_wire(uncached: int, cache_read: int, cache_write: int = 0) -> dict:
    """Anthropic-native usage: ``input_tokens`` is the UNCACHED residual."""
    u = {
        'input_tokens': uncached,
        'output_tokens': 100,
        'cache_read_input_tokens': cache_read,
    }
    if cache_write:
        u['cache_creation_input_tokens'] = cache_write
    return u


_MODEL = 'aws.claude-opus-4.8'


# ── Defect 1: the convention cliff ──────────────────────────────────────

def test_full_cache_hit_is_not_repriced_as_uncached():
    """cached == prompt_tokens must NOT flip the engine to the Anthropic branch.

    This is the exact cliff. Under the old ``inp <= cw + cr`` test the equality
    case took the Anthropic branch and re-added the cache on top of a total
    that already contained it.
    """
    u = _openai_wire(prompt_tokens=82843, cached=82843)
    cc = compute_cost(u, model_id=_MODEL)
    assert cc['totalInputTokens'] == 82843, (
        'total input must equal prompt_tokens on the OpenAI wire — got %s '
        '(the cache was added on top of a total that already contained it)'
        % cc['totalInputTokens'])
    assert cc['inputTokens'] == 0, (
        'a fully-cached round has ZERO uncached input; got %s'
        % cc['inputTokens'])


def test_full_cache_hit_costs_the_same_as_a_one_token_miss():
    """Continuity across the boundary: 1 uncached token must not cost 10x.

    cached=82842 (1 token uncached) and cached=82843 (0 uncached) differ by a
    SINGLE token, so their costs must differ by at most that token's price.
    Pre-fix they differed by 10.4x.
    """
    near = compute_cost(_openai_wire(82843, 82842), model_id=_MODEL)['costUsd']
    exact = compute_cost(_openai_wire(82843, 82843), model_id=_MODEL)['costUsd']
    assert exact <= near, (
        'a FULL cache hit (%s) must never cost more than a near-full hit (%s)'
        % (exact, near))
    assert abs(near - exact) < 0.01, (
        'one uncached token moved the price by $%.4f — the convention '
        'detection flipped at the boundary' % abs(near - exact))


def test_production_margin_of_one_stays_on_the_openai_branch():
    """The closest margin actually observed in production (prompt-cached == 1).

    aws.claude-opus-4.7, prompt=428603 cached=428602. One more cached token
    would have tipped the old predicate.
    """
    u = _openai_wire(prompt_tokens=428603, cached=428602)
    assert usage_cache_convention(u) == 'openai'
    cc = compute_cost(u, model_id='aws.claude-opus-4.7')
    assert cc['totalInputTokens'] == 428603
    assert cc['inputTokens'] == 1


def test_anthropic_native_convention_is_preserved():
    """REGRESSION GUARD: the Anthropic split must be untouched by the fix.

    ``input_tokens`` is the uncached residual, so the total is
    input + cache_read + cache_write.
    """
    u = _anthropic_wire(uncached=2, cache_read=82841, cache_write=1200)
    assert usage_cache_convention(u) == 'anthropic'
    cc = compute_cost(u, model_id=_MODEL)
    assert cc['inputTokens'] == 2
    assert cc['totalInputTokens'] == 2 + 82841 + 1200


def test_anthropic_fully_cached_round_has_zero_input_tokens():
    """Anthropic reports input_tokens=0 on a full hit — still 'anthropic'."""
    u = _anthropic_wire(uncached=0, cache_read=82841)
    assert usage_cache_convention(u) == 'anthropic'
    assert split_input_tokens(u) == (0, 82841)


def test_convention_is_decided_structurally_not_by_magnitude():
    """The detector must key on WHICH KEYS are present, not on their sizes.

    A magnitude comparison is inherently fragile — the whole point of the fix.
    Two payloads with IDENTICAL numbers but different key spellings must be
    classified differently.
    """
    same_numbers_openai = _openai_wire(prompt_tokens=1000, cached=1000)
    same_numbers_anthropic = _anthropic_wire(uncached=1000, cache_read=1000)
    assert usage_cache_convention(same_numbers_openai) == 'openai'
    assert usage_cache_convention(same_numbers_anthropic) == 'anthropic'
    assert split_input_tokens(same_numbers_openai) == (0, 1000)
    assert split_input_tokens(same_numbers_anthropic) == (1000, 2000)


def test_no_cache_activity_passes_input_through_unchanged():
    """A cold round has uncached == total == prompt_tokens, under either wire."""
    assert split_input_tokens({'prompt_tokens': 5000}) == (5000, 5000)
    assert split_input_tokens({'input_tokens': 5000}) == (5000, 5000)
    assert split_input_tokens(None) == (0, 0)


# ── Defect 2 + 3: the per-round log line ────────────────────────────────

class _Capture(logging.Handler):
    """Collect formatted records off a specific logger (no caplog dependency)."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        # getMessage() already applies record.args — re-applying % would
        # choke on literal '%' in the formatted output (e.g. "hit=100%").
        self.lines.append(record.getMessage())


@pytest.fixture()
def roi_log():
    from lib.tasks_pkg.cache_tracking import _roi
    cap = _Capture()
    _roi.logger.addHandler(cap)
    prev = _roi.logger.level
    _roi.logger.setLevel(logging.INFO)
    try:
        yield cap
    finally:
        _roi.logger.removeHandler(cap)
        _roi.logger.setLevel(prev)


def test_hit_pct_reports_the_true_read_ratio(roi_log):
    """A 99.998% cache hit must report ~100%, not the double-counted 50%.

    This is the live ``hit=50%`` cluster: 160 log lines pinned at exactly 50%
    is not a coincidence, it is ``cr / (cr + cr)``.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    log_round_cache_stats('convtest1', 0, _openai_wire(82843, 82841),
                          model=_MODEL, tid='t1234567')
    assert roi_log.lines, 'no CacheStats line emitted'
    line = roi_log.lines[-1]
    assert 'hit=100%' in line, 'expected hit=100%%, got: %s' % line


def test_anthropic_hit_pct_is_unchanged_by_the_fix(roi_log):
    """REGRESSION GUARD: the Anthropic split already reported correctly."""
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    # uncached=1000, read=1000 -> total 2000 -> 50% is the TRUE ratio here.
    log_round_cache_stats('convtest2', 0, _anthropic_wire(1000, 1000),
                          model=_MODEL, tid='t1234567')
    assert 'hit=50%' in roi_log.lines[-1], roi_log.lines[-1]


def test_a_total_miss_round_still_emits_a_line(roi_log):
    """The most EXPENSIVE round must not be the only silent one.

    Two Opus-5 sessions (23 and 22 rounds) ran to completion with
    total_read=0 and emitted ZERO per-round lines, because the early return
    fired on exactly the rounds worth seeing.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    log_round_cache_stats('convtest3', 4, _openai_wire(120000, 0),
                          model=_MODEL, tid='t1234567')
    assert roi_log.lines, 'a 120k-token full-miss round emitted NO log line'
    line = roi_log.lines[-1]
    assert 'cache_r=0' in line and 'hit=0%' in line, line
    assert 'COLD' in line, 'a full-miss round should be greppable: %s' % line


def test_trivially_small_rounds_stay_quiet(roi_log):
    """The anti-spam intent of the old gate is preserved for tiny prompts.

    A prompt below the largest documented minimum-cacheable length cannot be
    cached by any Claude tier, so its miss is not actionable signal.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    log_round_cache_stats('convtest4', 0, {'prompt_tokens': 200},
                          model=_MODEL, tid='t1234567')
    assert not roi_log.lines, 'a 200-token round should not be logged'


# ── Defect 1b: the billing adapter must not destroy the convention ──────

def test_billing_scalars_do_not_become_an_overcharge():
    """The wallet debit must equal the displayed price for an OpenAI round.

    ``compute_request_cost`` takes loose SCALARS. It used to re-encode them
    under hardcoded Anthropic key names, which flips their meaning: a gpt-4o
    round of 10000 TOTAL prompt tokens with 6000 cached was billed as 10000
    UNCACHED + 6000 cache on top — 52500µ against a displayed 37500µ, a 40%
    over-charge on every cached OpenAI round.
    """
    from lib.billing.cost import MICRO_PER_USD, compute_request_cost
    u = {'prompt_tokens': 10000, 'completion_tokens': 2000,
         'cache_read_tokens': 6000}
    cc = compute_cost(u, model_id='gpt-4o')
    displayed = round((cc['inputCostUsd'] + cc['outputCostUsd']
                       + cc['cacheWriteCostUsd'] + cc['cacheReadCostUsd'])
                      * MICRO_PER_USD)
    billed = compute_request_cost(
        'gpt-4o', input_tokens=10000, output_tokens=2000,
        cache_read_tokens=6000, margin=0.0)
    assert billed.base_micro == displayed, (
        'wallet debit %s != displayed %s — the billing adapter re-spelled the '
        'usage and changed its convention' % (billed.base_micro, displayed))


def test_synthesize_usage_round_trips_both_conventions():
    """Scalars must be spelled so the engine reads them back unchanged."""
    from lib.cost import synthesize_usage
    # Cache-inclusive total (OpenAI): cache is a subset of input.
    openai_like = synthesize_usage(input_tokens=10000, output_tokens=100,
                                   cache_read_tokens=6000)
    assert usage_cache_convention(openai_like) == 'openai'
    assert split_input_tokens(openai_like) == (4000, 10000)
    # Residual (Anthropic): cache exceeds the input, so input can't be a total.
    anthropic_like = synthesize_usage(input_tokens=500, output_tokens=100,
                                      cache_read_tokens=40000,
                                      cache_write_tokens=8000)
    assert usage_cache_convention(anthropic_like) == 'anthropic'
    assert split_input_tokens(anthropic_like) == (500, 48500)


def test_hybrid_payload_with_impossible_cache_reads_as_residual():
    """cache > prompt_tokens is arithmetically impossible under OpenAI rules.

    Some providers emit ``prompt_tokens`` carrying residual semantics. Since
    the cache cannot exceed a total that contains it, such a payload must be
    read as Anthropic — otherwise the hit rate exceeds 100% (observed live as
    ``hit=15000%``).
    """
    u = {'prompt_tokens': 100, 'cache_write_tokens': 5000,
         'cache_read_tokens': 15000}
    assert usage_cache_convention(u) == 'anthropic'
    uncached, total = split_input_tokens(u)
    assert (uncached, total) == (100, 20100)
    assert round(15000 / total * 100) <= 100, 'hit rate exceeded 100%'


# ── Defect 4: write-gated logic is structurally inert on this gateway ───

def test_gateway_never_reports_cache_write_so_floor_collapse_cannot_fire():
    """PIN: ``is_floor_collapse`` is unreachable on the sankuai gateway.

    It requires ``cache_write > 20000``, but the gateway pins
    ``cache_write_tokens`` to 0 on every model (231/231 observed rounds).
    Do NOT tune _FLOOR_WRITE_LO against this deployment — the branch is dead
    here, and a "fix" that makes it fire would be fitting noise.
    """
    from lib.tasks_pkg import floor_retry as fr
    # A round that WOULD be a textbook floor collapse if writes were metered.
    u = _openai_wire(prompt_tokens=300000, cached=74000)
    assert normalize_usage(u)['cache_write'] == 0
    assert fr.is_floor_collapse(u) is False


def test_no_reuse_classifiers_cannot_fire_without_metered_writes():
    """PIN: ``_classify_break``'s no_reuse / partial_no_reuse are dead here.

    Both require ``cache_write >= _MIN_NO_REUSE_TOKENS``.
    """
    from lib.tasks_pkg.cache_tracking._detect import _classify_break
    api_break, no_reuse, partial = _classify_break(
        call_count=5, was_compaction=False,
        prev_cache_read=200000, cache_read=0,
        prev_cache_write=0, cache_write=0,
        prev_prefix_tokens=200000,
    )
    assert no_reuse is False, 'no_reuse fired without a metered write'
    assert partial is False, 'partial_no_reuse fired without a metered write'
    # api_break MUST still fire — it is read-driven and remains our only
    # working break signal on this gateway.
    assert api_break is True, (
        'api_break is the ONLY live break detector on a write-blind gateway; '
        'if this goes False we have no cache diagnostics left')
