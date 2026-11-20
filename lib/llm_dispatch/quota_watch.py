"""Production quota watch: a provider's per-AppId minute limit rejecting conversation rounds.

Responsibility: read-only observation of the dispatcher's existing per-attempt
outcomes on each quota lane (one credential, i.e. AppId, and one wire model at
one provider), and one Router Watchdog fault signal per episode in which
conversation rounds are rejected by the gateway's per-AppId, per-model,
per-minute limit and held by dispatcher backpressure. Conversation rounds
("production") are the model rounds of conversation Tasks, dispatched inside
``production_dispatch``; every other dispatch (translation, fetch extraction,
titles, summaries) is auxiliary: counted as the lane's own load, never alerted
on. The watch changes no routing, admission, retry, health or billing decision.

Rules; every count is over the provider's own limit window, ``WINDOW_S``
(docs/modules/llm_io.md, "Production quota watch"):
- A minute-limit rejection is a 429 whose body names the gateway's per-AppId
  limit, requests or tokens per minute (``minute_limit``). Upstream overload,
  vendor or project-wide limits and billing stops are not.
- An episode opens on a lane when, within one window, a conversation round was
  rejected by that limit and a conversation round on the lane was held by
  dispatcher backpressure (queue wait > 0). Each owner whose rounds were
  rejected during the episode is told once.
- It closes when its evidence horizon passes with no further minute-limit
  rejection on the lane. The horizon is the provider's own: one window after
  the lane's last rejection, or later when that 429 named a longer hold
  (``Retry-After``). The close records which evidence ended it:
  ``recovered`` when conversation rounds on the lane were accepted after the
  last rejection, ``lapsed`` when none used the lane (nothing routed there:
  the Watchdog steps off it, or the owner reserved or disabled the key), so
  none was rejected or held. The watch never sends a request to a lane to
  close an episode; a lapsed close says the limit's state since is unobserved.
  The horizon is checked on every observation, on every read of open
  AppIds, and by one timer per episode set to that horizon, so a lane that
  nothing routes to closes too.

Transitions leave the dispatch thread before any storage read or wake, and one
lane's deliveries run in order (``_deliver_to_watchdog``). A failing watch is
logged and never reaches the dispatch. State is in memory and per process,
bounded by one window of events per lane in use. Each process reports as
itself (its process identity, taken after a fork), and the Watchdog producer
merges every process's episodes of one lane into one alert that wakes only on
its own state change (lib/router/watchdog/quota.py).

A Watchdog Turn dispatches inside ``avoid_throttled_appids``: its picks step
off AppIds with an open alert of its owner or with an episode open in this
process (``throttled_appids``, read by the dispatcher), so the Watchdog does
not add its own rounds to the limit it is reporting on.

Entry points: production_dispatch, production_owner, observe_rejection,
observe_settlement, minute_limit, avoid_throttled_appids, throttled_appids,
QuotaWatch, WINDOW_S, CLOSE_RECOVERED, CLOSE_LAPSED.
Dependencies: lib.router.watchdog.quota, imported on the delivery thread and
by throttled_appids; lib.storage.worker_process for the reporter identity.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import statistics
import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

# The gateway limits each AppId per model and per minute, so one provider
# minute is the window of every count and of recovery.
WINDOW_S = 60.0

# Which evidence closed an episode (the close transition's ``cause``).
CLOSE_RECOVERED = 'recovered'
CLOSE_LAPSED = 'lapsed'

# The gateway's wording of those limits (sankuai AIGC validation stage):
#   "App:**4861在模型:hy4每分钟请求次数超过限制"   requests per minute
#   "App:**0733在模型:gpt-6-luna的tpm超过限制"     tokens per minute
_MINUTE_LIMIT = re.compile(r'App:\*+([0-9A-Za-z]+)在模型:\S+?(每分钟请求次数超过限制|的tpm超过限制)')

_PRODUCTION_OWNER: ContextVar[int | None] = ContextVar('quota_watch_production_owner', default=None)
_AVOIDING_OWNER: ContextVar[int | None] = ContextVar('quota_watch_avoiding_owner', default=None)


def minute_limit(message: str) -> str | None:
    """``'rpm'`` or ``'tpm'`` when a 429 body names the gateway's per-AppId minute limit."""
    match = _MINUTE_LIMIT.search(message or '')
    if match is None:
        return None
    return 'rpm' if match.group(2).startswith('每分钟') else 'tpm'


