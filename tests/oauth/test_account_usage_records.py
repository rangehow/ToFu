"""Per-account request attribution from Tofu's local request records."""
from types import SimpleNamespace

import pytest

from lib.oauth.account_usage import load_account_usage, summarize_request_records

pytestmark = pytest.mark.unit

A = 'oa_' + 'a' * 32
B = 'oa_' + 'b' * 32
C = 'oa_' + 'c' * 32
REMOVED = 'oa_' + 'd' * 32


def record(receipt, account=None, *, prompt=10, completion=5, cost=None, at=None,
           provider='codex'):
    row = {
        'usageReceiptId': receipt,
        'subscriptionProvider': provider,
        'usage': {'prompt_tokens': prompt, 'completion_tokens': completion},
    }
    if account is not None:
        row['accountId'] = account
    if cost is not None:
        row['cost'] = {'costUsd': cost}
    if at is not None:
        row['requestTimestampMs'] = at
    return row


def test_three_configured_accounts_get_exact_local_counts():
    summary = summarize_request_records([
        record('r1', A, prompt=100, completion=20, cost=0.02, at=1_000),
        record('r2', A, prompt=50, completion=10, cost=0.01, at=3_000),
        record('r3', B, prompt=7, completion=3, at=2_000),
        record('r4', C, prompt=1, completion=1, cost=0.5, at=4_000),
    ], account_ids=[A, B, C], provider='codex')

    assert summary['source'] == 'local_record'
    assert summary['accounts'][A] == {
        'requests': 2, 'tokens': 180, 'costUsd': pytest.approx(0.03),
        'pricedRequests': 2, 'firstUsedAtMs': 1_000, 'lastUsedAtMs': 3_000,
    }
    assert summary['accounts'][B] == {
        'requests': 1, 'tokens': 10, 'costUsd': 0.0,
        'pricedRequests': 0, 'firstUsedAtMs': 2_000, 'lastUsedAtMs': 2_000,
    }
    assert summary['accounts'][C]['requests'] == 1
    assert summary['unknown']['requests'] == 0
    assert summary['window'] == {'fromMs': 1_000, 'toMs': 4_000}


def test_unresolvable_slot_is_unknown_and_never_inferred():
    summary = summarize_request_records([
        record('missing', None, at=10),
        record('removed', REMOVED, at=20),
        record('other-provider', A, provider='claude', at=30),
    ], account_ids=[A, B], provider='codex')

    assert summary['accounts'][A]['requests'] == 0
    assert summary['accounts'][B]['requests'] == 0
    assert summary['unknown']['requests'] == 2
    assert summary['unknown']['firstUsedAtMs'] == 10
    assert summary['unknown']['lastUsedAtMs'] == 20


def test_rename_keeps_counts_and_remove_does_not_reattribute():
    rows = [record('a', A, at=1), record('b', B, at=2)]

    # A rename changes only the alias; the opaque account id is the identity.
    renamed = summarize_request_records(rows, account_ids=[A, B], provider='codex')
    assert renamed['accounts'][A]['requests'] == 1
    assert renamed['accounts'][B]['requests'] == 1

    # Removing B moves its old rows to unknown; it never moves them to A.
    removed = summarize_request_records(rows, account_ids=[A], provider='codex')
    assert removed['accounts'][A]['requests'] == 1
    assert B not in removed['accounts']
    assert removed['unknown']['requests'] == 1


def test_the_same_receipt_is_counted_once():
    summary = summarize_request_records([
        record('same', A, at=1),
        record('same', A, at=1),
    ], account_ids=[A], provider='codex')
    assert summary['accounts'][A]['requests'] == 1


def test_dispatch_records_the_bound_account_as_local_evidence():
    from lib.llm_dispatch.slot import Slot
    from lib.tasks_pkg.llm_fallback._usage import _response_route_projection

    bound = Slot(key_name='k', api_key='synthetic', model='codex-model',
                 capabilities=set(), oauth='codex',
                 subscription_account=(SimpleNamespace(account_id=A), None))
    assert bound.subscription_attribution() == {
        'subscription_provider': 'codex', 'account_id': A}
    unbound = Slot(key_name='k', api_key='synthetic', model='codex-model',
                   capabilities=set(), oauth='codex')
    assert unbound.subscription_attribution() == {'subscription_provider': 'codex'}
    api_key_slot = Slot(key_name='k', api_key='synthetic', model='m', capabilities=set())
    assert api_key_slot.subscription_attribution() == {}

    projected = _response_route_projection('codex-model', {'_dispatch': {
        'model': 'codex-model', 'provider_id': 'oauth_codex',
        'account_id': A, 'subscription_provider': 'codex'}})
    assert projected['accountId'] == A
    assert projected['subscriptionProvider'] == 'codex'


def test_loader_reads_only_stored_request_rows():
    projections = [SimpleNamespace(turns=[{
        'apiRounds': [
            record('r1', A, at=1),
            {'model': 'not-a-subscription-round'},
        ],
    }])]

    class Repository:
        def scan_turn_usage(self, **kwargs):
            assert kwargs['user_id'] == 7
            return 1, iter(projections)

    summary = load_account_usage(7, provider='codex', account_ids=[A],
                                 repository=Repository(), now_ms=5_000)
    assert summary['accounts'][A]['requests'] == 1
    assert summary['unknown']['requests'] == 0
    assert summary['conversationsScanned'] == 1
    assert summary['truncated'] is False