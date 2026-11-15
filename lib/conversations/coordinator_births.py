"""Worker births that complete after delegate_project_work has returned.

Responsibility
--------------
Owner decision 2026-11-11: delegating must be as immediate as a human sending
a message. A delegation is admitted from the Coordinator task's memory and
returns its deterministic assignment and conversation IDs at once; its birth
(authority validation, root preparation, the Worker conversation, the grant,
its Turn and dispatch) runs on a thread of its own. Each birth is recorded on
its Coordinator task (``task[BIRTHS_KEY]``), so it lives exactly as long as
that task, and the task's other Coordinator tools can wait for it. Durable
dispatch evidence stays the authority: a birth that completed is read from
storage like any Worker; this record only holds what storage cannot show yet
(a birth in flight) or never will (a birth refused before its Worker existed).

Entry points
------------
``start`` (idempotent per assignment), ``wait``, ``unfinished``, ``failures``,
``recorded``, ``row``/``rows``, ``BirthFailed`` for a replay of a refused birth,
and ``withdraw_unadopted`` for births started while their response streamed.

A birth started from a call whose arguments finished streaming (``early``)
belongs to that occurrence until the attempt's ordinary dispatch of the same
call (same key, same arguments) adopts it. ``withdraw_unadopted`` withdraws
every early birth no dispatch adopted: one already born at once, one in
flight as it finishes, on its own thread (docs/PROJECT_COORDINATION.md, "A
delegation starts when its arguments are complete").

Dependencies
------------
Standard library and the log owner. The caller supplies the birth itself and
the refusal payload its tool would have returned.
"""
from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from lib.log import get_logger

logger = get_logger(__name__)

BIRTHS_KEY = '_coordinatorWorkerBirths'
# How often a Coordinator wait rechecks Stop and waiting input: the slice its
# Worker wait has always waited in, shared so there is one.
WAIT_SLICE_SECONDS = 0.25
_LOCK = threading.Lock()


@dataclass(eq=False)
class Birth:
    assignment_id: str
    conversation_id: str
    title: str
    digest: str
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    failure: dict | None = None
    early: bool = False
    adopted: bool = False
    withdrawn: bool = False
    finished: bool = False
    withdraw: Callable[['Birth'], None] | None = None


class BirthFailed(Exception):
    """A replayed delegation whose birth was refused; ``payload`` is that refusal."""

    def __init__(self, payload: dict):
        super().__init__(payload.get('error') or 'Worker birth failed')
        self.payload = payload


def spawn(body: Callable[[], None], name: str) -> None:
    """Run one birth on a thread of its own, in the caller's context."""
    threading.Thread(target=contextvars.copy_context().run, args=(body,), name=name,
                     daemon=True).start()


class BirthWithdrawn(RuntimeError):
    """An early birth withdrawn before it began: no dispatched call adopted it."""


def start(task: dict, *, assignment_id: str, conversation_id: str, title: str, digest: str,
          run: Callable[[], dict], refusal: Callable[[BaseException], dict],
          conflict: Callable[[], Exception], early: bool = False,
          withdraw: Callable[[Birth], None] | None = None) -> Birth:
    """The birth of ``assignment_id`` for this task: the one in flight, or a new one started now.

    A replay joins a birth still in flight. A finished one runs again, as every
    replay of a delegation always did: it reuses the accepted Worker and
    re-dispatches its grant (a deferred dependency may be ready now), or
    retries a refusal; durable admission still refuses a key reused for
    different work. Different arguments while the first birth is in flight are
    refused here with ``conflict()``, the refusal durable admission would give.

    ``early``: started from a call whose arguments finished streaming; the
    first ordinary start with the same digest adopts it as it stands, in
    flight or finished, and never runs it again. An unadopted early birth is
    never replaced: a different digest is ``conflict()`` even once it finished,
    so ``withdraw_unadopted`` still sees it. ``withdraw`` is the cancel path
    ``withdraw_unadopted`` takes for a born Worker.
    """
    with _LOCK:
        births = task.setdefault(BIRTHS_KEY, {})
        birth = births.get(assignment_id)
        if birth is not None and birth.early and not birth.adopted and not birth.withdrawn:
            if birth.digest != digest:
                raise conflict()
            birth.adopted = not early
            return birth
        if birth is not None and not birth.done.is_set():
            if birth.digest != digest:
                raise conflict()
            return birth
        birth = Birth(assignment_id, conversation_id, title, digest, early=early,
                      withdraw=withdraw)
        births[assignment_id] = birth

    def body():
        try:
            if birth.withdrawn:
                raise BirthWithdrawn('No dispatched call adopted this delegation; it never started')
            birth.result = run()
        except Exception as error:
            birth.failure = refusal(error)
            logger.warning('[Coordinator] Worker birth refused task=%s assignment=%s: %s',
                           task.get('id'), assignment_id, error)
        finally:
            with _LOCK:
                birth.finished = True
                withdraw_now = birth.withdrawn and birth.failure is None
            logger.info('[Coordinator] Worker birth %s task=%s assignment=%s in %.1fs%s',
                        'refused' if birth.failure is not None else 'completed',
                        task.get('id'), assignment_id, time.monotonic() - birth.started,
                        ' (started while its response streamed)' if birth.early else '')
            if withdraw_now:
                _withdraw(task, birth)
            # Waiters (cancel, finish, poll) see a withdrawn birth only once its Worker is cancelled.
            birth.done.set()

    spawn(body, 'coordinator-birth-' + assignment_id[:8])
    return birth


