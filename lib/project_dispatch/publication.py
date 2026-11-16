"""Project publication adapter over the existing workspace delivery owner.

Entry points: workspace_origin pins private admission before registration;
publication_reason checks live repository authority and immutable delta scope;
delivery_view projects origin.delivery without inventing execution state;
delivery_final says whether that receipt is final for its work id.
Repository validation owns source/grant/result and exact attempt/task identity.
No scheduler, durable ledger, tool authority or execution is created here.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import errno
from pathlib import Path
import subprocess
import threading

from . import admission
from .repository import (
    DispatchContention, DispatchGrantRevoked, ProjectDispatchError, PublicationAwaitsReview,
    PublicationResultChanged)

from lib.log import get_logger

logger = get_logger(__name__)

# (owner, project root, task) -> (row fingerprint, reason, cause) of the exact
# workspace row whose publication this process last found refused by an outcome
# only its assignment can change: a revoked grant (never lifted) or a missing
# accepting review (lifted only by a review or publish, whose commit path then
# releases the hold and so rewrites the row). The delivery receiver revisits
# held rows on every pass; an unchanged row then reads nothing. A release
# attempt, a changed row or a recovery pass decides again, and an entry lives
# only while its row's latest verdict is such a refusal.
_ASSIGNMENT_DECIDED: dict[tuple, tuple[bytes, str, Exception]] = {}


def _row_key(row: Mapping) -> tuple:
    return (row.get('user_id'), str(row.get('project_root') or ''), str(row.get('task_id') or ''))


def _fingerprint(row: Mapping) -> bytes:
    import hashlib
    import json
    return hashlib.sha256(json.dumps([row.get(name) for name in (
        'id', 'state', 'managed', 'checkpoint_sha', 'workspace_path', 'origin')],
        sort_keys=True, separators=(',', ':'), default=str).encode()).digest()


def _identity_changed(row: Mapping, binding) -> bool:
    """The row no longer names its grant's owner, task and project (resolved now)."""
    return (not isinstance(binding, Mapping)
            or type(row.get('user_id')) is not int or row['user_id'] < 1
            or row['user_id'] != binding.get('owner')
            or not row.get('task_id')
            or not _same_project(row.get('project_root'), binding.get('projectPath')))


def awaits_assignment(row: Mapping) -> bool:
    """Whether this exact row's publication was last refused by its assignment alone.

    Its identity (owner, task, and the project its root resolves to now) is
    checked again: a moved alias changes the verdict without changing the row.
    """
    remembered = _ASSIGNMENT_DECIDED.get(_row_key(row))
    return (remembered is not None and remembered[0] == _fingerprint(row)
            and not _identity_changed(row, (row.get('origin') or {}).get(admission.KEY)))


def forget_assignment_decision(row: Mapping | None = None) -> None:
    """Decide ``row`` (every row when None) afresh: its assignment may have changed."""
    if row is None:
        _ASSIGNMENT_DECIDED.clear()
    else:
        _ASSIGNMENT_DECIDED.pop(_row_key(row), None)

# The fail-closed cause this process last logged, by workspace task. An entry
# lives only while that row's latest check fails closed, so it is bounded by the
# rows currently held for a hidden cause.
_LOGGED_HOLD_CAUSES: dict[str, tuple[str, str]] = {}
_LOGGED_HOLD_CAUSES_LOCK = threading.Lock()


def workspace_origin(task) -> dict:
    """Copy the admitted carrier into the FIRST registration, never backfill it."""
    config = (task or {}).get('config') or {}
    return {admission.KEY: deepcopy(config[admission.KEY])} if admission.KEY in config else {}


def _same_project(left, right) -> bool:
    return bool(left and right) and Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def _path_allowed(path: str, scopes, root: Path, *, project_grant: bool = False) -> bool:
    """The descriptor-I/O policy also fences external checkpoint mutations.

    A project grant writes with ordinary tools in its isolated worktree, so its
    checkpoint may change a nested source package named like a root runtime tree
    or a tracked tree named like build output (``docs/report/build/``).
    """
    from .scope import relative_path_allowed
    return relative_path_allowed(path, scopes, project_grant=project_grant)


def publication_reason(row: Mapping, receipt: Mapping) -> str:
    """Return missing authority as a reason for existing `blocked`, not a state.

    Absent carriers preserve generic task delivery. Present malformed carriers,
    repository/read failures and unverifiable checkpoint scope fail closed.
    Called again inside canonical repository admission after unlocked gates.
    """
    reason, hidden_cause = _publication_verdict(row, receipt)
    _note_hold_cause(row, receipt, reason, hidden_cause)
    return reason


