"""GitHub session supervisor: start, stop and restart pollers from configuration (C8).

Responsibility: runs where the scheduled-jobs capability runs. On the
scheduler's existing maintenance tick (and when this process changes the
account) it starts a ``GitHubAdapter`` for an enabled account, stops it for a
disabled one, restarts it after an exit or when its identity, credential or
API base changed, and passes the tick on (the adapter polls only when
GitHub's ``X-Poll-Interval`` says it may). No timer, backoff schedule or
retry count of its own: a restart happens at the next tick, and a refused
token (``needs_login``) stops restarts until the owner updates the account.
Same policy as the 大象 supervisor (C6), including its shutdown rule: stopping
fences ticks, configuration changes and new sessions until the next hosting
lifecycle starts, and a session leaves the registry only once it joined.
Shutdown asks the session to stop when its drain begins and joins it at its
background-service stop.

Entry points: ``reconcile_github_sessions(principal)`` (scheduler tick),
``reconcile_owner(owner)``, ``notify_account_changed(owner)`` (account
commands), ``running_adapter(owner)``, ``start_github_sessions()``,
``begin_stopping_github_sessions()`` and ``stop_github_sessions(timeout)``
(process lifecycle). Dependencies: ``ChannelStore``,
``GitHubAdapter``, ``delivery.global_router_binding``, the credential vault.
"""
from __future__ import annotations

import threading

from lib.channels.delivery import RouterUnavailable, global_router_binding
from lib.channels.github.adapter import CHANNEL, GitHubAdapter, account_binding
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

_ADAPTERS: dict[int, GitHubAdapter] = {}
_LOCK = threading.RLock()
# Set once the scheduler tick has called in: only then does this process host sessions.
_HOSTING = threading.Event()
# Set once shutdown begins stopping sessions; cleared only by the next lifecycle's start.
_STOPPING = threading.Event()
_TERMINAL_STATES = frozenset({'needs_login'})


def start_github_sessions() -> None:
    """Open a new hosting lifecycle only after the previous one joined."""
    with _LOCK:
        if _STOPPING.is_set():
            if _ADAPTERS:
                raise RuntimeError('GitHub channel sessions have not quiesced')
            _STOPPING.clear()


def reconcile_github_sessions(principal) -> None:
    """Scheduler-tick entry. TODO(enterprise): like the 大象 supervisor, a
    distributed scheduler needs an enabled-account index and a per-account
    lease before it can host sessions for every owner."""
    with _LOCK:
        if _STOPPING.is_set():
            return
        _HOSTING.set()
    owner = getattr(principal, 'owner_user_id', None)
    if owner is None:
        return
    store = ChannelStore(int(owner))
    reconcile_owner(int(owner), store=store)
    if _STOPPING.is_set():
        return
    # Same Router Watchdog report as the 大象 supervisor (docs/ROUTER.md R7).
    from lib.router.watchdog.signals import report_channel_status
    report_channel_status(int(owner), CHANNEL, store.account(CHANNEL))


def bridge_state(owner: int) -> str:
    """``running``/``stopped`` where this process hosts sessions, else ``not_hosted``."""
    if not _HOSTING.is_set():
        return 'not_hosted'
    return 'running' if running_adapter(owner) is not None else 'stopped'


def _credential_present(account: dict) -> bool:
    from lib.credentials_vault import has_entry
    return has_entry(str((account.get('credential') or {}).get('name') or ''))


def reconcile_owner(owner: int, *, store: ChannelStore | None = None,
                    adapter_factory=GitHubAdapter, stop_timeout: float = 2.0) -> str:
    """Converge one owner's GitHub session with its account; returns the action taken."""
    store = store or ChannelStore(owner)
    with _LOCK:
        if _STOPPING.is_set():
            return 'stopping'
        account = store.account(CHANNEL)
        current = _ADAPTERS.get(owner)
        if not account or not account.get('enabled'):
            if current is not None:
                if not current.stop(stop_timeout):
                    return 'stopping'
                _ADAPTERS.pop(owner, None)
                return 'stopped'
            return 'idle'
        if current is not None and current.alive() and current.binding == account_binding(account):
            current.tick()
            return 'running'
        if current is not None:
            # An exited session already recorded its outcome; a newer owner
            # decision (re-enable, new token) has replaced it with 'starting'.
            if not current.stop(stop_timeout):
                return 'stopping'
            _ADAPTERS.pop(owner, None)
        if ((account.get('status') or {}).get('state')) in _TERMINAL_STATES:
            return account['status']['state']
        if not (account.get('identity') or {}).get('uid'):
            store.set_status(CHANNEL, 'error', 'GitHub identity is unresolved; enable the account again')
            return 'error'
        if not _credential_present(account):
            store.set_status(CHANNEL, 'needs_login', 'The GitHub token is missing; add it in Settings')
            return 'needs_login'
        try:
            global_router_binding(owner, create=True)
        except RouterUnavailable as error:
            store.set_status(CHANNEL, 'error', str(error))
            return 'error'
        adapter = adapter_factory(owner, store=store)
        store.set_status(CHANNEL, 'starting', '')
        try:
            adapter.start()
        except Exception as error:
            logger.warning('[Channels] github session start failed owner=%s', owner, exc_info=True)
            store.set_status(CHANNEL, 'error',
                             f'session start failed: {getattr(error, "code", type(error).__name__)}')
            return 'error'
        _ADAPTERS[owner] = adapter
        if _STOPPING.is_set():
            # Shutdown began while this tick started it (begin_stopping_github_sessions takes no lock).
            adapter.request_stop()
            return 'stopping'
        return 'started'


def notify_account_changed(owner: int) -> None:
    """Apply a configuration change now when this process hosts sessions."""
    if _STOPPING.is_set() or not _HOSTING.is_set():
        return
    threading.Thread(target=_reconcile_quietly, args=(int(owner),),
                     name=f'github-reconcile-{owner}', daemon=True).start()


def _reconcile_quietly(owner: int) -> None:
    try:
        reconcile_owner(owner)
    except Exception:
        logger.warning('[Channels] github reconcile failed owner=%s', owner, exc_info=True)


def running_adapter(owner: int) -> GitHubAdapter | None:
    with _LOCK:
        adapter = _ADAPTERS.get(int(owner))
    return adapter if adapter is not None and adapter.alive() else None


def begin_stopping_github_sessions() -> None:
    """Shutdown's drain begins: fence new sessions and ask every one to stop, joining none.

    Never waits for _LOCK, which a tick holds across storage I/O and session
    joins: the registry is copied under the GIL, and a session that tick
    registers afterwards meets the fence as it registers (``reconcile_owner``).
    """
    _STOPPING.set()
    _HOSTING.clear()
    for adapter in tuple(_ADAPTERS.values()):
        adapter.request_stop()


def stop_github_sessions(timeout: float = 2.0) -> bool:
    """Join every session; one still live stays registered, so a retry joins it."""
    with _LOCK:
        _STOPPING.set()
        _HOSTING.clear()
        adapters = list(_ADAPTERS.items())
    for owner, adapter in adapters:
        try:
            joined = adapter.stop(timeout)
        except Exception:
            joined = False
            logger.warning('[Channels] github session stop failed owner=%s', owner, exc_info=True)
        if joined:
            with _LOCK:
                if _ADAPTERS.get(owner) is adapter:
                    _ADAPTERS.pop(owner)
    with _LOCK:
        return not _ADAPTERS


__all__ = ['begin_stopping_github_sessions', 'bridge_state', 'notify_account_changed',
           'reconcile_github_sessions', 'reconcile_owner', 'running_adapter', 'start_github_sessions',
           'stop_github_sessions']