@contextmanager
def production_dispatch(owner_user_id: int | None):
    """Mark the dispatches inside as conversation rounds of ``owner_user_id``.

    Without an owner nothing is marked, and those dispatches count as auxiliary.
    """
    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool):
        yield
        return
    token = _PRODUCTION_OWNER.set(owner_user_id)
    try:
        yield
    finally:
        _PRODUCTION_OWNER.reset(token)


def production_owner() -> int | None:
    """Owner of the conversation round dispatching in this context, or None for auxiliary work."""
    return _PRODUCTION_OWNER.get()


@contextmanager
def avoid_throttled_appids(owner_user_id: int | None):
    """Dispatches inside prefer AppIds without an open quota alert of ``owner_user_id``.

    Without an owner nothing changes.
    """
    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool):
        yield
        return
    token = _AVOIDING_OWNER.set(owner_user_id)
    try:
        yield
    finally:
        _AVOIDING_OWNER.reset(token)


def throttled_appids() -> frozenset | None:
    """``(provider, credential)`` AppIds this dispatch should step off, or None outside the scope.

    The union of the owner's open quota alerts (every process) and the lanes
    with an episode open in this process, which the alert may not show yet
    while its delivery is in flight.
    """
    owner = _AVOIDING_OWNER.get()
    if owner is None:
        return None
    appids = set(_WATCH.open_appids())
    try:
        from lib.router.watchdog.quota import open_quota_appids
        appids |= open_quota_appids(owner)
    except Exception:
        logger.warning('[QuotaWatch] open quota alerts unreadable owner=%s', owner, exc_info=True)
    return frozenset(appids)


@dataclass(frozen=True)
class _LaneRef:
    identity: tuple
    provider_id: str
    credential: str
    key_tail: str
    model: str


@dataclass(frozen=True)
class _Event:
    at: float
    owner: int | None    # the conversation round's owner; None for auxiliary work
    limit: str | None    # 'rpm' or 'tpm' for a minute-limit rejection; None when accepted
    wait_ms: float       # dispatcher backpressure this request had waited
    ttft_ms: float | None
    retry_after_s: float | None = None   # the hold the provider named for a rejection

    @property
    def horizon(self) -> float:
        """Until when this rejection is evidence of a full limit: the provider's window or its named hold."""
        hold = self.retry_after_s if self.retry_after_s is not None and self.retry_after_s > WINDOW_S else WINDOW_S
        return self.at + hold


@dataclass(frozen=True)
class _Window:
    """One lane's counts over the current window; the only production/auxiliary classification."""
    accepted: int
    rejected: int
    production_accepted: int
    production_rejected: int
    production_held: int
    longest_wait_ms: float
    rpm: int
    tpm: int
    ttfts: tuple
    rejections: tuple    # the window's conversation-round rejections, oldest first

    @classmethod
    def of(cls, events) -> '_Window':
        production = [event for event in events if event.owner is not None]
        production_rejections = tuple(event for event in production if event.limit)
        held = [event.wait_ms for event in production if event.wait_ms > 0]
        return cls(
            accepted=sum(event.limit is None for event in events),
            rejected=sum(bool(event.limit) for event in events),
            production_accepted=len(production) - len(production_rejections),
            production_rejected=len(production_rejections),
            production_held=len(held),
            longest_wait_ms=max(held, default=0.0),
            rpm=sum(event.limit == 'rpm' for event in production_rejections),
            tpm=sum(event.limit == 'tpm' for event in production_rejections),
            ttfts=tuple(event.ttft_ms for event in production
                        if event.limit is None and event.ttft_ms is not None),
            rejections=production_rejections,
        )


@dataclass
class _Episode:
    opened_at: float
    first_rejected_at: float
    last_rejected_at: float
    production_rejected: int
    held: int
    longest_wait_ms: float
    horizon: float
    # owner -> the delivery record its open and close transitions share
    deliveries: dict = field(default_factory=dict)
    # requests the provider accepted on the lane after its last rejection
    production_accepted_since: int = 0
    auxiliary_accepted_since: int = 0
    timer_at: float | None = None

    def note(self, event: _Event) -> None:
        if event.limit:
            self.horizon = max(self.horizon, event.horizon)
            self.production_accepted_since = self.auxiliary_accepted_since = 0
            if event.owner is None:
                return
            self.production_rejected += 1
            self.last_rejected_at = event.at
        elif event.owner is None:
            self.auxiliary_accepted_since += 1
            return
        else:
            self.production_accepted_since += 1
        if event.wait_ms > 0:
            self.held += 1
            self.longest_wait_ms = max(self.longest_wait_ms, event.wait_ms)