def _publication_verdict(row: Mapping, receipt: Mapping) -> tuple[str, Exception | None]:
    """The reason, and the failure behind a fail-closed one that the reason hides."""
    origin = row.get('origin') or {}
    if admission.KEY not in origin:
        return '', None
    binding = origin[admission.KEY]
    fingerprint = _fingerprint(row)
    try:
        if _identity_changed(row, binding):
            return 'project_publication_identity_changed', None
        # Only the assignment read is remembered; identity is decided above every time.
        remembered = _ASSIGNMENT_DECIDED.get(_row_key(row))
        if remembered is not None and remembered[0] == fingerprint:
            return remembered[1], remembered[2]
        _ASSIGNMENT_DECIDED.pop(_row_key(row), None)
        assignment = admission.repository_for(binding).validate_publication(binding, row['task_id'])

        if (assignment.get('publication') or {}).get('checkpointSha') != row.get('checkpoint_sha'):
            return 'project_publication_checkpoint_changed', None
        if (not isinstance(assignment, Mapping)
                or assignment.get('id') != binding.get('assignmentId')
                or assignment['grant']['owner'] != binding['owner']
                or not _same_project(assignment['request']['projectPath'], row['project_root'])
                or assignment['request']['recipientId'] != binding.get('recipientId')
                or assignment['grant']['revision'] != binding.get('revision')
                or assignment['grant']['revoked']):
            return 'project_publication_identity_changed', None
    except PublicationResultChanged:
        # Actionable: the source Coordinator re-reviews the current result
        # revision for the same checkpoint; the receiver retries each tick.
        return 'project_publication_result_changed', None
    except (DispatchGrantRevoked, PublicationAwaitsReview) as decided:
        # A copy without traceback or context: the entry outlives this frame.
        cause = type(decided)(*decided.args)
        _ASSIGNMENT_DECIDED[_row_key(row)] = (fingerprint, 'project_publication_authority_missing', cause)
        return 'project_publication_authority_missing', cause
    except Exception as failure:
        logger.debug('Publication authority check failed for task %s: %s',
                     row.get('task_id'), type(failure).__name__)
        return _authority_failure_reason(failure), failure

    profile = assignment['request'].get('profile') or {}
    if profile.get('mode') not in ('files', 'project') or not isinstance(profile.get('writePaths'), list):
        return 'project_publication_scope_missing', None
    # Source HEAD omits borrowed WIP. Only sourceSnapshot isolates task changes.
    baseline, checkpoint = origin.get('sourceSnapshot'), row.get('checkpoint_sha')
    if not baseline or not checkpoint:
        return 'project_publication_checkpoint_unavailable', None
    pairs = [(baseline, checkpoint)]
    repair = receipt.get('repair') or {}
    if repair:
        if (repair.get('originalBaselineSha') != baseline
                or repair.get('registeredCheckpointSha') != checkpoint
                or not repair.get('baselineSha') or not repair.get('checkpointSha')):
            return 'project_publication_checkpoint_unavailable', None
        pairs.append((repair['baselineSha'], repair['checkpointSha']))
    try:
        from lib import integration_control as ic
        root = Path(row['project_root']).resolve()
        for before, after in pairs:
            # Disable rename detection so BOTH old/deleted and new paths fence.
            paths = ic._git(root, ['diff', '--no-renames', '--name-only', '-z',
                                   before, after], check=True).stdout.split('\0')
            refused = next((path for path in paths if path and not _path_allowed(
                path, profile['writePaths'], root, project_grant=profile['mode'] == 'project')), None)
            if refused is not None:
                return ('project_publication_write_scope_exceeded',
                        PermissionError(f'refused path {refused!r}'))
    except Exception as failure:
        logger.debug('Publication checkpoint scope check failed for task %s: %s',
                     row.get('task_id'), type(failure).__name__)
        return _checkpoint_failure_reason(failure), failure
    return '', None


# A check the receiver repeats on every pass: an authority or Git read that did
# not answer now. delivery_view shows its hold as delivery in progress.
CHECK_UNAVAILABLE = 'project_publication_check_unavailable'
# A Tofu defect stopped the check; the reason appends the exception's type.
CHECK_DEFECT = 'project_publication_check_defect'
# Storage answers that refuse the authority itself rather than fail to read it.
_AUTHORITY_REFUSAL_CODES = frozenset({'database_not_found', 'database_forbidden'})
# The spawn failures delivery retries (delivery._transient_git_spawn_error).
_TRANSIENT_SPAWN_ERRNOS = frozenset({errno.EAGAIN, errno.EINTR, errno.ENOMEM})


def _defect(failure: Exception) -> str:
    code = getattr(failure, 'code', None)
    named = type(failure).__name__ + (f' {code}' if isinstance(code, str) and code else '')
    return f'{CHECK_DEFECT}: {named}'


