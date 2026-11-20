"""Watchdog quota signals: conversation rounds rejected by a provider's per-AppId minute limit.

Responsibility: merge the production quota watch's episodes of one (AppId,
wire model) lane (lib/llm_dispatch/quota_watch.py) into one owner-scoped quota
alert, and record each closed-to-open transition of that alert as one Watchdog
fault signal of source ``quota`` (docs/ROUTER.md R7). Every process that runs
conversation rounds (the execution host and each execution shard) keeps its
own watch, so one throttled lane opens and closes an episode in each of them;
the alert is the lane's state across all of them. It is open while at least
one reporter (a process's watch, by its process identity) holds an open
episode. An open while it is open only joins the holders: same alert, lane and
state means no new signal and no wake. It closes, and resolves its signal,
when the last holder closes or every remaining holder is proved dead. The
merge is by state identity alone: no interval, count or cap.

A holder's episode closes on its own evidence horizon (recovered or lapsed,
lib/llm_dispatch/quota_watch.py). ``settle_quota_alerts``, run before the
Watchdog reads its signals, closes what no live evidence holds open, and
records why: an alert whose every holder is proved dead; an alert superseded
by policy because the owner's routing no longer offers the lane to
conversation rounds (credential disabled, removed from the connection,
ungranted or the wire model excluded, i.e. reserved) or the dispatcher no
longer admits the key (a billing stop, auto-disable or the owner's toggle);
and a quota signal that no open alert names (its process exited before
closing it, or it predates the per-lane alert record). It never sends a
request to a lane, and an unreadable routing authority proves nothing.

The signal names the AppId only by the last four characters the provider
itself shows, plus the wire model; its detail is the first reporter's
evidence. The alert record keeps the dispatcher's credential identity (never
the key) so a Watchdog dispatch can step off a throttled AppId
(``open_quota_appids``). Recording and waking go through the shared
best-effort boundary in ``signals``: replays never duplicate a signal or a
wake, owners without an available Watchdog record nothing, and nothing raises
into the watch.

Entry points: report_quota_episode, resolve_quota_episode, open_quota_appids,
settle_quota_alerts, QUOTA_ERROR_KIND.
Dependencies: router.contract, router.watchdog.signals, storage.worker_process;
model_routing and key_stats, imported lazily by the policy check.
"""
from __future__ import annotations

from collections.abc import Mapping
import uuid

from lib.log import get_logger
from lib.storage.errors import StorageError

from ..contract import CONTRACT, NAMESPACES, validate
from .signals import FaultSignals, _bounded, _conflict, _report

logger = get_logger(__name__)

QUOTA_ERROR_KIND = 'production_quota_contention'
QUOTA_LANES = NAMESPACES['watchdogQuotaLanes']
_SIGNAL = CONTRACT['$defs']['watchdogSignal']['properties']
_RESOLUTION_LIMIT = _SIGNAL['resolution']['anyOf'][0]['maxLength']
_HINT = ('Tofu sees only its own requests on this AppId. Another workload using the same AppId\u2019s per-minute '
         'limit outside Tofu\u2019s dispatcher can cause this, and so can Tofu\u2019s own load: compare the requests '
         'answered in the window with the most this AppId and model accepted without a rejection. The Global '
         'Router decides whether to pause such a workload: tell it once with watchdog_notify_router, citing this '
         'signal. This signal stays open, and further rejections on this AppId and model wake you no more, '
         'until the quota watch of every process has passed a full provider minute (or the longer hold the '
         'provider named) with no per-minute limit rejection on them; it then closes this signal itself, as '
         'recovered when conversation rounds were accepted meanwhile or lapsed when none used the AppId. It '
         'also closes, as superseded, when the AppId stops being routable for conversation rounds.')
_DEAD_HOLDERS_RESOLUTION = ('Closed: every process that reported this episode has exited, so no quota watch '
                            'still observes it.')
_ORPHAN_RESOLUTION = ('Superseded: no open quota alert holds this signal. The process that reported it exited '
                      'before its episode closed, or it predates the per-lane alert record, so no quota watch '
                      'still observes it; a new rejection of held rounds opens a new signal.')


def _dead(holder: str) -> bool:
    """A holder is dropped only on kernel proof that its process exited."""
    from lib.storage.worker_process import parse_worker_process, process_claim_protected
    return parse_worker_process(holder) is not None and not process_claim_protected(holder)


