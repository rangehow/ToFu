"""Exact manual Detect relay; the scheduler owner reconstructs durable authority."""
import os
import time

from lib.conversation_sync.execution_identity import ManualDetectAdmission, read_durable_detect
from lib.identity import PERSONAL_USER_ID


def run_on_primary(admission, schedule_id, *, caller_shutdown=None):
    """Route a live shard invocation once, without serializing authority."""
    from lib.tasks_pkg.command_control_runtime import call_primary_manual_detect
    from runtime_guards import load_deployment_configuration

    # Match the only managed topology that provisions execution shards. The
    # receiving scheduler independently checks its actual composed principal.
    if (not isinstance(admission, ManualDetectAdmission)
            or load_deployment_configuration().mode != 'personal'
            or admission.principal.owner_user_id != PERSONAL_USER_ID
            or admission.principal.tenant_id is not None):
        return None, 'Task not found'
    try:
        admission.verify_current(require_live_worker=True)
    except (PermissionError, RuntimeError, ValueError):
        return None, 'Task not found'
    answer = call_primary_manual_detect(os.environ.get('TOFU_PROJECT_PATH'), admission.task_id,
        user_id=admission.principal.require_owner(), schedule_id=schedule_id,
        caller_shutdown=caller_shutdown,
        conversation_id=admission.conversation_id, turn_id=admission.turn_id,
        attempt_id=admission.attempt_id)
    return answer['success'], answer['result']


def apply_manual_detect(task_id, *, user_id, deadline_unix_ms, schedule_id,
                        conversation_id, turn_id, attempt_id, stopping=None, relay_closed=None):
    """Receive on the primary only; no lookup of caller-asserted scopes."""
    from lib.scheduler.manager import get_scheduler
    from lib.tasks_pkg.generated_command_control import MANUAL_DETECT_RESULT_MAX

    refused = {'success': None, 'result': 'Task not found'}
    try:
        principal = read_durable_detect(user_id, conversation_id, turn_id, attempt_id,
                                        running_task_id=task_id, require_live_worker=True)
        admission = ManualDetectAdmission(task_id, principal, conversation_id, turn_id, attempt_id)
        cancelled = _DetectCancellation(admission, deadline_unix_ms, stopping, relay_closed)
        if cancelled.is_set():
            return refused
    except (PermissionError, RuntimeError, ValueError):
        return refused
    success, result = get_scheduler().run_task_now(
        schedule_id, user_id=user_id, principal=principal, manual_detect_admission=admission,
        detect_only=True, detect_cancel_event=cancelled)
    if cancelled.is_set():
        # Withdrawal can race an already-entered commit. Do not tell the
        # caller that those effects certainly failed or were rolled back.
        raise RuntimeError('Detect relay outcome is unconfirmed after withdrawal')
    return {'success': success, 'result': result[:MANUAL_DETECT_RESULT_MAX]}


class _DetectCancellation:
    def __init__(self, admission, deadline_unix_ms, stopping, relay_closed):
        self.admission = admission
        self.deadline_unix_ms = deadline_unix_ms
        self.stopping = stopping
        self.relay_closed = relay_closed

    def dispatch_cancelled(self):
        """Local withdrawal check for provider dispatch where storage cannot be read."""
        return ((self.stopping is not None and self.stopping.is_set())
                or (self.relay_closed is not None and self.relay_closed.is_set())
                or self.deadline_unix_ms <= time.time_ns() // 1_000_000)

    def is_set(self):
        if self.dispatch_cancelled():
            return True
        try:
            self.admission.verify_current(require_live_worker=True)
            return self.dispatch_cancelled()
        except (PermissionError, RuntimeError, ValueError):
            return True
