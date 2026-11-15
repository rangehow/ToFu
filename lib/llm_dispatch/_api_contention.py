"""Shared-key contention admission, backoff and recovery accounting.

After the shared-contention gate, every attempt waits for its turn in the
provider pacing queue of its slot's quota lane (``LLMDispatcher.pacing_lane``,
``lib.http_transport.provider_pacing``; docs/NETWORK.md "Provider pacing").
"""

import asyncio
import math
import time
from lib.log import get_logger
from lib.llm.diagnostic_timing import timed_stage

logger = get_logger('lib.llm_dispatch.api')


_DEFAULT_429_RETRY_DELAY_S = 0.3


def _shared_contention_retry_delay(dispatcher, slot, is_contention,
                                   log_prefix, *,
                                   per_credential: bool = False,
                                   retry_after_s: float | None = None) -> float:
    """Arm the rejected gate, retaining legacy delay for old dispatchers.

    ``per_credential`` marks a limit of the slot's own credential (a gateway
    AppId's minute limit) rather than one every credential shares.
    ``retry_after_s`` is the hold the provider named for this rejection.
    """
    if not is_contention:
        return _DEFAULT_429_RETRY_DELAY_S
    note_contention = getattr(dispatcher, 'note_shared_contention', None)
    if not callable(note_contention):
        return _DEFAULT_429_RETRY_DELAY_S
    try:
        retry_delay = float(note_contention(
            slot, per_credential=per_credential, retry_after_s=retry_after_s))
        # Current dispatchers apply the delay before the next network request,
        # where every task sees the same gate. Older/fake dispatchers
        # have no admission hook, so preserve their post-failure delay.
        if callable(getattr(
                dispatcher, 'reserve_shared_contention_probe', None)):
            return 0.0
        if math.isfinite(retry_delay) and retry_delay >= 0:
            return max(_DEFAULT_429_RETRY_DELAY_S, retry_delay)
    except Exception as exc:
        logger.debug('%s note_shared_contention failed: %s', log_prefix, exc)
    return _DEFAULT_429_RETRY_DELAY_S


def _shared_contention_admission_decision(dispatcher, slot):
    """Return a validated ``(delay_s, admitted, openings, gates)`` decision.

    ``openings`` are the armed gates' signals; decisions of dispatchers
    without them only ever wait out ``delay_s``.
    """
    reserve_probe = getattr(
        dispatcher, 'reserve_shared_contention_probe', None)
    if not callable(reserve_probe):
        return 0.0, True, (), ()
    decision = reserve_probe(slot)
    delay_s = float(decision.delay_s)
    admitted = bool(decision.admitted)
    if not math.isfinite(delay_s) or delay_s < 0:
        raise ValueError(f'invalid shared-contention delay: {delay_s!r}')
    return (delay_s, admitted, tuple(getattr(decision, 'openings', ())),
            tuple(getattr(decision, 'gates', ())))


def _shared_contention_immediate_admission(
        dispatcher, slot, log_prefix) -> float | None:
    """Return 0 when reserved now, a positive defer delay, or None to wait.

    ``None`` preserves compatibility with old/fake dispatchers and turns a
    coordinator defect into the ordinary waitable path. A positive result is
    read-only: the family probe clock is not advanced when optional work
    yields.
    """
    reserve_now = getattr(
        dispatcher, 'reserve_shared_contention_probe_now', None)
    if not callable(reserve_now):
        return None
    try:
        decision = reserve_now(slot)
        delay_s = float(decision.delay_s)
        admitted = bool(decision.admitted)
        if not math.isfinite(delay_s) or delay_s < 0:
            raise ValueError(
                f'invalid immediate shared-contention delay: {delay_s!r}')
        if admitted:
            if delay_s > 0:
                raise ValueError(
                    'immediate shared-contention admission cannot wait')
            return 0.0
        if delay_s <= 0:
            raise ValueError(
                'deferred shared-contention admission needs a positive delay')
        return delay_s
    except Exception as exc:
        logger.debug('%s immediate shared-contention admission failed: %s',
                     log_prefix, exc)
        return None