@dataclass
class _Lane:
    provider_id: str
    credential: str
    key_tail: str
    model: str
    digest: str
    events: deque = field(default_factory=deque)
    clean_capacity: int = 0
    production_rejected_total: int = 0
    auxiliary_rejected_total: int = 0
    episode: _Episode | None = None

    def prune(self, now: float) -> None:
        while self.events and self.events[0].at <= now - WINDOW_S:
            self.events.popleft()


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _hold(ms: float) -> str:
    return f'{ms / 1000:.1f} s'


class QuotaWatch:
    """Per-process episodes of every quota lane in use; ``observe`` is the only writer."""

    def __init__(self, *, clock=time.time, deliver=None, reporter=None, schedule=None):
        self._clock = clock
        self._deliver = deliver
        # ``schedule(delay_s, callback)`` runs callback once after delay_s; None schedules nothing
        # (callers then rely on observations and reads to reach a horizon).
        self._schedule = schedule
        self._lock = threading.Lock()
        self._lanes: dict[tuple, _Lane] = {}
        self._reporter = reporter
        self._reporter_pid = None

    @property
    def reporter(self) -> str:
        """This process's identity as a reporter; recomputed after a fork (shards fork from one image)."""
        if self._reporter is None or (self._reporter_pid is not None and self._reporter_pid != os.getpid()):
            from lib.storage.worker_process import worker_process_id
            self._reporter = worker_process_id(uuid.uuid4().hex, legacy_prefix='quota-watch')
            self._reporter_pid = os.getpid()
        return self._reporter

    def open_appids(self) -> set:
        """``(provider, credential)`` of every lane with an episode open in this process."""
        with self._lock:
            transitions = self._close_due(self._clock())
            appids = {(lane.provider_id, lane.credential) for lane in self._lanes.values() if lane.episode}
        self._publish(transitions)
        return appids

    def settle(self) -> None:
        """Close every episode whose evidence horizon has passed; the episode timers call this."""
        with self._lock:
            transitions = self._close_due(self._clock())
        self._publish(transitions)

    def observe(self, ref: _LaneRef, *, owner: int | None, limit: str | None, queue_wait_ms: float,
                ttft_ms: float | None, retry_after_s: float | None = None) -> None:
        """Record one provider answer on a lane and deliver the episode transitions it caused."""
        with self._lock:
            event = _Event(self._clock(), owner, limit, max(0.0, float(queue_wait_ms or 0.0)), ttft_ms,
                           retry_after_s)
            transitions = self._observe(ref, event)
        self._publish(transitions)

    def _publish(self, transitions: list[dict]) -> None:
        deliver = self._deliver or _deliver_to_watchdog
        for transition in transitions:
            logger.info('[QuotaWatch] %s production quota episode on …%s %s (%s) owner=%s%s',
                        'opened' if transition['kind'] == 'open' else 'closed', transition['keyTail'],
                        transition['model'], transition['providerId'], transition['owner'],
                        f" ({transition['cause']})" if transition['kind'] == 'close' else '')
            deliver(transition)

    def _close_due(self, now: float) -> list[dict]:
        """Close the episodes whose horizon passed with no rejection; re-arm the timers of the rest."""
        transitions = []
        for lane in self._lanes.values():
            episode = lane.episode
            if episode is None:
                continue
            if now >= episode.horizon:
                lane.episode = None
                transitions += [self._close(lane, episode, owner, now) for owner in episode.deliveries]
            else:
                self._arm(episode)
        return transitions

    def _arm(self, episode: _Episode) -> None:
        """One pending timer per episode, at its current horizon: a lane nothing routes to still closes."""
        if self._schedule is None or episode.timer_at == episode.horizon:
            return
        if episode.timer_at is not None and episode.timer_at < episode.horizon:
            return  # the pending timer fires first and re-arms for the later horizon
        at = episode.timer_at = episode.horizon
        self._schedule(max(0.0, at - self._clock()), lambda: self._on_timer(at))

    def _on_timer(self, at: float) -> None:
        try:
            with self._lock:
                for lane in self._lanes.values():
                    if lane.episode is not None and lane.episode.timer_at == at:
                        lane.episode.timer_at = None
                transitions = self._close_due(self._clock())
            self._publish(transitions)
        except Exception:
            logger.warning('[QuotaWatch] horizon check failed', exc_info=True)

    def _observe(self, ref: _LaneRef, event: _Event) -> list[dict]:
        lane = self._lanes.get(ref.identity)
        if lane is None:
            digest = hashlib.sha256(json.dumps(ref.identity, ensure_ascii=False).encode()).hexdigest()[:16]
            lane = self._lanes[ref.identity] = _Lane(ref.provider_id, ref.credential, ref.key_tail, ref.model,
                                                     digest)
        lane.events.append(event)
        if event.limit:
            if event.owner is None:
                lane.auxiliary_rejected_total += 1
            else:
                lane.production_rejected_total += 1
        lane.prune(event.at)
        window = _Window.of(lane.events)
        if not window.rejected:
            lane.clean_capacity = max(lane.clean_capacity, window.accepted)
        # An accepted round counts toward recovery before the horizon check that
        # it may itself be the first observation past.
        if lane.episode is not None and not event.limit:
            lane.episode.note(event)
        transitions = self._close_due(event.at)
        episode = lane.episode
        if episode is None:
            if window.production_rejected and window.production_held:
                transitions += self._open_episode(lane, window, event.at)
            return transitions
        if not event.limit:
            return transitions
        episode.note(event)
        self._arm(episode)
        if event.owner is None or event.owner in episode.deliveries:
            return transitions
        return transitions + [self._open(lane, episode, event.owner, self._evidence(lane, window, event.at))]

    def _open_episode(self, lane: _Lane, window: _Window, now: float) -> list[dict]:
        episode = lane.episode = _Episode(
            opened_at=now, first_rejected_at=window.rejections[0].at, last_rejected_at=window.rejections[-1].at,
            production_rejected=window.production_rejected, held=window.production_held,
            longest_wait_ms=window.longest_wait_ms,
            horizon=max(event.horizon for event in lane.events if event.limit))
        last_rejection = max(event.at for event in lane.events if event.limit)
        for event in lane.events:
            if event.at > last_rejection and not event.limit:
                if event.owner is None:
                    episode.auxiliary_accepted_since += 1
                else:
                    episode.production_accepted_since += 1
        self._arm(episode)
        evidence = self._evidence(lane, window, now)
        owners = dict.fromkeys(event.owner for event in window.rejections)
        return [self._open(lane, episode, owner, evidence) for owner in owners]

    def _evidence(self, lane: _Lane, window: _Window, now: float) -> dict:
        others = []
        for other in self._lanes.values():
            if other is lane or (other.provider_id, other.model) != (lane.provider_id, lane.model):
                continue
            other.prune(now)
            if other.events:
                counts = _Window.of(other.events)
                others.append({'keyTail': other.key_tail, 'accepted': counts.accepted,
                               'rejected': counts.rejected})
        return {
            'window': {'startMs': _ms(now - WINDOW_S), 'endMs': _ms(now)},
            'production': {'accepted': window.production_accepted, 'rejected': window.production_rejected,
                           'held': window.production_held, 'longestWaitMs': window.longest_wait_ms,
                           'ttftMedianMs': statistics.median(window.ttfts) if window.ttfts else None,
                           'ttftMaxMs': max(window.ttfts, default=None)},
            'auxiliary': {'accepted': window.accepted - window.production_accepted,
                          'rejected': window.rejected - window.production_rejected},
            'limits': {'rpm': window.rpm, 'tpm': window.tpm},
            'ownAnswered': window.accepted + window.rejected,
            'cleanCapacity': lane.clean_capacity,
            'otherAppIds': sorted(others, key=lambda entry: entry['keyTail']),
        }

    def _transition(self, kind: str, lane: _Lane, episode: _Episode, owner: int) -> dict:
        return {'kind': kind, 'owner': owner, 'providerId': lane.provider_id, 'lane': lane.digest,
                'credential': lane.credential, 'reporter': self.reporter,
                'keyTail': lane.key_tail, 'model': lane.model, 'openedAtMs': _ms(episode.opened_at),
                'delivery': episode.deliveries.setdefault(owner, {'signalId': None})}

    def _open(self, lane: _Lane, episode: _Episode, owner: int, evidence: dict) -> dict:
        return {**self._transition('open', lane, episode, owner), 'evidence': evidence,
                'detail': _open_detail(lane, evidence)}

    def _close(self, lane: _Lane, episode: _Episode, owner: int, now: float) -> dict:
        totals = {'productionRejected': episode.production_rejected, 'held': episode.held,
                  'longestWaitMs': episode.longest_wait_ms, 'firstRejectedAtMs': _ms(episode.first_rejected_at),
                  'lastRejectedAtMs': _ms(episode.last_rejected_at), 'horizonMs': _ms(episode.horizon),
                  'productionAcceptedSince': episode.production_accepted_since,
                  'auxiliaryAcceptedSince': episode.auxiliary_accepted_since}
        lane_name = f'AppId …{lane.key_tail} ({lane.provider_id}, model {lane.model})'
        if episode.production_accepted_since:
            cause = CLOSE_RECOVERED
            outcome = (
                f'Recovered at {_utc(now)} UTC: conversation rounds on {lane_name} were accepted with no per-minute '
                f'limit rejection for a full provider minute. ')
        else:
            cause = CLOSE_LAPSED
            outcome = (
                f'Lapsed at {_utc(now)} UTC: no conversation round used {lane_name} in the provider minute (and any '
                'hold the provider named) after its last per-minute limit rejection, so none was rejected or held; '
                f'auxiliary requests accepted on it meanwhile: {episode.auxiliary_accepted_since}. Tofu sent no '
                "probe to close this; the limit's state since is unobserved, and a new rejection of held rounds "
                'opens a new episode. ')
        resolution = (
            f'{outcome}Episode from {_utc(episode.first_rejected_at)} to {_utc(episode.last_rejected_at)} UTC: '
            f'{episode.production_rejected} conversation rounds rejected, {episode.held} held '
            f'(longest hold {_hold(episode.longest_wait_ms)}).')
        return {**self._transition('close', lane, episode, owner), 'closedAtMs': _ms(now), 'cause': cause,
                'evidence': {'episode': totals}, 'resolution': resolution}

    def snapshot(self) -> list[dict]:
        """Every lane in use, with its current window size, totals and open episode."""
        self.settle()
        with self._lock:
            now = self._clock()
            rows = []
            for lane in self._lanes.values():
                lane.prune(now)
                episode = lane.episode
                rows.append({
                    'providerId': lane.provider_id, 'keyTail': lane.key_tail, 'model': lane.model,
                    'windowEvents': len(lane.events), 'cleanCapacity': lane.clean_capacity,
                    'productionRejectedTotal': lane.production_rejected_total,
                    'auxiliaryRejectedTotal': lane.auxiliary_rejected_total,
                    'episode': None if episode is None else {'openedAtMs': _ms(episode.opened_at),
                                                             'owners': sorted(episode.deliveries)}})
            return rows