def _authority_failure_reason(failure: Exception) -> str:
    """Name what kept the live authority check from deciding.

    The repository's refusals, and storage answering that the assignment or
    owner is not there, are authority verdicts. Contention and a busy,
    unavailable or over-deadline authority are reads that the receiver's next
    pass repeats. Anything else is a defect, never missing authority.
    """
    from lib.agent_core.storage_errors import StorageError, is_transient_storage_error
    if isinstance(failure, DispatchContention):
        return CHECK_UNAVAILABLE
    if isinstance(failure, (PermissionError, ProjectDispatchError)):
        return 'project_publication_authority_missing'
    if is_transient_storage_error(failure):
        return CHECK_UNAVAILABLE
    if isinstance(failure, StorageError) and failure.code in _AUTHORITY_REFUSAL_CODES:
        return 'project_publication_authority_missing'
    return _defect(failure)


def _checkpoint_failure_reason(failure: Exception) -> str:
    """Name what kept the checkpoint diff from deciding the write scope.

    ``integration_control._git`` chains its timeout or spawn error to the
    ``IntegrationError`` it raises: the next pass repeats that read. Git's own
    refusal, such as a missing object, leaves the checkpoint unavailable.
    """
    from lib import integration_control as ic
    cause = failure.__cause__
    if isinstance(cause, subprocess.TimeoutExpired) or (
            isinstance(cause, OSError) and cause.errno in _TRANSIENT_SPAWN_ERRNOS):
        return CHECK_UNAVAILABLE
    # IntegrationError is an alias of RuntimeError; its subclasses
    # (RecursionError, NotImplementedError) are defects, not Git answers.
    if type(failure) is ic.IntegrationError:
        return 'project_publication_checkpoint_unavailable'
    return _defect(failure)


class PublicationRefused(ProjectDispatchError):
    """The delivery cannot publish the requested checkpoint; nothing was recorded."""

    execution_started = False

    def __init__(self, reason: str, recovery: str):
        super().__init__('Publication cannot start: ' + reason)
        self.recovery = recovery


# A completed receipt in these states is (or will be) acted on by the receiver.
_PUBLISHABLE_STATES = frozenset({'pending', 'blocked', 'delivered'})
_UNSETTLED_RECOVERY = (
    'The completed attempt never settled this delivery. Follow the Worker up '
    '(control_conversation action=follow_up) or regenerate its Turn, so a new attempt reopens '
    'the delivery in the same checkout, or delegate a carry assignment.')


def require_publishable(workspace: Mapping, executor_task_id: str) -> None:
    """Refuse, before any review or pin is recorded, an accept that cannot publish.

    The registered receipt must record the assignment's completed execution
    attempt (its generation, when the receipt names one) in a state the
    receiver acts on. The pinned checkpoint itself is the registered row's,
    which the caller checks and the receiver recaptures to if the receipt's
    differs.
    """
    receipt = (workspace.get('origin') or {}).get('delivery') or {}
    state, reason = str(receipt.get('state') or 'absent'), str(receipt.get('reason') or '')
    described = state + (f' ({reason})' if reason else '')
    generation = receipt.get('executorTaskId')
    if receipt and isinstance(generation, str) and generation and generation != executor_task_id:
        raise PublicationRefused(
            f'the delivery is {described} for attempt {generation}, not for the completed '
            f'attempt {executor_task_id}', _UNSETTLED_RECOVERY)
    if receipt.get('taskOutcome') != 'completed':
        if state == 'working':
            raise PublicationRefused(
                'the delivery has not settled the completed result yet (working)',
                'Retry once poll on its assignment_id reports its checkpoint.')
        raise PublicationRefused(
            f'the delivery is {described}: it records an attempt that did not complete',
            _UNSETTLED_RECOVERY)
    retry = receipt.get('retry') or {}
    if state not in _PUBLISHABLE_STATES and not (
            state == 'failed' and retry.get('condition') == 'validation_inputs_changed'):
        raise PublicationRefused(f'the delivery is {described}',
                                 'Report the delivery failure; this checkpoint cannot publish.')


def _note_hold_cause(row: Mapping, receipt: Mapping, reason: str, cause: Exception | None) -> None:
    """Name a fail-closed hold's hidden cause once per process and whenever it changes.

    The receiver rechecks every held receipt on each pass, so an unchanged cause
    logs nothing. A hold written by an earlier process, or a new cause under the
    same reason, still logs once. Delivered receipts only gate cleanup and log nothing.
    """
    task_id = str(row.get('task_id') or '')
    with _LOGGED_HOLD_CAUSES_LOCK:
        if cause is None or receipt.get('state') == 'delivered':
            _LOGGED_HOLD_CAUSES.pop(task_id, None)
            return
        logged = (reason, f'{type(cause).__name__}: {str(cause)[:500]}')
        if _LOGGED_HOLD_CAUSES.get(task_id) == logged and receipt.get('reason') == reason:
            return
        _LOGGED_HOLD_CAUSES[task_id] = logged
    if reason.startswith(CHECK_DEFECT):
        logger.error('[ProjectPublication] %s task=%s: %s', reason, task_id, logged[1], exc_info=cause)
    else:
        logger.warning('[ProjectPublication] %s task=%s: %s', reason, task_id, logged[1])


