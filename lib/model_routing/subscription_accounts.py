"""Private subscription-account fields on the revisioned routing aggregate."""
import math
import re

from .domain import ModelRoutingError

PROVIDERS = frozenset({'codex', 'claude', 'kimi'})
ACCOUNT_FIELD = 'subscription_accounts'
SECRET_PREFIX = 'oauth_'
_FIELDS = frozenset({'provider', 'account_id', 'subject_fingerprint', 'secret_reference',
                     'alias', 'is_default', 'expires_at'})


def normalize_accounts(rows):
    def invalid():
        return ModelRoutingError('Invalid subscription account metadata', field=ACCOUNT_FIELD)

    if not isinstance(rows, list):
        raise invalid()
    accounts, subjects, references, defaults = set(), set(), set(), set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() != _FIELDS:
            raise invalid()
        provider = row['provider']
        account, subject, reference = (row[key] for key in ('account_id', 'subject_fingerprint', 'secret_reference'))
        alias, expiry = row['alias'], row['expires_at']
        if (not isinstance(provider, str) or provider not in PROVIDERS
                or any(not isinstance(value, str) or not re.fullmatch(pattern, value)
                       for value, pattern in ((account, r'oa_[0-9a-f]{32}'), (subject, r'[0-9a-f]{64}'),
                                              (reference, r'oauth_[0-9a-f]{32}')))
                or not isinstance(alias, str) or len(alias) > 128 or not alias.isprintable() and alias != ''
                or type(row['is_default']) is not bool or type(expiry) not in (int, float)
                or not 0 <= expiry <= 32_503_680_000 or not math.isfinite(expiry)):
            raise invalid()
        if account in accounts or (provider, subject) in subjects or reference in references:
            raise invalid()
        accounts.add(account)
        subjects.add((provider, subject))
        references.add(reference)
        if row['is_default']:
            if provider in defaults:
                raise invalid()
            defaults.add(provider)
    return sorted((dict(row) for row in rows), key=lambda row: (row['provider'], row['account_id']))


def retain_private_accounts(candidate, current, *, account_mutation):
    """Preserve private account metadata on generic aggregate writes."""
    if account_mutation:
        return candidate
    if ACCOUNT_FIELD in candidate and candidate[ACCOUNT_FIELD] != current.get(ACCOUNT_FIELD, []):
        raise ModelRoutingError('Subscription accounts require their lifecycle service',
                                kind='subscription_account_lifecycle_required')
    if ACCOUNT_FIELD in current:
        return {**candidate, ACCOUNT_FIELD: current[ACCOUNT_FIELD]}
    return candidate