def _open_detail(lane: _Lane, evidence: dict) -> str:
    production, auxiliary, window = evidence['production'], evidence['auxiliary'], evidence['window']
    ttft = ('none accepted' if production['ttftMedianMs'] is None else
            f"median {_hold(production['ttftMedianMs'])}, max {_hold(production['ttftMaxMs'])}")
    others = '; '.join(f"…{entry['keyTail']} accepted {entry['accepted']}, rejected {entry['rejected']}"
                       for entry in evidence['otherAppIds']) or 'none'
    return '\n'.join([
        f'Conversation rounds on AppId …{lane.key_tail} at provider {lane.provider_id}, model {lane.model}, were '
        "rejected by the provider's per-AppId per-minute limit and held by the dispatcher.",
        f"Window {_utc(window['startMs'] / 1000)} to {_utc(window['endMs'] / 1000)} UTC (one provider minute): "
        f"conversation rounds accepted {production['accepted']}, rejected {production['rejected']} (requests per "
        f"minute {evidence['limits']['rpm']}, tokens per minute {evidence['limits']['tpm']}), held "
        f"{production['held']} (longest hold {_hold(production['longestWaitMs'])}); TTFT of accepted rounds "
        f"{ttft}. Auxiliary requests accepted {auxiliary['accepted']}, rejected {auxiliary['rejected']}.",
        f"Tofu's own load on this AppId and model: {evidence['ownAnswered']} requests answered in the window; the "
        "most it accepted in one window without a rejection since this process started: "
        f"{evidence['cleanCapacity']}.",
        f'Other AppIds serving {lane.model} in the window: {others}.',
    ])