def _uncommitted_delivery(acknowledgement) -> tuple[str, str]:
    """Project a working-tree publication that no commit on the branch holds yet."""
    if not acknowledgement:
        return 'pending', 'delivered_uncommitted: published to the working tree; its commit is pending'
    detail = acknowledgement.get('reason') or (
        'a whole-workspace checkpoint does not commit one delta' if 'schema' in acknowledgement else '')
    return 'blocked', ('delivered_uncommitted: published to the working tree but never committed ('
                       + str(acknowledgement.get('status') or 'unknown') + (f': {detail}' if detail else '')
                       + '); its delta must be committed on the branch')


def _associated_receipt(assignment: Mapping, attempt: Mapping | None, workspace_get):
    """``(task_id, row, receipt)`` of the exact associated workspace, or ``None``.

    Raises only what the workspace read raises; callers decide what unknown means.
    """
    execution = assignment.get('execution') or {}
    if not isinstance(attempt, Mapping) or not attempt.get('taskId'):
        return None
    request, grant = assignment['request'], assignment['grant']
    if (attempt.get('attemptId') != execution.get('attemptId')
            or attempt.get('turnId') != execution.get('turnId')
            or attempt.get('conversationId') != request['recipientId']):
        return None
    # The checkout is registered under the association's work id; each physical
    # attempt of the assignment runs as its own task inside it.
    task_id = execution.get('taskId') or attempt['taskId']
    project_root = str(Path(request['projectPath']).expanduser().resolve())
    row = workspace_get(project_root, task_id, user_id=grant['owner'])
    if (not isinstance(row, Mapping) or row.get('task_id') != task_id
            or type(row.get('user_id')) is not int or row['user_id'] < 1
            or row['user_id'] != grant['owner']
            or not _same_project(row.get('project_root'), request['projectPath'])):
        return None
    origin = row.get('origin') or {}
    binding = origin.get(admission.KEY) or {}
    if (binding.get('assignmentId') != assignment['id']
            or binding.get('owner') != grant['owner']
            or binding.get('recipientId') != request['recipientId']
            or not _same_project(binding.get('projectPath'), request['projectPath'])):
        return None
    receipt = origin.get('delivery') or {}
    return (task_id, row, receipt) if receipt else None


def delivery_final(assignment: Mapping, attempt: Mapping | None, workspace_get) -> bool:
    """Whether the association's receipt is ``delivered``, final for its work id.

    A delivered receipt (``no_changes`` too) never reopens for a later attempt,
    and its checkout may already be reclaimed. Unknown evidence is not final.
    """
    found = _associated_receipt(assignment, attempt, workspace_get)
    return found is not None and found[2].get('state') == 'delivered'


def delivery_view(assignment: Mapping, attempt: Mapping | None, workspace_get) -> dict | None:
    """Read the exact associated workspace receipt, never infer from candidate.

    workspace_get uses the existing (project_root, task_id, user_id=owner) port.
    Absence or unavailable storage is unknown delivery, not canonical success.
    Status ``delivered`` means the delta is committed on the branch
    (``delivery.canonical_delivery``): a working-tree publication without that
    evidence projects as pending, or blocked once its commit was refused.
    This is diagnostic projection; publication_reason remains the live fence.
    """
    try:
        found = _associated_receipt(assignment, attempt, workspace_get)
        if found is None:
            return None
        task_id, row, receipt = found
        origin = row.get('origin') or {}
        status, reason = str(receipt.get('state') or 'unknown'), str(receipt.get('reason') or '')
        if status == 'delivered':
            from lib.project_mod.delivery import canonical_delivery
            if not canonical_delivery(origin):
                status, reason = _uncommitted_delivery(origin.get('deliveryCheckpoint'))
        elif status == 'blocked' and reason == CHECK_UNAVAILABLE:
            # The receiver publishes once the read answers; nothing to act on.
            status = 'pending'
        return {'taskId': task_id, 'status': status[:64], 'reason': reason[:1024],
                'checkpointSha': str(receipt.get('checkpointSha') or row.get('checkpoint_sha') or '')[:256],
                'targetSha': str(receipt.get('targetSha') or '')[:256]}
    except Exception as diagnostic_error:
        logger.debug('delivery_view failure handled: %s', type(diagnostic_error).__name__)
        return None
