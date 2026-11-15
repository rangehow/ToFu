"""Watchdog quota signals: conversation rounds rejected by a provider's per-AppId minute limit.

Responsibility: record one production quota-watch episode of one (AppId,
model) lane (lib/llm_dispatch/quota_watch.py) as one Watchdog fault signal of
source ``quota``, and close it when the watch observes recovery (docs/ROUTER.md
R7). The signal names the AppId only by the last four characters the provider
itself shows, plus the wire model; its detail is the watch's evidence.
Recording and waking go through the shared best-effort boundary in
``signals``: replays never duplicate a signal or a wake, owners without an
available Watchdog record nothing, and nothing raises into the watch.

Entry points: report_quota_episode, resolve_quota_episode, QUOTA_ERROR_KIND.
Dependencies: router.contract, router.watchdog.signals.
"""
from __future__ import annotations

from lib.log import get_logger

from ..contract import CONTRACT
from .signals import FaultSignals, _bounded, _report

logger = get_logger(__name__)

QUOTA_ERROR_KIND = 'production_quota_contention'
_SIGNAL = CONTRACT['$defs']['watchdogSignal']['properties']
_RESOLUTION_LIMIT = _SIGNAL['resolution']['anyOf'][0]['maxLength']
_HINT = ('Tofu sees only its own requests on this AppId. Another workload using the same AppId\u2019s per-minute '
         'limit outside Tofu\u2019s dispatcher can cause this, and so can Tofu\u2019s own load: compare the requests '
         'answered in the window with the most this AppId and model accepted without a rejection. The Global '
         'Router decides whether to pause such a workload: tell it once with watchdog_notify_router, citing this '
         'signal. The quota watch closes this signal itself after a full provider minute in which conversation '
         'rounds on this AppId and model are accepted with no per-minute limit rejection.')


def report_quota_episode(owner: int, *, provider_id: str, lane: str, key_tail: str, model: str,
                         opened_at_ms: int, detail: str, client=None, dispatch=None) -> dict | None:
    """One signal per episode; the lane is the fault, so a later episode is a new signal."""
    if not lane or not key_tail or not model:
        return None

    def build(owner, watchdog):
        subject = {'role': 'quota', 'keyTail': key_tail[-4:], 'model': model[:160]}
        error = {'kind': QUOTA_ERROR_KIND, 'detail': _bounded('detail', detail), 'hint': _bounded('hint', _HINT)}
        # (identity, subject, error, condition, episode parts): only the watch observes recovery.
        return [provider_id, lane, opened_at_ms], subject, error, {'kind': 'producer'}, [provider_id, lane]
    return _report(owner, 'quota', build, client=client, dispatch=dispatch)


def resolve_quota_episode(owner: int, signal_id: str, resolution: str, *, client=None) -> bool:
    """The watch closes its own signal on recovery; never raises into it."""
    try:
        # The producer, not the Watchdog, observed recovery; say so where the contract records it.
        by = {'resolved_by': 'producer'} if 'resolvedBy' in _SIGNAL else {}
        FaultSignals(owner, client=client).resolve([signal_id], str(resolution)[:_RESOLUTION_LIMIT], **by)
        return True
    except Exception:
        logger.warning('[RouterWatchdog] quota signal %s resolve failed owner=%s', signal_id, owner,
                       exc_info=True)
        return False


__all__ = ['QUOTA_ERROR_KIND', 'report_quota_episode', 'resolve_quota_episode']