def _lane_ref(slot, message: str = '') -> _LaneRef:
    match = _MINUTE_LIMIT.search(message)
    # The key's last four characters, as the provider's own 429 masks the AppId.
    key_tail = (getattr(slot, 'api_key', '') or '')[-4:] or (match.group(1)[-4:] if match else '')
    identity = slot.quota_lane()
    return _LaneRef(identity=identity, provider_id=identity[0], credential=identity[1], key_tail=key_tail,
                    model=slot.model)


def observe_rejection(slot, error, *, queue_wait_ms: float) -> None:
    """One provider 429 on ``slot``; only the gateway's per-AppId minute limit is recorded."""
    try:
        message = str(error)
        limit = minute_limit(message)
        if limit is None:
            return
        retry_after_s = getattr(error, 'retry_after_s', None)
        _WATCH.observe(_lane_ref(slot, message), owner=production_owner(), limit=limit,
                       queue_wait_ms=queue_wait_ms, ttft_ms=None,
                       retry_after_s=float(retry_after_s) if isinstance(retry_after_s, (int, float)) else None)
    except Exception:
        logger.debug('[QuotaWatch] rejection not observed', exc_info=True)


def observe_settlement(slot, *, ttft_ms: float | None, queue_wait_ms: float) -> None:
    """One request ``slot``'s provider accepted: a verified-complete stream or a chat response."""
    try:
        _WATCH.observe(_lane_ref(slot), owner=production_owner(), limit=None,
                       queue_wait_ms=queue_wait_ms, ttft_ms=ttft_ms)
    except Exception:
        logger.debug('[QuotaWatch] settlement not observed', exc_info=True)