class _QuotaLanes:
    """One owner's quota alert records, written by compare-and-set only."""

    def __init__(self, owner: int, client=None):
        self.owner = owner
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client

    def _key(self, lane: str) -> str:
        return f'{self.owner}:{lane}'

    def read(self, lane: str) -> tuple[dict | None, int]:
        row = self.client().query('record.get', {'namespace': QUOTA_LANES, 'key': self._key(lane)})
        if row is None:
            return None, 0
        value = row.get('value') if isinstance(row, Mapping) else None
        if not isinstance(value, Mapping) or value.get('owner') != self.owner or value.get('lane') != lane:
            raise StorageError('database_integrity', 'Invalid watchdog quota lane record')
        return dict(value), int(row['version'])

    def update(self, lane: str, change) -> tuple[dict | None, dict]:
        """Apply ``change(current) -> (proposed, outcome)`` atomically; ``None`` writes nothing."""
        while True:
            current, version = self.read(lane)
            proposed, outcome = change(current)
            if proposed is None:
                return current, outcome
            try:
                self.client(write=True).command('record.put', {
                    'namespace': QUOTA_LANES, 'key': self._key(lane),
                    'value': validate(proposed, 'watchdogQuotaLane'), 'expected_version': version,
                }, 'router-watchdog:' + uuid.uuid4().hex)
                return proposed, outcome
            except StorageError as failure:
                if not _conflict(failure):
                    raise

    def open_page(self) -> list[dict]:
        from lib.project_dispatch.history import PAGE_ROWS
        prefix, after, alerts = f'{self.owner}:', '', []
        while True:
            rows = self.client().query('record.list', {
                'namespace': QUOTA_LANES, 'prefix': prefix, 'after_key': after, 'limit': PAGE_ROWS})
            if not isinstance(rows, list):
                raise StorageError('database_integrity', 'Invalid watchdog quota lane page')
            alerts += [dict(row['value']) for row in rows
                       if isinstance(row, Mapping) and isinstance(row.get('value'), Mapping)
                       and row['value'].get('owner') == self.owner and row['value'].get('state') == 'open']
            if len(rows) < PAGE_ROWS:
                return alerts
            after = rows[-1]['key']


def report_quota_episode(owner: int, *, provider_id: str, lane: str, credential: str, key_tail: str,
                         model: str, opened_at_ms: int, detail: str, reporter: str, client=None,
                         dispatch=None) -> dict | None:
    """One reporter's open episode; only the alert's closed-to-open transition is a new signal and wake."""
    if not lane or not credential or not key_tail or not model or not reporter:
        return None
    stale = []

    def opened(current):
        holders = [holder for holder in (current or {}).get('holders', []) if not _dead(holder)]
        if current is not None and current['state'] == 'open':
            if holders:
                if reporter in holders:
                    return None, current['identity']
                return {**current, 'holders': holders + [reporter]}, current['identity']
            stale.append(current['identity'])
        identity = [provider_id, lane, int(opened_at_ms)]
        return {'version': 1, 'owner': owner, 'lane': lane, 'providerId': provider_id[:256],
                'credential': credential[:256], 'keyTail': key_tail[-4:], 'model': model[:160],
                'state': 'open', 'holders': [reporter], 'identity': identity}, identity

    def build(owner, watchdog):
        _alert, identity = _QuotaLanes(owner, client).update(lane, opened)
        if stale and stale[0]:
            _resolve(owner, stale[0], _DEAD_HOLDERS_RESOLUTION, client)
        subject = {'role': 'quota', 'keyTail': key_tail[-4:], 'model': model[:160]}
        error = {'kind': QUOTA_ERROR_KIND, 'detail': _bounded('detail', detail), 'hint': _bounded('hint', _HINT)}
        return identity, subject, error
    # The merged alert's identity names the signal, so a later reporter (or a
    # replay) finds the existing signal: _record_and_wake wakes only on create.
    return _report(owner, 'quota', build, client=client, dispatch=dispatch)


def _resolve(owner: int, identity, resolution: str, client) -> None:
    signals = FaultSignals(owner, client=client)
    signal_id = signals.signal_id('quota', identity)
    if signals.get(signal_id) is None:
        return
    # The producer, not the Watchdog, observed recovery; say so where the contract records it.
    by = {'resolved_by': 'producer'} if 'resolvedBy' in _SIGNAL else {}
    signals.resolve([signal_id], str(resolution)[:_RESOLUTION_LIMIT], **by)


def resolve_quota_episode(owner: int, *, lane: str, reporter: str, resolution: str, client=None) -> bool:
    """One reporter's episode closed; the alert closes with its last live holder. Never raises."""
    try:
        def closed(current):
            if current is None or current['state'] != 'open':
                return None, None
            holders = [holder for holder in current['holders'] if holder != reporter and not _dead(holder)]
            if holders:
                if holders == current['holders']:
                    return None, None
                return {**current, 'holders': holders}, None
            return {**current, 'state': 'closed', 'holders': []}, current['identity']

        _alert, identity = _QuotaLanes(owner, client).update(lane, closed)
        if identity is None:
            return False
        _resolve(owner, identity, resolution, client)
        return True
    except Exception:
        logger.warning('[RouterWatchdog] quota lane %s close failed owner=%s', lane, owner, exc_info=True)
        return False


