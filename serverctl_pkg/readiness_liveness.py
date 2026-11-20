"""Readiness liveness contract: when an unanswered readiness probe means down.

Responsibility: the single definition of the application server's liveness
contract (docs/modules/infra_runtime.md, "Readiness liveness contract"): the
per-probe deadline, the run of consecutive deadline expiries after which a
server that answers nothing counts as down, and the classification of one
probe's transport outcome. A server that answers ``/api/ready`` within the
deadline, however slowly, is never down; connection refused or reset is down
at once.

Entry points: ``READINESS_PROBE_DEADLINE_SECONDS``, ``READINESS_TIMEOUT_RUN``,
``probe_outcome``, ``timeout_run``, ``readiness_verdict``.
Dependencies: the standard library only. ``server_manager``, ``serverctl`` and
the out-of-band watcher (``oob_alert.py``, also run as a script) import it.
"""
from __future__ import annotations

import socket
import urllib.error

READINESS_PROBE_DEADLINE_SECONDS = 10.0
READINESS_TIMEOUT_RUN = 3

ANSWERED = 'answered'
TIMEOUT = 'timeout'
UNANSWERED = 'unanswered'

UP = 'up'
SLOW = 'slow'
DOWN = 'down'


def probe_outcome(error: BaseException | None) -> str:
    """``answered`` (no transport error), ``timeout`` (deadline expired) or ``unanswered``."""
    if error is None:
        return ANSWERED
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return TIMEOUT
    return UNANSWERED


def timeout_run(previous_run: int, outcome: str) -> int:
    """Consecutive deadline expiries including this probe; any other outcome ends the run."""
    return previous_run + 1 if outcome == TIMEOUT else 0


def readiness_verdict(outcome: str, run: int) -> str:
    """``up`` when answered, ``slow`` while a timeout run is shorter than the contract, else ``down``."""
    if outcome == ANSWERED:
        return UP
    if outcome == TIMEOUT and run < READINESS_TIMEOUT_RUN:
        return SLOW
    return DOWN
