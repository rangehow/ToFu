"""Per-account request attribution from Tofu's own local request records.

This module answers a deliberately narrow question: of the subscription
requests Tofu dispatched and recorded under one owner, which configured
subscription account did each record name? It never asks a provider for usage
and never reconciles billing. Every number it returns is a local record.

Authority and limits
--------------------
The local record is the persisted per-request ``apiRounds`` ledger projected by
``conversation.turn_usage``. A round participates only when it carries the
explicit ``subscriptionProvider`` marker stamped at dispatch time. That marker
is evidence, not an inference: route names, model names, provider ids and
ordering are never used to decide that a request belonged to a subscription
account or which account served it.

A round is attributed to a configured account only when its stored
``accountId`` names one of the account ids supplied by the caller. Anything
else — no marker of a resolved binding, a malformed id, or a removed/unknown
account — is counted under ``unknown``. Renaming an account never changes its
opaque ``oa_...`` id, and removing it never re-attributes its old records to
another account.

Cost is included only where the local record stamped it. Token totals are the
same observed prompt+completion totals the cost engine reads; an incomplete
token receipt remains a lower bound and is never estimated here.
"""

from __future__ import annotations

import math
import time

UNKNOWN = 'unknown'
LOCAL_RECORD_SOURCE = 'local_record'
# ``ConversationRepository.list`` owns this hard upper bound; a scan that
# reaches it reports partial coverage instead of implying a complete history.
MAX_CONVERSATIONS = 10_000


def _bucket() -> dict:
    return {
        'requests': 0,
        'tokens': 0,
        'costUsd': 0.0,
        'pricedRequests': 0,
        'firstUsedAtMs': None,
        'lastUsedAtMs': None,
    }


def _request_time_ms(record: dict):
    value = record.get('requestTimestampMs')
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def _request_cost_usd(record: dict):
    cost = record.get('cost')
    if not isinstance(cost, dict):
        return None
    value = cost.get('costUsd')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _add_record(bucket: dict, record: dict) -> None:
    from lib.cost import observed_usage_totals

    tokens, _complete = observed_usage_totals(record.get('usage'))
    bucket['requests'] += 1
    bucket['tokens'] += max(0, int(tokens))
    cost = _request_cost_usd(record)
    if cost is not None:
        bucket['costUsd'] += cost
        bucket['pricedRequests'] += 1
    at = _request_time_ms(record)
    if at is None:
        return
    if bucket['firstUsedAtMs'] is None or at < bucket['firstUsedAtMs']:
        bucket['firstUsedAtMs'] = at
    if bucket['lastUsedAtMs'] is None or at > bucket['lastUsedAtMs']:
        bucket['lastUsedAtMs'] = at


def _receipt_identity(record: dict):
    receipt = record.get('usageReceiptId')
    return receipt if isinstance(receipt, str) and receipt else None


def _window(buckets) -> dict | None:
    firsts = [bucket['firstUsedAtMs'] for bucket in buckets
              if bucket['firstUsedAtMs'] is not None]
    lasts = [bucket['lastUsedAtMs'] for bucket in buckets
             if bucket['lastUsedAtMs'] is not None]
    if not firsts or not lasts:
        return None
    return {'fromMs': min(firsts), 'toMs': max(lasts)}


def summarize_request_records(records, *, account_ids, provider: str) -> dict:
    """Attribute stored request rows to configured accounts or ``unknown``.

    Pure: callers supply already-persisted per-request mappings (the
    ``apiRounds`` shape). Only rows carrying ``subscriptionProvider`` equal to
    *provider* are in scope; everything else belongs to another feature and is
    ignored rather than guessed into an account.
    """
    configured = sorted(account_id for account_id in account_ids
                        if isinstance(account_id, str) and account_id)
    accounts = {account_id: _bucket() for account_id in configured}
    unknown = _bucket()
    seen_receipts: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get('subscriptionProvider') != provider:
            continue
        receipt = _receipt_identity(record)
        if receipt is not None:
            if receipt in seen_receipts:
                continue
            seen_receipts.add(receipt)
        account_id = record.get('accountId')
        bucket = (accounts[account_id]
                  if isinstance(account_id, str) and account_id in accounts
                  else unknown)
        _add_record(bucket, record)
    return {
        'source': LOCAL_RECORD_SOURCE,
        'provider': provider,
        'accounts': accounts,
        'unknown': unknown,
        'window': _window([*accounts.values(), unknown]),
    }


def load_account_usage(owner_user_id: int, *, provider: str, account_ids,
                       repository=None, now_ms: int | None = None) -> dict:
    """Read the owner's local per-request records and summarize attribution.

    Only ``conversation.turn_usage`` projections are read; no transcript
    content or provider call is involved. ``truncated`` reports that the
    repository's own conversation bound was reached, so the figures describe a
    partial window rather than a complete history.
    """
    from lib.conversations.repository import ConversationRepository

    repo = repository if repository is not None else ConversationRepository()
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    total, projections = repo.scan_turn_usage(
        user_id=owner_user_id,
        updated_at_gte=0,
        created_at_lt=now + 1,
        timestamp_gte=0,
        timestamp_lt=now + 1,
        limit=MAX_CONVERSATIONS,
    )
    records = []
    for projection in projections:
        for turn in projection.turns:
            rounds = turn.get('apiRounds')
            if isinstance(rounds, list):
                records.extend(row for row in rounds if isinstance(row, dict))
    summary = summarize_request_records(records, account_ids=account_ids,
                                       provider=provider)
    summary['conversationsScanned'] = total
    summary['truncated'] = total >= MAX_CONVERSATIONS
    return summary


__all__ = [
    'LOCAL_RECORD_SOURCE',
    'MAX_CONVERSATIONS',
    'UNKNOWN',
    'load_account_usage',
    'summarize_request_records',
]