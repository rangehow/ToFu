"""Provider-signalled pacing of one upstream request quota (docs/NETWORK.md).

A provider that limits requests per window (a gateway AppId's per-minute
request limit, an API key's RPM) states the limit only in its responses: a
429, optionally carrying ``Retry-After`` or rate-limit headers. This module
turns those signals into first-in, first-out send admission for the requests
drawing on one quota lane, so a burst above the limit waits for the instant
the provider's window has room instead of being rejected and held behind
retries.

Entry points: ``shared_pacer()`` (the process registry), ``ProviderPacer.join``,
``PacingTicket.poll``/``cancel``/``rejected``/``accepted``, ``paced_send``
with ``note_wire_send`` (the managed transports stamp the actual send) and
``response_rate_signals`` (header parsing). A request owner names its lane (an
opaque, non-secret identity of the provider quota, e.g. provider, credential
ID, wire model) and the window the provider counts that quota over, and waits
out the delays ``poll`` returns with its own abortable wait. The module never
sends, retries or selects a route. Dependencies: standard library,
``lib.agent_core.abort_signal`` (wake signals) and the route-health retention
bound of ``lib.network_routes``.
"""
from __future__ import annotations

from bisect import insort
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import itertools
import math
import re
import threading
import time

from lib.agent_core.abort_signal import AbortSignal

__all__ = ['PacingTicket', 'ProviderPacer', 'RateSignals',
           'current_paced_send', 'note_wire_send', 'paced_send',
           'reset_shared_pacer', 'response_rate_signals', 'shared_pacer']


@dataclass(frozen=True)
class RateSignals:
    """What one provider response states about its request limit."""

    retry_after_s: float | None = None  # Retry-After: send nothing sooner
    limit: int | None = None            # requests admitted per window
    remaining: int | None = None        # requests left in the current window
    reset_s: float | None = None        # seconds until the request window resets
    window_s: float | None = None       # the limit's window, when named


_DURATION_PART = re.compile(r'(\d+(?:\.\d+)?)(ms|h|m|s)')
_DURATION_UNIT_S = {'h': 3600.0, 'm': 60.0, 's': 1.0, 'ms': 0.001}
_POLICY_WINDOW = re.compile(r'(?:^|;)\s*w\s*=\s*(\d+(?:\.\d+)?)')


def _header(headers, name: str):
    value = headers.get(name)
    if value is None and isinstance(headers, dict):
        lowered = name.lower()
        value = next((item for key, item in headers.items()
                      if str(key).lower() == lowered), None)
    return None if value is None else str(value).strip()


