"""lib/conversations/drift_repair.py — turn a detected stall into a correction.

WHY THIS EXISTS (pt_cadaa70ffa6b468d)
-------------------------------------
``drift_tracker`` can already tell a client that is merely sampling late (its
value is MOVING, just 60s behind) from one that is frozen while the server
advances — the latter being the "notify frame was dropped and this tab will
never converge on its own" case. But the whole subsystem stopped at
*observation*: the verdict only chose a log level. The server knew exactly
which socket was stuck and did nothing about it, so recovery still required
the user to press F5.

That is the second half of the reported symptom: not only was the frontend
lagging, it had no way to find out, and nothing was coming to tell it.

This module is the repair half. It is deliberately SEPARATE from the tracker:

  * the tracker answers "is this a fault?" — pure, in-memory, no I/O;
  * this answers "should I spend a frame fixing it, and have I already?" —
    policy plus a rate limit.

Keeping them apart is what lets the repair policy be unit-tested without a
push hub, and stops a future edit from making the *detector* mutate state.

WHY A COOLDOWN IS PART OF THE DESIGN, NOT AN OPTIMISATION
---------------------------------------------------------
A stalled client keeps POSTing its digest every 60s. If the snapshot does not
heal it (say the socket is half-open in a way the ping watchdog has not yet
caught), an uncooled repair would emit a frame per client per minute for as
long as the condition lasts — turning a repair into a slow flood exactly when
the transport is already unhealthy. The cooldown bounds that to one attempt
per socket per window, so a repair that is not working degrades to "rare and
visible in the log" rather than "constant and invisible".

A repair is also only attempted for a SUSTAINED stall (tracker's own
threshold, ~180s = at least three consecutive frozen reports). A single missed
beat is not worth a frame; the next notify almost always closes it.
"""

from __future__ import annotations

import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'should_repair',
    'note_repair_attempt',
    'repair_cooldown_sec',
    'reset',
    'tracked_sockets',
]

#: Minimum gap between two repair frames aimed at the SAME socket.
#:
#: Default 300s: the client reports every ~60s, so this admits at most one
#: repair per five reports. Overridable for an investigation without a code
#: change, matching how the tracker exposes its own threshold.
_DEFAULT_COOLDOWN_SEC = 300.0

#: Hard cap on tracked sockets, so a client cycling its ``_rid`` (every
#: reconnect mints a fresh one — see push.js::_mintWsRid) cannot pin unbounded
#: memory. Oldest-touched entries are evicted first.
_MAX_TRACKED = 2000

_lock = threading.Lock()
_last_repair: dict[str, float] = {}


def repair_cooldown_sec() -> float:
    """Seconds a socket must wait between two repair frames."""
    raw = os.environ.get('TOFU_SYNC_REPAIR_COOLDOWN_SEC', '')
    if not raw:
        return _DEFAULT_COOLDOWN_SEC
    try:
        val = float(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[DriftRepair] bad TOFU_SYNC_REPAIR_COOLDOWN_SEC=%r: %s',
                     raw, e)
        return _DEFAULT_COOLDOWN_SEC
    return val if val > 0 else _DEFAULT_COOLDOWN_SEC


def should_repair(socket_id: str, verdicts, now: float | None = None) -> bool:
    """Decide whether to push a corrective snapshot to ``socket_id``.

    Args:
        socket_id: The reporting client's push-socket correlation id. Empty
            means the client did not tell us which socket it is — we cannot
            target a repair, so the answer is always False. (Broadcasting
            "somewhere in the fleet" instead would hit healthy tabs and hide
            the condition; see PushHub.deliver_to_socket.)
        verdicts: The per-divergence verdict dicts from
            ``drift_tracker.observe_divergence``. A repair is warranted iff at
            least one is ``sustained`` — i.e. the client value is frozen while
            the server advances, and has been for the tracker's threshold.
        now: Epoch seconds; injectable so tests need no sleeps.

    Returns:
        True iff a repair should be sent now (sustained stall AND out of
        cooldown). Does NOT record the attempt — call ``note_repair_attempt``
        once the frame is actually delivered, so a delivery that fails (no
        such socket on this replica) does not burn the window.
    """
    if not socket_id:
        return False
    try:
        sustained = any(bool(v.get('sustained')) for v in (verdicts or [])
                        if isinstance(v, dict))
    except Exception as e:
        logger.debug('[DriftRepair] malformed verdicts (%r): %s', verdicts, e)
        return False
    if not sustained:
        return False

    ts = time.time() if now is None else now
    with _lock:
        prev = _last_repair.get(socket_id)
    if prev is not None and (ts - prev) < repair_cooldown_sec():
        logger.debug('[DriftRepair] socket=%s still in cooldown (%.0fs ago)',
                     socket_id[:8], ts - prev)
        return False
    return True


def note_repair_attempt(socket_id: str, now: float | None = None) -> None:
    """Record that a repair frame was DELIVERED to ``socket_id``.

    Separate from :func:`should_repair` on purpose: the cooldown must start
    when a frame actually reached the socket, not when we merely decided to
    try. A repair that could not be delivered (socket lives on another
    replica) should be retried on the next probe rather than suppressed for
    the whole window.
    """
    ts = time.time() if now is None else now
    with _lock:
        if socket_id not in _last_repair and len(_last_repair) >= _MAX_TRACKED:
            oldest = min(_last_repair, key=_last_repair.get)
            _last_repair.pop(oldest, None)
        _last_repair[socket_id] = ts


def tracked_sockets() -> int:
    """Number of sockets currently holding a cooldown. Diagnostics/tests."""
    with _lock:
        return len(_last_repair)


def reset() -> None:
    """Drop all cooldown state. Test-support only."""
    with _lock:
        _last_repair.clear()
