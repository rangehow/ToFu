"""Watchdog storage signals: one storage fence episode, reported once readiness returns.

Responsibility: record one closed storage fence episode of this deployment
(``StorageRuntime.add_fence_recovery_callback``) as one Watchdog fault signal of
source ``storage`` for every owner with an available Watchdog (docs/ROUTER.md
R7). While storage is fenced no signal or wake can be written, so the episode
is reported when readiness returns; the manager log, ``serverctl status`` and
the browser show the fence while it holds. Recording and waking go through the
shared best-effort boundary in ``signals``: a replay of the same episode is the
same signal, owners without an available Watchdog record nothing, and nothing
raises into the runtime.

Entry points: report_storage_fence_to_owners, report_storage_fence,
register_storage_fence_reports, STORAGE_FENCE_ERROR_KIND.
Dependencies: process_roles, router.home, router.watchdog.signals.
"""
from __future__ import annotations

from datetime import datetime, timezone

from lib.log import get_logger

from .signals import _bounded, _report

logger = get_logger(__name__)

STORAGE_FENCE_ERROR_KIND = 'storage_fenced'
_HINT = ('While storage was fenced every persistence wait of this deployment stopped: running Turns, Workers '
         'and background-command deliveries held until readiness returned, and no owner-facing alert could be '
         'written. The storage host\'s verdict (liveness probes, recycle, restart breaker) is in '
         'logs/server-console.log; the manager logged the outage in logs/server-manager.log as "is live but not '
         'ready". Check that the interrupted work resumed, then close this signal with watchdog_resolve_signals.')


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _detail(episode: dict) -> str:
    since, until = float(episode['since']), float(episode['until'])
    parts = [f'Storage was fenced for {max(0.0, until - since):.0f}s, from {_utc(since)} until {_utc(until)}',
             f"cause: {episode.get('cause') or 'unknown'}"]
    if episode.get('lastRestartError'):
        parts.append(f"last restart error: {episode['lastRestartError']}")
    parts.append(f"{int(episode.get('restartAttempts') or 0)} restart attempt(s)")
    return '; '.join(parts)


def report_storage_fence(owner: int, episode: dict, *, client=None, dispatch=None) -> dict | None:
    """One signal per episode; the episode's start is its identity.

    The ``fence`` tag and the ``fence`` fault episode keep a fence apart from
    the supervisor's ``authority`` storage episodes (an unresponsive authority,
    fenced or kept) that start at the same millisecond. The tuple is the
    condition-aware ``_report`` shape; a base that reads three parts ignores
    the condition and the episode.
    """
    def build(owner, watchdog):
        error = {'kind': STORAGE_FENCE_ERROR_KIND, 'detail': _bounded('detail', _detail(episode)),
                 'hint': _bounded('hint', _HINT)}
        identity = ['fence', round(float(episode['since']) * 1000)]
        return identity, {'role': 'storage'}, error, {'kind': 'event'}, ['fence']
    return _report(owner, 'storage', build, client=client, dispatch=dispatch)


def report_storage_fence_to_owners(episode: dict, *, owners=None, client=None,
                                   dispatch=None) -> list[dict]:
    """Report one closed fence to every owner with a router home: the fence was deployment-wide."""
    try:
        if owners is None:
            from lib.router.home import router_home_owners
            owners = router_home_owners()
        signals = []
        for owner in owners:
            signal = report_storage_fence(owner, episode, client=client, dispatch=dispatch)
            if signal is not None:
                signals.append(signal)
        return signals
    except Exception:
        logger.warning('[RouterWatchdog] storage fence report failed', exc_info=True)
        return []


def register_storage_fence_reports(runtime, *, process_role: str) -> bool:
    """Report this process's fence episodes when it serves requests.

    Every process attached to the storage host observes the same fence. The
    request-services process (``api``, or ``all`` in one process) is the one
    reporter, so a split deployment records each fence once.
    """
    from lib.process_roles import CAPABILITY_REQUEST_SERVICES, process_role_has
    if not process_role_has(process_role, CAPABILITY_REQUEST_SERVICES):
        return False
    runtime.add_fence_recovery_callback(report_storage_fence_to_owners)
    return True


__all__ = ['STORAGE_FENCE_ERROR_KIND', 'register_storage_fence_reports', 'report_storage_fence',
           'report_storage_fence_to_owners']