def _non_negative_int(value):
    try:
        number = int(str(value).split(',')[0].strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _seconds(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _duration_s(value):
    """OpenAI reset durations: ``1s``, ``6m0s``, ``1h2m3.5s``, ``20ms``."""
    if not value:
        return None
    parts = _DURATION_PART.findall(value)
    if not parts or ''.join(number + unit for number, unit in parts) != value:
        return _seconds(value)
    return sum(float(number) * _DURATION_UNIT_S[unit] for number, unit in parts)


def _until_s(value, now_epoch):
    """Seconds from ``now_epoch`` until an HTTP-date or RFC 3339 instant."""
    try:
        moment = (parsedate_to_datetime(value) if not value[:4].isdigit()
                  else datetime.fromisoformat(value.replace('Z', '+00:00')))
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, moment.timestamp() - now_epoch)


def response_rate_signals(headers, *, now_epoch: float | None = None):
    """Parse a response's request-limit headers; None when it states none.

    ``Retry-After`` (RFC 9110: delay-seconds or HTTP-date), the OpenAI
    ``x-ratelimit-*-requests`` family, Anthropic's
    ``anthropic-ratelimit-requests-*`` family and the IETF ``RateLimit-*``
    fields (``RateLimit-Policy`` ``w=`` names the window).
    """
    if headers is None:
        return None
    now_epoch = time.time() if now_epoch is None else now_epoch
    retry_after = _header(headers, 'Retry-After')
    retry_after_s = None
    if retry_after:
        retry_after_s = _seconds(retry_after)
        if retry_after_s is None:
            retry_after_s = _until_s(retry_after, now_epoch)
    limit = remaining = reset_s = window_s = None
    families = (
        ('x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
         'x-ratelimit-reset-requests', _duration_s),
        ('anthropic-ratelimit-requests-limit',
         'anthropic-ratelimit-requests-remaining',
         'anthropic-ratelimit-requests-reset',
         lambda value: _until_s(value, now_epoch)),
        ('RateLimit-Limit', 'RateLimit-Remaining', 'RateLimit-Reset', _seconds),
    )
    for limit_name, remaining_name, reset_name, parse_reset in families:
        limit = _non_negative_int(_header(headers, limit_name))
        remaining = _non_negative_int(_header(headers, remaining_name))
        reset = _header(headers, reset_name)
        reset_s = parse_reset(reset) if reset else None
        if limit is not None or remaining is not None or reset_s is not None:
            break
    policy = _header(headers, 'RateLimit-Policy')
    if policy:
        match = _POLICY_WINDOW.search(policy)
        window_s = float(match.group(1)) if match else None
    if not limit:
        limit = None
    signals = RateSignals(retry_after_s, limit, remaining, reset_s, window_s)
    return None if signals == RateSignals() else signals


class _Lane:
    __slots__ = ('window_s', 'skew_s', 'limit', 'declared', 'sent',
                 'waiting', 'hold_until', 'next_probe_at', 'changed', 'touched')

    def __init__(self, window_s: float, now: float):
        self.window_s = window_s
        # How long after its write the provider may still be counting a
        # request: the latest refusal's round trip on this lane bounds the
        # time from our write to the provider's limit check.
        self.skew_s = 0.0
        self.limit = None          # requests per window; None = not paced
        self.declared = False      # the provider stated the limit itself
        self.sent = []             # granted tickets still inside the window
        self.waiting = []          # tickets not yet granted, by order
        self.hold_until = 0.0      # no grant before (Retry-After, reset)
        self.next_probe_at = 0.0   # earliest send beyond a learned limit
        self.changed = AbortSignal()
        self.touched = now

    def idle(self, now: float) -> bool:
        return (not self.waiting and now >= self.hold_until
                and all(ticket.sent_at <= now - self.occupancy_s
                        for ticket in self.sent))

    @property
    def occupancy_s(self) -> float:
        """How long a written request may count against the provider's window."""
        return self.window_s + self.skew_s


class PacingTicket:
    """One request's place in its lane, from joining until its outcome.

    ``sent_at`` is when the request occupies the provider's window from: its
    grant, then each time a managed transport writes it (``note_wire_send``).
    """

    __slots__ = ('_pacer', 'lane', 'order', 'state', 'granted_at', 'sent_at',
                 'probe')

    def __init__(self, pacer, lane, *, order: int):
        self._pacer = pacer
        self.lane = lane
        self.order = order
        self.state = 'waiting'
        self.granted_at = None
        self.sent_at = None
        self.probe = False

    def poll(self):
        """Grant due requests; ``(seconds until this one may send, wake signal)``.

        Zero seconds means this request is cleared to send now. The signal
        fires when the lane changes, so a waiter re-polls instead of sleeping
        through an earlier turn.
        """
        return self._pacer._poll(self)

    def cancel(self) -> None:
        """Leave the queue without sending (an abort or deferral)."""
        self._pacer._cancel(self)

    def rejected(self, signals: RateSignals | None = None, *,
                 request_limit: bool = False) -> None:
        """The provider refused this send.

        ``request_limit`` states that the refusal names a request-count limit
        over the lane's window (for example a gateway's per-minute request
        limit): the provider admitted this lane's requests already inside the
        window when this one arrived, and no more.
        """
        self._pacer._rejected(self, signals, request_limit)

    def accepted(self) -> None:
        """The provider served this send."""
        self._pacer._accepted(self)


_paced_send: ContextVar[PacingTicket | None] = ContextVar(
    'tofu_paced_send', default=None)


@contextmanager
def paced_send(ticket: PacingTicket | None):
    """Bind the requests this block sends to ``ticket`` (None binds nothing)."""
    token = _paced_send.set(ticket)
    try:
        yield ticket
    finally:
        _paced_send.reset(token)


def current_paced_send() -> PacingTicket | None:
    """The ticket bound by :func:`paced_send`, for handing to a worker thread."""
    return _paced_send.get()


def note_wire_send() -> None:
    """Managed transports call this just before writing a request.

    A paced request occupies the provider's window from when it is written,
    not from its grant: preparing a large body takes long enough that an
    older request can reach the provider after a newer one would be granted.
    """
    ticket = _paced_send.get()
    if ticket is not None:
        ticket._pacer._wire_send(ticket)


class ProviderPacer:
    """First-in, first-out send admission per provider quota lane.

    A lane is unpaced until the provider states a limit. A refusal naming a
    request limit over the window teaches the number of this lane's requests
    that were inside that window when the refused one was written;
    ``RateLimit``/``x-ratelimit`` headers state the limit directly. Once
    paced, a request is granted while fewer than the limit occupy the
    trailing window, otherwise at the instant the oldest of them leaves it. A
    write occupies one window plus the lane's latest refusal round trip, the
    bound on how late the provider's limit check can count it. Requests are
    granted in the order they first joined; a request that
    comes back keeps its turn ahead of later work, so no request starves.

    A learned limit may describe a moment when other workloads shared the
    quota. While requests wait behind it, one request per provider window is
    sent beyond it: the provider serving it retires the limit, refusing it
    teaches the limit again. A limit the provider declared needs no probe.
    ``Retry-After`` and an exhausted window's reset hold the whole lane.

    Waiting lanes are never evicted. Idle lanes keep their learned limit
    under the route-health retention bound (``lib.network_routes``).
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._lanes: OrderedDict = OrderedDict()
        self._orders = itertools.count()

    def join(self, lane, *, window_s: float,
             order: int | None = None) -> PacingTicket:
        """Queue one request on ``lane``, in ``order``.

        A new request takes the next order; a request that comes back (the
        provider refused it, or it moves to another lane) passes the order of
        its first ticket and keeps its turn. ``window_s`` is the provider's
        window for this quota, as the request owner knows it from the
        provider's contract; a window the provider states in its headers
        replaces it.
        """
        if not (math.isfinite(window_s) and window_s > 0):
            raise ValueError(f'invalid provider window: {window_s!r}')
        with self._lock:
            now = self._clock()
            state = self._lanes.get(lane)
            if state is None:
                state = self._lanes[lane] = _Lane(float(window_s), now)
                self._retire_idle_locked(now)
            elif not state.declared:
                state.window_s = float(window_s)
            self._lanes.move_to_end(lane)
            state.touched = now
            ticket = PacingTicket(
                self, lane, order=next(self._orders) if order is None else order)
            insort(state.waiting, ticket, key=lambda queued: queued.order)
            return ticket

    def admission_delay(self, lane) -> float:
        """Seconds until a request joining ``lane`` now would be granted.

        Read-only, for choosing among lanes: zero for a lane that is unknown,
        unpaced or has room.
        """
        with self._lock:
            state = self._lanes.get(lane)
            if state is None:
                return 0.0
            now = self._clock()
            sent_at = [ticket.sent_at for ticket in state.sent
                       if ticket.sent_at > now - state.occupancy_s]
            probe_at, at = state.next_probe_at, now
            for _queued in range(len(state.waiting) + 1):
                at, probe = self._admissible_at(state, at, sent_at, probe_at)
                sent_at.append(at)
                if probe:
                    probe_at = at + state.window_s
            return at - now

    def snapshot(self) -> list[dict]:
        """Bounded diagnostics of every paced or busy lane."""
        with self._lock:
            now = self._clock()
            rows = []
            for lane, state in self._lanes.items():
                self._expire_locked(state, now)
                if state.limit is None and state.idle(now):
                    continue
                rows.append({
                    'lane': list(lane) if isinstance(lane, tuple) else lane,
                    'limit': state.limit,
                    'limit_source': ('declared' if state.declared
                                     else 'learned' if state.limit else None),
                    'window_s': state.window_s,
                    'in_window': len(state.sent),
                    'waiting': len(state.waiting),
                    'hold_s': round(max(0.0, state.hold_until - now), 3),
                })
            return rows

    # ── internals (self._lock held unless stated) ──

    def _expire_locked(self, state, now):
        state.sent = [ticket for ticket in state.sent
                      if ticket.sent_at > now - state.occupancy_s]

    def _retire_idle_locked(self, now):
        from lib.network_routes import HEALTH_CACHE_ORIGINS, HEALTH_CACHE_TTL_S
        idle = [lane for lane, state in self._lanes.items() if state.idle(now)]
        excess = len(idle) - HEALTH_CACHE_ORIGINS
        for lane in idle:
            if excess > 0 or now - self._lanes[lane].touched >= HEALTH_CACHE_TTL_S:
                del self._lanes[lane]
                excess -= 1

    def _admissible_at(self, state, at, sent_at, probe_at):
        """Earliest instant >= ``at`` a request may go, and whether it probes."""
        at = max(at, state.hold_until)
        if state.limit is None:
            return at, False
        while True:
            recent = sorted(moment for moment in sent_at
                            if moment > at - state.occupancy_s)
            if len(recent) < state.limit:
                return at, False
            if not state.declared and probe_at <= at:
                return at, True
            free_at = recent[len(recent) - state.limit] + state.occupancy_s
            at = free_at if state.declared else min(free_at, probe_at)

    def _pump_locked(self, state, now):
        self._expire_locked(state, now)
        granted = False
        while state.waiting:
            due, probe = self._admissible_at(
                state, now, [ticket.sent_at for ticket in state.sent],
                state.next_probe_at)
            if due > now:
                break
            ticket = state.waiting.pop(0)
            ticket.state, ticket.probe = 'granted', probe
            ticket.granted_at = ticket.sent_at = now
            state.sent.append(ticket)
            if probe:
                state.next_probe_at = now + state.window_s
            granted = True
        if granted:
            self._changed_locked(state)

    def _changed_locked(self, state):
        changed, state.changed = state.changed, AbortSignal()
        changed.set()

    def _poll(self, ticket):
        with self._lock:
            if ticket.state == 'granted':
                return 0.0, AbortSignal()
            if ticket.state != 'waiting':
                raise RuntimeError(f'pacing ticket is {ticket.state}')
            now = self._clock()
            state = self._lanes[ticket.lane]
            state.touched = now
            self._pump_locked(state, now)
            if ticket.state == 'granted':
                return 0.0, state.changed
            sent_at = [queued.sent_at for queued in state.sent]
            probe_at, at = state.next_probe_at, now
            for queued in state.waiting:
                at, probe = self._admissible_at(state, at, sent_at, probe_at)
                if queued is ticket:
                    return at - now, state.changed
                sent_at.append(at)
                if probe:
                    probe_at = at + state.window_s
            raise RuntimeError('pacing ticket is not queued on its lane')

    def _wire_send(self, ticket):
        with self._lock:
            if ticket.state == 'granted':
                ticket.sent_at = max(ticket.sent_at, self._clock())

    def _cancel(self, ticket):
        with self._lock:
            state = self._lanes.get(ticket.lane)
            if ticket.state != 'waiting' or state is None:
                return
            state.waiting.remove(ticket)
            ticket.state = 'cancelled'
            self._changed_locked(state)

    def _rejected(self, ticket, signals, request_limit):
        with self._lock:
            state = self._lanes.get(ticket.lane)
            if state is None or ticket.state != 'granted':
                return
            now = self._clock()
            ticket.state = 'rejected'
            if ticket in state.sent:
                state.sent.remove(ticket)  # a refused request takes no room
            if signals is not None and signals.window_s:
                state.window_s = signals.window_s
            state.skew_s = max(0.0, now - ticket.sent_at)
            # What the provider admitted when this request reached it: the
            # lane's requests written before it that it may still count.
            admitted = sum(
                1 for sent in state.sent
                if ticket.sent_at - state.occupancy_s < sent.sent_at
                <= ticket.sent_at)
            if signals is not None and signals.limit:
                state.limit, state.declared = signals.limit, True
            elif request_limit and admitted and not state.declared:
                state.limit = admitted
            if state.limit is not None:
                state.next_probe_at = now + state.window_s
            holds = []
            if signals is not None and signals.retry_after_s is not None:
                holds.append(signals.retry_after_s)
            if (signals is not None and signals.remaining == 0
                    and signals.reset_s is not None):
                holds.append(signals.reset_s)
            if holds:
                state.hold_until = max(state.hold_until, now + max(holds))
            self._expire_locked(state, now)
            self._changed_locked(state)

    def _accepted(self, ticket):
        with self._lock:
            state = self._lanes.get(ticket.lane)
            if state is None or ticket.state != 'granted':
                return
            ticket.state = 'accepted'
            if ticket.probe and not state.declared:
                state.limit = None  # served beyond the learned limit
                self._changed_locked(state)


_shared = ProviderPacer()


def shared_pacer() -> ProviderPacer:
    """The process registry every request owner paces its lanes in."""
    return _shared


def reset_shared_pacer() -> None:
    """Forget every lane; tests isolate the process registry with it."""
    global _shared
    _shared = ProviderPacer()