# (lane, owner) -> the thread delivering that pair's latest transition
_DELIVERIES: dict[tuple[str, int], threading.Thread] = {}
_DELIVERIES_LOCK = threading.Lock()


def _deliver_to_watchdog(transition: dict) -> None:
    """Hand one transition to the Watchdog producer off the dispatch thread, after the pair's previous one."""
    key = (transition['lane'], transition['owner'])
    with _DELIVERIES_LOCK:
        thread = threading.Thread(target=_deliver, args=(_DELIVERIES.get(key), transition), daemon=True,
                                  name=f'quota-watch-{transition["kind"]}')
        _DELIVERIES[key] = thread
        thread.start()


def _deliver(previous: threading.Thread | None, transition: dict) -> None:
    if previous is not None:
        previous.join()
    try:
        from lib.router.watchdog import quota
        record = transition['delivery']
        if transition['kind'] == 'open':
            signal = quota.report_quota_episode(
                transition['owner'], provider_id=transition['providerId'], lane=transition['lane'],
                credential=transition['credential'], key_tail=transition['keyTail'], model=transition['model'],
                opened_at_ms=transition['openedAtMs'], detail=transition['detail'],
                reporter=transition['reporter'])
            record['signalId'] = (signal or {}).get('signalId')
        elif record.get('signalId'):
            quota.resolve_quota_episode(transition['owner'], lane=transition['lane'],
                                        reporter=transition['reporter'], resolution=transition['resolution'])
    except Exception:
        logger.warning('[QuotaWatch] %s delivery failed owner=%s', transition.get('kind'),
                       transition.get('owner'), exc_info=True)


def _timer(delay_s: float, callback) -> None:
    timer = threading.Timer(delay_s, callback)
    timer.daemon = True
    timer.name = 'quota-watch-horizon'
    timer.start()


_WATCH = QuotaWatch(schedule=_timer)


__all__ = ['CLOSE_LAPSED', 'CLOSE_RECOVERED', 'QuotaWatch', 'WINDOW_S', 'avoid_throttled_appids', 'minute_limit', 'observe_rejection',
           'observe_settlement', 'production_dispatch', 'production_owner', 'throttled_appids']
