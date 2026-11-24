"""Internal owner-scoped subscription account lifecycle; no provider I/O."""
from dataclasses import dataclass, field
import hashlib
import json
import secrets
import time

from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.subscription_accounts import ACCOUNT_FIELD, PROVIDERS, normalize_accounts


@dataclass(frozen=True, slots=True)
class AccountIntent:
    boundary: object
    provider: str
    account_id: str
    expected_revision: int
    reauth: bool


@dataclass(frozen=True, slots=True)
class AccountBinding:
    boundary: object
    provider: str
    account_id: str
    secret_reference: str = field(repr=False)


def _missing():
    return ModelRoutingError('Subscription account unavailable', kind='subscription_account_unavailable')


class SubscriptionAccounts:
    def __init__(self, repository):
        self.repository = repository

    def _read(self, boundary, provider, expected_revision=None):
        if provider not in PROVIDERS:
            raise _missing()
        current = self.repository.get(boundary)
        if expected_revision is not None and (type(expected_revision) is not int or current.revision != expected_revision):
            raise ModelRoutingError('Subscription accounts changed', kind='model_routing_revision_conflict')
        return current, [dict(row) for row in current.document.get(ACCOUNT_FIELD, ())]

    @staticmethod
    def _find(rows, provider, account_id):
        row = next((row for row in rows if row['provider'] == provider and row['account_id'] == account_id), None)
        if row is None:
            raise _missing()
        return row

    def _commit(self, current, rows):
        return self.repository.compare_and_swap(
            current.boundary, {**current.document, ACCOUNT_FIELD: normalize_accounts(rows)},
            expected_revision=current.revision, account_mutation=True)

    def inventory(self, boundary, provider):
        current, rows = self._read(boundary, provider)
        selected, summaries = None, []
        now = time.time()
        for row in rows:
            if row['provider'] != provider:
                continue
            if row['is_default']:
                selected = row['account_id']
            summaries.append({'account_id': row['account_id'], 'alias': row['alias'],
                              'status': 'authorized' if row['expires_at'] > now else 'needs_reauth'})
        return {'provider': provider, 'revision': current.revision,
                'default_account_id': selected, 'accounts': summaries}

    def begin(self, boundary, provider, expected_revision, *, account_id=None):
        _, rows = self._read(boundary, provider, expected_revision)
        if account_id is not None:
            self._find(rows, provider, account_id)
        return AccountIntent(boundary, provider, account_id if account_id is not None else 'oa_' + secrets.token_hex(16),
                             expected_revision, account_id is not None)

    def complete(self, intent, *, verified_subject, token, expires_at):
        """Only a provider-owned exchange may supply the verified subject and token."""
        if not isinstance(verified_subject, str) or not verified_subject or not isinstance(token, dict):
            raise _missing()
        current, rows = self._read(intent.boundary, intent.provider, intent.expected_revision)
        subject = hashlib.sha256(verified_subject.encode()).hexdigest()
        if intent.reauth:
            row = self._find(rows, intent.provider, intent.account_id)
            if not secrets.compare_digest(row['subject_fingerprint'], subject):
                raise _missing()
        else:
            if any(row['provider'] == intent.provider and row['subject_fingerprint'] == subject for row in rows):
                raise ModelRoutingError('Subscription account already linked', kind='subscription_account_exists')
            row = {'provider': intent.provider, 'account_id': intent.account_id,
                   'subject_fingerprint': subject, 'alias': '', 'is_default': False,
                   'secret_reference': 'oauth_' + '0' * 32}
            rows.append(row)
        row['expires_at'] = expires_at
        normalize_accounts(rows)
        secret = self.repository.put_secret(intent.boundary, json.dumps(token, allow_nan=False), subscription_account=True)
        row['secret_reference'] = secret['secret_reference']
        # On ambiguous success the existing orphan collector, not a caller retry,
        # determines whether the freshly staged reference remains reachable.
        self._commit(current, rows)
        return intent.account_id

    def update(self, boundary, provider, account_id, expected_revision, *, action, alias=None):
        current, rows = self._read(boundary, provider, expected_revision)
        row = self._find(rows, provider, account_id)
        if action == 'rename':
            row['alias'] = alias
        elif action == 'default':
            for item in rows:
                if item['provider'] == provider:
                    item['is_default'] = item is row
        elif action == 'remove':
            rows.remove(row)
        else:
            raise ValueError('Unknown subscription account command')
        committed = self._commit(current, rows)
        return committed.revision

    @staticmethod
    def issued_default(authority, provider):
        """Select once from the same snapshot as route admission."""
        if provider != 'codex' or ACCOUNT_FIELD not in authority.document:
            return None
        row = next((row for row in authority.document[ACCOUNT_FIELD]
                    if row['provider'] == provider and row['is_default']), None)
        if row is None:
            raise _missing()
        return AccountBinding(authority.boundary, provider, row['account_id'], row['secret_reference'])

    @staticmethod
    def issued_for_account(authority, provider, account_id):
        """Bind the exact account the compiler selected, from the same snapshot.

        The grant authorization was already proven against this snapshot's
        account fingerprints; outbound re-proves the bearer at send time.
        """
        if provider != 'codex' or ACCOUNT_FIELD not in authority.document:
            raise _missing()
        row = next((row for row in authority.document[ACCOUNT_FIELD]
                    if row['provider'] == provider and row['account_id'] == account_id), None)
        if row is None:
            raise _missing()
        return AccountBinding(authority.boundary, provider, row['account_id'], row['secret_reference'])

    def bind(self, boundary, provider, account_id):
        _, rows = self._read(boundary, provider)
        if account_id is None:
            account_id = next((row['account_id'] for row in rows
                               if row['provider'] == provider and row['is_default']), None)
        row = self._find(rows, provider, account_id)
        return AccountBinding(boundary, provider, account_id, row['secret_reference'])

    def reject_generation(self, binding):
        """Record a provider rejection on this generation only, never on its replacement."""
        current, rows = self._read(binding.boundary, binding.provider)
        row = self._find(rows, binding.provider, binding.account_id)
        if row['secret_reference'] != binding.secret_reference or row['expires_at'] == 0:
            return
        row['expires_at'] = 0
        self._commit(current, rows)

    def revalidate(self, binding):
        _, rows = self._read(binding.boundary, binding.provider)
        row = self._find(rows, binding.provider, binding.account_id)
        if row['secret_reference'] != binding.secret_reference or row['expires_at'] <= time.time():
            raise _missing()
        return row['subject_fingerprint']

    def resolve(self, binding):
        """Recheck the exact grant generation, never consult the current default."""
        self.revalidate(binding)
        stored = self.repository.resolve_secret(binding.boundary, binding.secret_reference, subscription_account=True)
        self.revalidate(binding)
        return json.loads(stored)