def lane_unroutable(owner: int, alert: Mapping, *, repository=None, key_enabled=None) -> str | None:
    """Why the owner's policy no longer offers the alert's lane to conversation rounds, or None.

    Evidence only: an unreadable authority, or a credential it does not know (a
    configured-pool key), proves nothing and returns None.
    """
    provider, credential, model = alert['providerId'], alert['credential'], alert['model']
    try:
        from lib.llm_dispatch.slot import key_stats_namespace
        if key_enabled is None:
            from lib.key_stats import is_key_enabled as key_enabled
        if not key_enabled(key_stats_namespace(owner, provider), credential, model=model):
            return ('the dispatcher no longer admits this key for this model (a billing stop, the daily '
                    'auto-disable, or the owner switched it off)')
    except Exception:
        logger.debug('[RouterWatchdog] key admission unreadable for quota lane', exc_info=True)
    try:
        from lib.model_routing.candidate_compiler import RouteCandidateCompiler
        from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
        from lib.model_routing.repository import ModelRoutingRepository, OwnerBoundary
        authority = (repository or ModelRoutingRepository()).get(OwnerBoundary.create(owner))
        document = authority.document
        if not any(row.get('credential_id') == credential for row in document.get('credentials', ())):
            return None
        accesses = {row['provider_access_id'] for row in document.get('provider_accesses', ())
                    if row.get('provider_id') == provider}
        offerings = {row['offering_id'] for row in document.get('offerings', ())
                     if row.get('provider_access_id') in accesses}
        serving = {row['offering_id'] for row in document.get('deployments', ())
                   if row.get('offering_id') in offerings and str(row.get('wire_model_id', '')).strip() == model}
        compiler = RouteCandidateCompiler(document, normalized=getattr(authority, 'normalized', False))
        for offering_id in sorted(serving):
            try:
                candidates = compiler.compile(
                    NativeModelSelection(None, ProviderOfferingRef(provider, offering_id)))
            except Exception:
                continue
            if any(candidate.credential['credential_id'] == credential
                   and str(candidate.deployment['wire_model_id']).strip() == model for candidate in candidates):
                return None
        return ('the owner\'s model routing no longer offers this AppId for this model to conversation rounds '
                '(the credential is disabled, reserved off the connection, ungranted, or the model is excluded)')
    except Exception:
        logger.debug('[RouterWatchdog] routing authority unreadable for quota lane', exc_info=True)
        return None


def _superseded_resolution(alert: Mapping, reason: str) -> str:
    return (f'Superseded by policy: {reason}, so no conversation round can be rejected on AppId '
            f'…{alert["keyTail"]} ({alert["providerId"]}, model {alert["model"]}) any more. Tofu sent no probe to '
            'close this alert; routing it again and seeing held rounds rejected opens a new one.')


def settle_quota_alerts(owner: int, *, client=None, unroutable=lane_unroutable) -> list[str]:
    """Close the owner's quota alerts and signals that no live evidence holds open. Never raises.

    Returns the resolved signal ids.
    """
    try:
        lanes = _QuotaLanes(owner, client)
        signals = FaultSignals(owner, client=client)
        resolved = []
        for alert in lanes.open_page():
            if all(_dead(holder) for holder in alert['holders']):
                resolution = _DEAD_HOLDERS_RESOLUTION
            else:
                reason = unroutable(owner, alert)
                if reason is None:
                    continue
                resolution = _superseded_resolution(alert, reason)

            def closed(current, identity=alert['identity']):
                if current is None or current['state'] != 'open' or current['identity'] != identity:
                    return None, None
                return {**current, 'state': 'closed', 'holders': []}, identity
            _alert, identity = lanes.update(alert['lane'], closed)
            if identity is not None:
                _resolve(owner, identity, resolution, client)
                resolved.append(signals.signal_id('quota', identity))

        def held_ids():
            return {signals.signal_id('quota', alert['identity']) for alert in lanes.open_page()}
        orphans, cursor = [], None
        while True:
            page = signals.open_page(cursor)
            orphans += [signal['signalId'] for signal in page['signals'] if signal['source'] == 'quota']
            cursor = page['next_cursor']
            if cursor is None:
                break
        # Read after the signals: a producer writes the alert before its signal,
        # so a signal opened meanwhile is named here.
        held = held_ids()
        orphans = [signal_id for signal_id in orphans if signal_id not in held]
        if orphans:
            by = {'resolved_by': 'producer'} if 'resolvedBy' in _SIGNAL else {}
            signals.resolve(orphans, _ORPHAN_RESOLUTION[:_RESOLUTION_LIMIT], **by)
        return resolved + orphans
    except Exception:
        logger.warning('[RouterWatchdog] quota alert settlement failed owner=%s', owner, exc_info=True)
        return []


def open_quota_appids(owner: int, *, client=None) -> frozenset:
    """``(provider, credential)`` of every AppId with an open quota alert of ``owner``."""
    return frozenset((alert['providerId'], alert['credential'])
                     for alert in _QuotaLanes(owner, client).open_page())


__all__ = ['QUOTA_ERROR_KIND', 'lane_unroutable', 'open_quota_appids', 'report_quota_episode',
           'resolve_quota_episode', 'settle_quota_alerts']
