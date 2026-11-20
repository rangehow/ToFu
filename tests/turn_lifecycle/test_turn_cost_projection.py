"""Authoritative settled-cost fold into the durable turn projection.

Root cause (2026-08-29, conversation mtd9ci53zq3xfm): ``_task_projection``
folded ``usage`` and the per-round ``apiRounds`` ledger but never an
authoritative top-level ``cost`` total. On reload the finish footer therefore
had no ``msg.cost`` and fell back to the client-side ``calcCostCny``
micro-batch; that fill mutates no Turn fact, so the surface footer compare
skipped the re-render and the cost tag + hover breakdown never appeared.

The fold mirrors the done-event stamp in ``orchestrator/_finalize`` so live
and reload paths read the same number from the same ``lib.cost`` formula;
``apiRounds`` stays the per-round breakdown ledger.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('usage', [None, {}, {'prompt_tokens': 7}])
def test_root_and_child_usage_projection_is_contract_valid_without_invented_cost(usage):
    from copy import deepcopy
    from lib.conversation_sync.validation import decode
    from lib.turn_projection_patch import normalize_projection_document

    source = {'content': 'Preserve output', 'usage': usage}
    before = deepcopy(source)
    projection = normalize_projection_document(source)
    decode('TurnProjection', projection)
    assert source == before
    if usage is None:
        assert 'usage' not in projection
    else:
        assert projection['usage'] == usage
    assert 'cost' not in projection
    assert normalize_projection_document(projection) == projection


def _task():
    return {'id': 'task-costfold1', 'convId': 'conv-1', 'config': {}}


def test_projection_folds_settled_cost_from_usage():
    from lib.turn_lifecycle import _task_projection

    task = _task()
    task['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 500}
    task['model'] = 'gpt-4o'
    projection = _task_projection(task, {})
    cost = projection.get('cost')
    assert cost is not None
    assert cost['costCny'] > 0
    assert cost['totalInputTokens'] == 1000
    assert cost['outputTokens'] == 500


def test_projection_without_usage_has_no_cost_key():
    from lib.turn_lifecycle import _task_projection

    projection = _task_projection(_task(), {})
    assert 'cost' not in projection


def test_projection_zero_usage_has_no_cost_key():
    from lib.turn_lifecycle import _task_projection

    task = _task()
    task['usage'] = {'prompt_tokens': 0, 'completion_tokens': 0}
    projection = _task_projection(task, {})
    assert 'cost' not in projection


def test_cost_fold_recomputes_carried_stale_cost():
    """The projection is cumulative (``dict(previous)``), but the fold
    RE-DERIVES cost from the carried usage on every write — a stale carried
    value can never survive next to different math (the single lib.cost
    formula owns the number)."""
    from lib.cost import compute_cost
    from lib.turn_lifecycle import _task_projection

    usage = {'prompt_tokens': 1000, 'completion_tokens': 500}
    previous = {'usage': usage, 'model': 'gpt-4o',
                'cost': {'costCny': 999.0, 'totalInputTokens': 1}}
    projection = _task_projection(_task(), previous)
    expected = compute_cost(usage, model_id='gpt-4o')
    assert projection['cost']['costCny'] == expected['costCny']
    assert projection['cost']['totalInputTokens'] == 1000


def test_mixed_model_rounds_are_priced_individually_before_aggregation():
    from lib.cost import compute_cost
    from lib.turn_lifecycle import _task_projection

    rounds = [
        {'model': 'gpt-4o',
         'usage': {'prompt_tokens': 10_000, 'completion_tokens': 1_000,
                   'cache_read_tokens': 8_000}},
        {'model': 'gpt-5.6-luna',
         'usage': {'prompt_tokens': 20_000, 'completion_tokens': 2_000,
                   'cache_read_tokens': 15_000}},
    ]
    task = _task()
    task.update({
        'model': 'gpt-5.6-sol',
        'usage': {'prompt_tokens': 30_000, 'completion_tokens': 3_000,
                  'cache_read_tokens': 23_000},
        'apiRounds': rounds,
    })

    projection = _task_projection(task, {})
    expected = sum(
        compute_cost(entry['usage'], model_id=entry['model'])['costCny']
        for entry in rounds
    )
    wrongly_flattened = compute_cost(
        task['usage'], model_id='gpt-5.6-sol')['costCny']

    assert projection['cost']['costCny'] == pytest.approx(expected, abs=1e-4)
    assert projection['cost']['costCny'] != wrongly_flattened
    assert projection['cost']['pricingSource'] == 'api_round_aggregate'
    assert projection['cost']['pricingSnapshot']['models'] == [
        'gpt-4o', 'gpt-5.6-luna']
    assert projection['cost']['totalInputTokens'] == 30_000
    assert projection['cost']['cacheReadTokens'] == 23_000


def _settled_rounds(count, *, frozen=False):
    from lib.conversation_sync.settled_rows import settle
    from lib.cost import compute_cost

    rounds = []
    for index in range(1, count + 1):
        usage = {'prompt_tokens': 10_000 + index, 'completion_tokens': 500,
                 'cache_read_tokens': 8_000}
        row = {'round': index, 'model': 'gpt-4o', 'usage': usage}
        if frozen:
            row['cost'] = compute_cost(usage, model_id='gpt-4o',
                                       pricing_override={'input': 0.5, 'output': 1.0})
        rounds.append(settle(row))
    return rounds


def test_stored_quote_projection_is_identical_for_native_and_python_ledgers():
    import copy

    from lib.turn_lifecycle import _task_projection
    from tests.turn_lifecycle import _projection_oracle as oracle

    rounds = _settled_rounds(8, frozen=True)
    task = _task()
    task.update(model='gpt-4o', usage={'prompt_tokens': 1, 'completion_tokens': 1},
                apiRounds=rounds)
    live = _task_projection(task, {})['cost']
    task['_nativeFrozenCostProjection'] = True
    frozen = _task_projection(task, {})['cost']
    expected = oracle.compute_api_rounds_cost(
        copy.deepcopy(rounds), fallback_model_id='gpt-4o', use_frozen_costs=True)
    expected.update(cacheSavingsUsd=0.0, cacheSavingsCny=0.0)
    assert frozen == expected
    assert frozen == live
    del task['_nativeFrozenCostProjection']
    assert _task_projection(task, {})['cost'] == live == expected


def test_a_captured_frame_over_2000_settled_rounds_reprices_and_resanitizes_none(
        monkeypatch):
    """Delivery-thread frames cost O(changed rounds), not O(ledger), per frame."""
    import lib.cost as cost_module
    import lib.task_replay.persistence_projection as projection_module
    from lib.conversation_sync.settled_rows import settle
    from lib.turn_lifecycle import _task_projection, capture_task_projection_source

    calls = {'priced': 0, 'sanitized': 0}
    real_cost = cost_module._LedgerPricing.round_cost
    real_sanitize = projection_module._sanitized_settled_round

    def counting_cost(self, *args):
        calls['priced'] += 1
        return real_cost(self, *args)

    def counting_sanitize(row):
        calls['sanitized'] += 1
        return real_sanitize(row)

    monkeypatch.setattr(cost_module._LedgerPricing, 'round_cost', counting_cost)
    monkeypatch.setattr(projection_module, '_sanitized_settled_round', counting_sanitize)
    task = _task()
    task.update(model='gpt-4o', usage={'prompt_tokens': 1, 'completion_tokens': 1},
                apiRounds=_settled_rounds(2000), _attemptId='att-costfold1',
                status='running')
    event = {'type': 'phase', 'phase': 'waiting_model', 'emittedAt': 1_700_000_000_000}
    previous = _task_projection(capture_task_projection_source(task, event), {}, event)
    for _frame in range(2):
        calls.update(priced=0, sanitized=0)
        projection = _task_projection(
            capture_task_projection_source(task, event), previous, event)
        assert calls == {'priced': 0, 'sanitized': 0}
        assert projection['cost'] == previous['cost']
    task['apiRounds'] = [*task['apiRounds'], settle(
        {'round': 2001, 'model': 'gpt-4o', 'usage': {'prompt_tokens': 7, 'completion_tokens': 1}})]
    calls.update(priced=0, sanitized=0)
    grown = _task_projection(capture_task_projection_source(task, event), previous, event)
    assert calls['priced'] == 1
    assert grown['cost']['pricingSnapshot']['rounds'] == 2001


def test_cost_fold_failure_is_non_fatal(monkeypatch):
    """Pricing resolution is display-only: a failure must never break the
    projection write itself."""
    import lib.cost
    from lib.turn_lifecycle import _task_projection

    def _boom(*args, **kwargs):
        raise RuntimeError('pricing table exploded')

    monkeypatch.setattr(lib.cost, 'compute_cost', _boom)
    task = _task()
    task['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 500}
    projection = _task_projection(task, {})
    assert 'cost' not in projection
    assert projection['usage']['prompt_tokens'] == 1000