def _admit_or_defer_shared_contention(
        dispatcher, slot, sleep_fn, log_prefix, *, defer: bool,
        state, abort_check) -> float:
    """Use ordinary wait admission or yield optional work before transport.

    Returns the monotonic instant the request was cleared to send; its
    success opens only gates that last rejected before then.
    """
    if defer:
        delay_s = _shared_contention_immediate_admission(
            dispatcher, slot, log_prefix)
        if delay_s is not None:
            if delay_s > 0:
                from lib.llm_dispatch._api_errors import (
                    DispatchSharedContentionDeferred,
                )
                raise DispatchSharedContentionDeferred(
                    retry_after_s=delay_s)
            _pace_send_now(dispatcher, slot, state)
            return time.monotonic()
    _wait_for_shared_contention_admission(
        dispatcher, slot, sleep_fn, log_prefix,
        state=state, abort_check=abort_check)
    ticket = _join_paced_send(dispatcher, slot, state)
    if ticket is not None:
        _wait_for_paced_send(ticket, state=state, abort_check=abort_check,
                             log_prefix=log_prefix)
    return time.monotonic()


def _log_admission_wait(log_prefix, started, gates, opened) -> None:
    """One INFO record per admission that met an armed gate.

    An admission that met no armed gate did not wait and is not recorded.
    """
    if not gates:
        return
    logger.info('%s [Dispatch] shared-contention admission wait %.2fs on %s '
                '(%s)', log_prefix, time.monotonic() - started,
                ', '.join(f'{provider}/{credential or "*"}:{model}'
                          for provider, credential, model in gates),
                'gate opened' if opened else 'probe due')


@timed_stage('dispatcher_wait')
def _await_gate_openings(openings, seconds, *, state, abort_check) -> bool:
    """Wait until every gate in *openings* opens (True) or *seconds* pass.

    Abortable like ``abortable_sleep``; the whole wait is dispatcher
    backpressure in ``state``'s queue-wait accounting.
    """
    from lib.llm import AbortedError
    from lib.llm._transport import ABORT_POLL_INTERVAL_S
    started = time.monotonic()
    deadline = started + seconds
    try:
        for opening in openings:
            while not opening.is_set():
                if abort_check is not None and abort_check():
                    raise AbortedError(
                        'User aborted during shared-contention admission')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                opening.wait(remaining if abort_check is None
                             else min(remaining, ABORT_POLL_INTERVAL_S))
        return True
    finally:
        state.record_queue_wait(started)


async def _await_gate_openings_async(openings, seconds, *, state,
                                     abort_check) -> bool:
    """Async :func:`_await_gate_openings`: an opening wakes this loop."""
    from lib.llm import AbortedError
    from lib.llm._transport import ABORT_POLL_INTERVAL_S
    started = time.monotonic()
    deadline = started + seconds
    try:
        for opening in openings:
            subscription = opening.subscribe_async()
            try:
                while not subscription.future.done():
                    if abort_check is not None and abort_check():
                        raise AbortedError(
                            'User aborted during shared-contention admission')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    await asyncio.wait(
                        (subscription.future,),
                        timeout=(remaining if abort_check is None
                                 else min(remaining, ABORT_POLL_INTERVAL_S)))
            finally:
                subscription.close()
        return True
    finally:
        state.record_queue_wait(started)


