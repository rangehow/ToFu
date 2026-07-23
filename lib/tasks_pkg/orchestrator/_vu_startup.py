"""orchestrator/_vu_startup.py — VU startup + external-edit probe (run_task slice).

**Extraction context** (board epic ``pt_03f4cdf1``, slice 2, FIRST source
extraction from the 1813-line ``run_task``):

Two self-contained helpers that ``run_task`` previously carried as nested
closures inside its main ``try:`` block. Both were pure-ish (no dependency
on any local variable OTHER than what they explicitly closed over) so they
translate 1:1 to module-level functions taking their captures as arguments.

  * :func:`_vu_phase` — emit a PHASE event during the VU sub-task's
    startup window (silent no-op on ordinary worker/endpoint turns).
  * :func:`_probe_external_edits` — the daemon-thread target that runs
    the FUSE external-edit probe after ``ensure_project_state``; on
    detection of committed off-Tofu edits, appends a
    ``PROJECT_EXTERNAL_EDIT`` event so the UI can prompt the user.

Kept OUT of ``_finalize`` (post-loop helpers) and ``_turn`` (per-round
primitives) because they run ONCE at task startup and are neither
finalization nor per-turn.

**Strangler-fig discipline** (matches routes/chat_helpers.py's pattern):
these functions are DEFINED here; ``_run.py`` imports them and calls them
at the same source sites where the closures previously lived. There is now
exactly ONE definition of each. Wire-parity guarded by
``tests/test_lib_orchestrator_wire_parity.py``.
"""

from __future__ import annotations

import threading
from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _vu_phase(task: dict[str, Any], detail: str, *, vu_startup: bool) -> None:
    """Emit a PHASE event during the VU sub-task's startup window.

    The VU sub-task's ``events`` is a ``_VUEventForwarder``, so any PHASE
    emitted here auto-forwards into the synthetic-user bubble. The
    pre-stream prep window (tool assembly → tool-history rebuild → system-
    context injection → FUSE memory/project prefetch) is otherwise SILENT
    for up to tens of seconds on a large conversation (measured 2.9–4.7s
    typical, ~26s on a 3000-event conv), leaving the bubble on a vague
    placeholder. Naming each real sub-step keeps the display honest.

    Gated on ``vu_startup`` so the ordinary worker/endpoint startup path
    stays byte-identical (no new events).

    Args:
        task: The live task dict — passed to ``append_event`` as-is.
        detail: Short user-visible description of the current sub-step.
        vu_startup: True IFF this is a VU sub-task's startup window
            (``bool(task.get('_vu_subtask'))`` at the call site).
    """
    if not vu_startup:
        return
    try:
        append_event(task, build_event(
            EventType.PHASE, phase='working', detail=detail))
    except Exception as _e:
        _tid = (task.get('id') or '')[:8]
        logger.debug('[Task %s] vu startup phase emit failed: %s', _tid, _e)


def _probe_external_edits(task: dict[str, Any], project_path: str) -> None:
    """Daemon-thread target that runs the FUSE external-edit probe.

    Runs SILENTLY: no phase event, no UI status — the LLM response starts
    streaming immediately. Cost is bounded by the size of the tracked-
    files set (files the assistant has touched this session), not the
    worktree, so this is cheap even on slow filesystems.

    Correctness guard: if the round has already started mutating files by
    the time the probe finishes, we skip the synthetic external-edit
    snapshot to avoid misattribution. The next round's probe catches the
    drift cleanly on top of a stable timeline.

    Args:
        task: The live task dict — read for ``modifiedFileList`` /
            ``modifiedFiles`` / ``id``, and mutated via ``append_event``
            on detection.
        project_path: The active project root the probe should scan.
    """
    try:
        if task.get('modifiedFileList') or task.get('modifiedFiles'):
            logger.debug('[Task:%s] skipping external-edit probe '
                         '— round already mutated files',
                         task['id'][:8])
            return
        # Pass the set of known Tofu task ids so the probe can tell a
        # CONCURRENT conversation's write on the shared project root
        # (last_writer_task_id ∈ known) from a genuine out-of-band IDE
        # edit — the former must NOT surface as an "edited outside
        # Tofu" toast.
        try:
            from lib.tasks_pkg.manager import (
                tasks as _known_tasks,
                tasks_lock as _known_tasks_lock,
            )
            with _known_tasks_lock:
                _known_task_ids = set(_known_tasks.keys())
        except Exception as _kte:
            logger.debug('[Task:%s] known-task-id snapshot failed: %s',
                         task['id'][:8], _kte)
            _known_task_ids = None
        from lib import file_history as fh
        _ext = fh.detect_external_edits(
            project_path, known_task_ids=_known_task_ids)
        if _ext.get('siblingFiles'):
            logger.info('[Task:%s] external-edit probe attributed %d '
                        'drifted file(s) to concurrent Tofu task(s) — '
                        'suppressed IDE toast', task['id'][:8],
                        len(_ext.get('siblingFiles', [])))
        if (task.get('modifiedFileList') or task.get('modifiedFiles')):
            logger.debug('[Task:%s] external-edit probe completed after '
                         'round started mutating files — not emitting '
                         'SSE event (attribution ambiguous)',
                         task['id'][:8])
            return
        if _ext.get('committed'):
            append_event(task, build_event(
                EventType.PROJECT_EXTERNAL_EDIT,
                files=_ext.get('files', []),
                sha=_ext.get('snapshotId'),
            ))
            logger.info('[Task:%s] captured %d external edit(s) snap=%s',
                        task['id'][:8], len(_ext.get('files', [])),
                        (_ext.get('snapshotId') or '')[:8])
    except Exception as e:
        logger.warning('[Task:%s] external-edit detection failed: %s',
                       task['id'][:8], e)


def start_external_edit_probe(task: dict[str, Any], project_path: str) -> None:
    """Spawn ``_probe_external_edits`` in a daemon thread.

    Sole responsibility: wrap the ``threading.Thread`` boilerplate so the
    call site in ``_run.py`` stays a single line. The probe itself is
    the pure-ish target function above — spawning it separately keeps it
    directly testable without stubbing out threading.
    """
    threading.Thread(
        target=_probe_external_edits,
        args=(task, project_path),
        name=f'ext-edit-probe-{task["id"][:8]}',
        daemon=True,
    ).start()


__all__ = ['_vu_phase', '_probe_external_edits', 'start_external_edit_probe']
