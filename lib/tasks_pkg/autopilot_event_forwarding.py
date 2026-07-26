"""Autopilot VU event-forwarding cluster.

Extracted from ``lib.tasks_pkg.autopilot`` under pt_00459503 slice 5 —
the epic-named "event-forwarding" module.  Kept as a LEAF (imports only
the shared event builder + logger, plus a lazy ``manager.append_event``
inside the forwarder to avoid a cycle with the task manager) so
``autopilot.py`` can re-export these symbols BY IDENTITY without any
back-import from this file.

Contents:
  * ``_VU_FORWARD_TYPES`` — the frozenset of sub-task event types that
    get wrapped and forwarded onto the parent task's SSE stream.
  * ``_VUEventForwarder`` — the ``list`` subclass we swap into
    ``sub_task['events']`` so every ``append_event`` on the sub-task
    also lands a wrapped ``autopilot_vu_event`` on the parent's stream
    (routed to the synthetic-user bubble by ``vuMsgId``).
  * ``_emit_vu_setup_phase`` — the pre-stream "working" phase emitter
    used to attribute the 2.5–26.7 s silent warmup window between
    ``autopilot_vu_start`` and the sub-task's first orchestrator phase.

Byte-identical bodies to the pre-extraction inline forms in
``autopilot.py`` (verified by ``tests/test_autopilot_event_forwarding_wire_parity.py``).
"""

from __future__ import annotations

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger

logger = get_logger(__name__)


_VU_FORWARD_TYPES = frozenset({
    'delta', 'phase',
    'tool_start', 'tool_result', 'tool_progress', 'tool_complete',
    'tool_compacted',
    'stdin_request', 'stdin_resolved',
    'write_approval_request',
    'human_guidance_request', 'human_guidance_response',
})


class _VUEventForwarder(list):
    """List subclass that forwards the VU sub-task's events to the parent.

    The orchestrator drives all SSE updates by calling
    ``manager.append_event(task, ev)`` which does
    ``task['events'].append(ev)`` under the task's events_lock.  By
    swapping ``sub_task['events']`` with this subclass we get a hook on
    every event the VU sub-task emits, without monkey-patching
    ``append_event`` globally.

    For each VU event we still append it to the underlying list (so the
    sub-task's own SSE stream stays intact for any reader that ever
    connects to it), and additionally forward two flavours of derived
    events onto the PARENT task's stream:

      1. ``autopilot_vu_event`` — wraps the original VU sub-task event
         (delta / tool_start / tool_result / tool_progress / tool_complete /
         tool_compacted / stdin_* / write_approval_request /
         human_guidance_*) so the frontend can render the VU's reply +
         tool calls into the synthetic-user bubble *as they happen*,
         instead of materializing the whole bubble after the VU
         finishes.  The wrapper carries ``vuMsgId`` so the frontend can
         target the right message.

    The synthetic-user bubble itself is created eagerly by the
    ``autopilot_vu_start`` event (emitted from ``maybe_run_autopilot``
    BEFORE the VU sub-task runs), so the user sees an "Autopilot ·
    composing…" bubble in the USER lane the moment the worker stops —
    NOT a phase chip glued to the worker bubble.  All VU thinking, tool
    calls, and reply text then stream into that bubble via the wrapped
    events above.
    """

    def __init__(self, parent_task, vu_msg_id):
        super().__init__()
        self._parent = parent_task
        self._vu_msg_id = vu_msg_id

    def append(self, ev):
        super().append(ev)
        try:
            self._forward_to_parent(ev)
        except Exception as e:
            logger.debug('[Autopilot] event forward failed (non-fatal): %s', e)

    def _forward_to_parent(self, ev):
        from lib.tasks_pkg.manager import append_event as _ap_event
        et = (ev or {}).get('type')

        # Forward the inner event verbatim, wrapped so the frontend
        # routes it into the VU bubble (by vuMsgId) instead of the
        # parent's worker bubble.  We re-emit the parent-stream phase
        # chip below as well; the two are not mutually exclusive (one
        # paints the VU bubble, the other annotates the parent's chip).
        if et in _VU_FORWARD_TYPES:
            _ap_event(self._parent, build_event(
                EventType.AUTOPILOT_VU_EVENT,
                vuMsgId=self._vu_msg_id,
                inner=ev,
            ))


def _emit_vu_setup_phase(task: dict, vu_msg_id: str | None, detail: str) -> None:
    """Surface a pre-stream Autopilot setup step in the VU bubble.

    Diagnosis (task_events probe, debug/autopilot_warmup_window_probe.py):
    between ``autopilot_vu_start`` and the VU sub-task's first orchestrator
    phase (``llm_thinking`` / ``waiting_model``) there is a genuinely SILENT
    window — measured 2.5–26.7s across 12 real runs — during which
    ``run_virtual_user`` resolves the objective (DB read), assembles the
    message list and builds the sub-task. Nothing was emitted, so the bubble
    sat on the bare "Autopilot…" placeholder with no attribution of what was
    blocking.

    This emits a ``working`` phase wrapped as ``autopilot_vu_event`` — the
    SAME envelope ``_VUEventForwarder`` uses for the sub-task's own events —
    so it routes into the VU bubble by ``vuMsgId`` and renders through the
    existing ``updateStreamingUI`` ``working`` branch (``phase.detail`` shown
    verbatim). No new event type; the frontend already handles it.

    Emitted directly on the PARENT task because the sub-task (and its
    forwarding event list) does not exist yet at these steps.
    """
    if not vu_msg_id:
        return
    try:
        from lib.tasks_pkg.manager import append_event
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_EVENT,
            vuMsgId=vu_msg_id,
            inner={'type': 'phase', 'phase': 'working', 'detail': detail},
        ))
    except Exception as e:
        logger.debug('[Autopilot] vu setup-phase emit failed (non-fatal): %s', e)