def _wait_for_shared_contention_admission(
        dispatcher, slot, sleep_fn, log_prefix, *, state,
        abort_check) -> None:
    """Synchronously wait for one pre-request family probe reservation.

    An armed gate is awaited through its openings, so the gate opening
    releases this request at once instead of at its reserved slot.
    ``sleep_fn`` waits out decisions that carry no openings.
    """
    started = time.monotonic()
    gates = []
    opened = False
    while True:
        try:
            delay_s, admitted, openings, decision_gates = \
                _shared_contention_admission_decision(dispatcher, slot)
        except Exception as exc:
            # Fail closed to the historical 0.3s retry rather than turning a
            # coordinator defect into an unbounded upstream request herd.
            logger.debug('%s shared-contention admission failed: %s',
                         log_prefix, exc)
            sleep_fn(_DEFAULT_429_RETRY_DELAY_S)
            return
        if gates and not decision_gates:
            opened = True  # the gate opened between two decisions
        gates.extend(gate for gate in decision_gates if gate not in gates)
        if delay_s > 0:
            if openings:
                opened = _await_gate_openings(
                    openings, delay_s, state=state, abort_check=abort_check)
            else:
                sleep_fn(delay_s)
        if opened or admitted:
            break
    _log_admission_wait(log_prefix, started, gates, opened)


async def _wait_for_shared_contention_admission_async(
        dispatcher, slot, *, state, abort_check, async_sleep_fn,
        log_prefix) -> None:
    """Asynchronously wait for one pre-request family probe reservation."""
    started = time.monotonic()
    gates = []
    opened = False
    while True:
        try:
            delay_s, admitted, openings, decision_gates = \
                _shared_contention_admission_decision(dispatcher, slot)
        except Exception as exc:
            logger.debug('%s shared-contention admission failed: %s',
                         log_prefix, exc)
            wait_started = time.monotonic()
            await async_sleep_fn(_DEFAULT_429_RETRY_DELAY_S, abort_check)
            state.record_queue_wait(wait_started)
            return
        if gates and not decision_gates:
            opened = True  # the gate opened between two decisions
        gates.extend(gate for gate in decision_gates if gate not in gates)
        if delay_s > 0:
            if openings:
                opened = await _await_gate_openings_async(
                    openings, delay_s, state=state, abort_check=abort_check)
            else:
                wait_started = time.monotonic()
                await async_sleep_fn(delay_s, abort_check)
                state.record_queue_wait(wait_started)
        if opened or admitted:
            break
    _log_admission_wait(log_prefix, started, gates, opened)


async def _admit_or_defer_shared_contention_async(
        dispatcher, slot, *, state, abort_check, async_sleep_fn,
        log_prefix, defer: bool) -> float:
    """Async counterpart of :func:`_admit_or_defer_shared_contention`."""
    if defer:
        delay_s = _shared_contention_immediate_admission(
            dispatcher, slot, log_prefix)
        if delay_s is not None:
            if delay_s > 0:
                from lib.llm_dispatch._api_errors import (
                    DispatchSharedContentionDeferred,
                )
                raise DispatchSharedContentionDeferred(
                    retry_after_s=delay_s)
            _pace_send_now(dispatcher, slot, state)
            return time.monotonic()
    await _wait_for_shared_contention_admission_async(
        dispatcher,
        slot,
        state=state,
        abort_check=abort_check,
        async_sleep_fn=async_sleep_fn,
        log_prefix=log_prefix,
    )
    ticket = _join_paced_send(dispatcher, slot, state)
    if ticket is not None:
        await _wait_for_paced_send_async(
            ticket, state=state, abort_check=abort_check,
            log_prefix=log_prefix)
    return time.monotonic()


def _join_paced_send(dispatcher, slot, state):
    """Queue this attempt in its quota lane's provider pacing.

    The lane's window is the provider's limit window (``quota_watch.WINDOW_S``,
    the gateway's minute); a window the provider names in its headers
    replaces it. Dispatchers without ``pacing_lane`` (old/fake) do not pace.
    Every later attempt of the request, on any lane, keeps the turn of its
    first ticket.
    """
    lane_of = getattr(dispatcher, 'pacing_lane', None)
    if not callable(lane_of):
        return None
    from lib.http_transport.provider_pacing import shared_pacer
    from lib.llm_dispatch import quota_watch
    ticket = shared_pacer().join(
        lane_of(slot), window_s=quota_watch.WINDOW_S,
        order=getattr(state, 'pacing_order', None))
    state.pacing_order = ticket.order
    state.pacing_ticket = ticket
    return ticket