def _withdraw(task: dict, birth: Birth) -> None:
    try:
        birth.withdraw(birth)
        logger.info('[Coordinator] withdrew unadopted early Worker task=%s assignment=%s',
                    task.get('id'), birth.assignment_id)
    except Exception as error:
        logger.error('[Coordinator] could not withdraw unadopted early Worker task=%s '
                     'assignment=%s: %s', task.get('id'), birth.assignment_id, error, exc_info=True)


def withdraw_unadopted(task: dict) -> list[Birth]:
    """Withdraw this task's early births that no dispatched call adopted; return them.

    A born Worker is cancelled at once through its ``withdraw`` port; a birth
    in flight cancels its Worker as it finishes, or never starts one.
    """
    now, marked = [], []
    with _LOCK:
        for birth in (task.get(BIRTHS_KEY) or {}).values():
            if birth.early and not birth.adopted and not birth.withdrawn:
                birth.withdrawn = True
                marked.append(birth)
                if birth.finished and birth.failure is None:
                    now.append(birth)
    for birth in now:
        _withdraw(task, birth)
    return marked


def recorded(task: dict, ids: Iterable[str] | None = None) -> list[Birth]:
    """This task's births among ``ids`` (all of them for None)."""
    return _selected(task, ids)


def _selected(task: dict, ids: Iterable[str] | None) -> list[Birth]:
    with _LOCK:
        births = dict(task.get(BIRTHS_KEY) or {})
    return list(births.values()) if ids is None else [births[i] for i in ids if i in births]


def unfinished(task: dict, ids: Iterable[str] | None = None) -> list[Birth]:
    return [birth for birth in _selected(task, ids) if not birth.done.is_set()]


def failures(task: dict, ids: Iterable[str] | None = None) -> dict[str, Birth]:
    return {birth.assignment_id: birth for birth in _selected(task, ids)
            if birth.done.is_set() and birth.failure is not None}


def wait(task: dict, ids: Iterable[str] | None, *, deadline: float | None,
         interrupted: Callable[[], bool]) -> list[Birth]:
    """Wait for the selected births; return those still unfinished when it ends.

    It ends when every selected birth finished, at ``deadline`` (monotonic) or
    when ``interrupted()``, checked every ``WAIT_SLICE_SECONDS`` while time
    remains.
    """
    while True:
        pending = unfinished(task, ids)
        remaining = None if deadline is None else deadline - time.monotonic()
        if not pending or remaining is not None and remaining <= 0 or interrupted():
            return pending
        pending[0].done.wait(WAIT_SLICE_SECONDS if remaining is None else min(WAIT_SLICE_SECONDS, remaining))


def row(birth: Birth) -> dict:
    """A Worker row for a birth storage does not show: starting, or refused before birth."""
    result = {'assignment_id': birth.assignment_id, 'conversation_id': birth.conversation_id,
              'title': birth.title[:160],
              'status': 'failed_to_start' if birth.failure is not None else 'starting'}
    if birth.failure is not None:
        result['error'] = birth.failure
    return result


def rows(task: dict, *, exclude: set[str]) -> list[dict]:
    """Rows of this task's births that storage does not show yet, except ``exclude``."""
    return [row(birth) for birth in _selected(task, None)
            if birth.assignment_id not in exclude and not birth.withdrawn
            and not (birth.done.is_set() and birth.failure is None)]
