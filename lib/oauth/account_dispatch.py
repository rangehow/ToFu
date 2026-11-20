"""Private Codex outbound resolution for one immutable account generation."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import secrets
from urllib.parse import urlsplit

from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import ModelRoutingRepository
from lib.oauth.accounts import SubscriptionAccounts
from lib.agent_core.storage_errors import StorageError


_scope = ContextVar('issued_codex_account', default=None)


@contextmanager
def scoped_account_dispatch(slot):
    account = getattr(slot, 'subscription_account', None)
    token = _scope.set(account)
    try:
        yield
    finally:
        _scope.reset(token)


def call_in_account_scope(slot, transport, *args, **kwargs):
    with scoped_account_dispatch(slot):
        return transport(*args, **kwargs)


def current_account_dispatch():
    return _scope.get()


def require_codex_destination(endpoint):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'https' or parsed.netloc not in {'chatgpt.com', 'chatgpt.com:443'}
            or parsed.path != '/backend-api/codex/responses' or parsed.query or parsed.fragment):
        raise _unavailable()


def _unavailable():
    return SubscriptionCredentialUnavailableError(reason='credential_source_binding',
                                                  credential_source='tofu_oauth', transient=False)


def reject_bound_account(scoped):
    binding, repository = scoped
    accounts = SubscriptionAccounts(repository)
    for _ in range(3):
        try:
            accounts.reject_generation(binding)
            return
        except ModelRoutingError as error:
            if error.kind == 'subscription_account_unavailable':
                return
            if error.kind != 'model_routing_revision_conflict':
                raise
        except StorageError as error:
            if error.code != 'database_conflict':
                raise
    # Do not report a successful rejection write on sustained contention.
    raise ModelRoutingError('Subscription rejection could not be recorded', kind='model_routing_revision_conflict')


def revalidate_codex_account(binding, repository):
    try:
        return SubscriptionAccounts(repository).revalidate(binding)
    except ModelRoutingError:
        raise _unavailable() from None


def resolve_codex_account_snapshot(binding, *, repository=None):
    from lib.oauth.codex import _access_snapshot

    try:
        account = SubscriptionAccounts(repository if repository is not None else ModelRoutingRepository())
        token = account.resolve(binding)
        snapshot = _access_snapshot(token)
        if snapshot is None or not snapshot.bearer_account_id or snapshot.account_id != snapshot.bearer_account_id:
            raise ValueError('Invalid Codex account credential')
        subject = hashlib.sha256(snapshot.bearer_account_id.encode()).hexdigest()
        if not secrets.compare_digest(subject, account.revalidate(binding)):
            raise ValueError('Invalid Codex account credential')
        return snapshot
    except (ModelRoutingError, ValueError, TypeError):
        raise _unavailable() from None