def _pace_send_now(dispatcher, slot, state) -> None:
    """Immediate-only pacing: send now or yield with the lane's delay."""
    ticket = _join_paced_send(dispatcher, slot, state)
    if ticket is None:
        return
    delay_s, _changed = ticket.poll()
    if delay_s > 0:
        ticket.cancel()
        state.pacing_ticket = None
        from lib.llm_dispatch._api_errors import (
            DispatchSharedContentionDeferred,
        )
        raise DispatchSharedContentionDeferred(retry_after_s=delay_s)


def _log_paced_wait(log_prefix, ticket, started) -> None:
    provider, credential, model = ticket.lane
    logger.info('%s [Dispatch] paced send waited %.2fs for the provider window '
                'on %s/%s:%s', log_prefix, time.monotonic() - started,
                provider, credential, model)


def _wait_for_paced_send(ticket, *, state, abort_check, log_prefix) -> None:
    """Wait, abortably and metered as queue wait, until the lane grants it."""
    started = time.monotonic()
    waited = False
    try:
        while True:
            delay_s, changed = ticket.poll()
            if delay_s <= 0:
                break
            waited = True
            _await_gate_openings((changed,), delay_s, state=state,
                                 abort_check=abort_check)
    except BaseException:
        ticket.cancel()
        raise
    if waited:
        _log_paced_wait(log_prefix, ticket, started)


async def _wait_for_paced_send_async(ticket, *, state, abort_check,
                                     log_prefix) -> None:
    """Async :func:`_wait_for_paced_send`: a lane change wakes this loop."""
    started = time.monotonic()
    waited = False
    try:
        while True:
            delay_s, changed = ticket.poll()
            if delay_s <= 0:
                break
            waited = True
            await _await_gate_openings_async(
                (changed,), delay_s, state=state, abort_check=abort_check)
    except BaseException:
        ticket.cancel()
        raise
    if waited:
        _log_paced_wait(log_prefix, ticket, started)


def _paced_call(state, call, *args, **kwargs):
    """Run one transport call with its sends bound to the attempt's ticket.

    The managed transports then stamp when the request is actually written
    (``provider_pacing.note_wire_send``).
    """
    from lib.http_transport.provider_pacing import paced_send
    with paced_send(getattr(state, 'pacing_ticket', None)):
        return call(*args, **kwargs)


async def _paced_acall(state, call, *args, **kwargs):
    """Async :func:`_paced_call`; the binding stays in this task."""
    from lib.http_transport.provider_pacing import paced_send
    with paced_send(getattr(state, 'pacing_ticket', None)):
        return await call(*args, **kwargs)


def _note_paced_send_refused(state, error) -> None:
    """The provider refused the attempt's send: its lane learns from it.

    A gateway per-minute request-limit body names a request-count limit over
    the lane's window; ``provider_rate_signals`` carries the response's
    ``Retry-After`` and rate-limit headers.
    """
    ticket = getattr(state, 'pacing_ticket', None)
    if ticket is None:
        return
    from lib.llm_dispatch.quota_watch import minute_limit
    state.pacing_ticket = None
    ticket.rejected(getattr(error, 'provider_rate_signals', None),
                    request_limit=minute_limit(str(error)) == 'rpm')


def _note_paced_send_answered(state) -> None:
    """The provider served the attempt's send."""
    ticket = getattr(state, 'pacing_ticket', None)
    if ticket is not None:
        state.pacing_ticket = None
        ticket.accepted()


def _note_shared_contention_recovered(dispatcher, slot, log_prefix, *,
                                      admitted_at: float) -> None:
    """Best-effort signal that a request cleared to send at ``admitted_at``
    (monotonic) succeeded: gates that last rejected before it open."""
    note_success = getattr(dispatcher, 'note_shared_success', None)
    if not callable(note_success):
        return
    try:
        note_success(slot, admitted_at=admitted_at)
    except Exception as exc:
        logger.debug('%s note_shared_success failed: %s', log_prefix, exc)
