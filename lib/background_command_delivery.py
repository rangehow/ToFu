"""Bounded retries for publishing an already-finished command's result.

The caller freezes payload and identity once; queue.enqueue's durable receipt
makes a lost-ACK retry safe. completion_delivery_config projects recorded source
roots for a fresh task; it never transfers a previous workspace registration.
This does not claim recovery before first commit. owner_absence classifies a
refusal: only an owner-scoped read confirming the conversation absent makes it
CompletionOwnerAbsent; the command registry decides whether that is final.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.project_dispatch.admission import KEY as DISPATCH_ADMISSION_KEY

logger = get_logger(__name__)

@dataclass(frozen=True, slots=True)
class _ClosedSettlement:
    """One closed Worker's settlement as this process last decided it, with its inputs."""
    queue_ids: frozenset[str]
    assignment_id: str
    # None when the cause (revocation, review) can never change again.
    assignment_version: int | None
    turn_id: str
    turn_state: tuple
    block_id: str | None
    # A bound batch whose inputs are all named above may skip the whole decision.
    reusable: bool


# (owner, conversation) -> the closed settlement whose evidence this process saw
# attached (or which had none to attach), or a settled Turn awaiting its resume.
# A retirement removes the receipts with the block, so that batch ends; the memo
# serves the batches whose receipts stay listed and are revisited every tick (a
# Turn awaiting checkpoint_resume, or an authority that predates retirement).
# While the same
# receipts, assignment version and Turn status/revision stand, neither the
# verdict nor the attachment can differ, so the tick reads only those versions;
# a Turn revision alone still proves the block attached without rereading a
# multi-megabyte projection. drain_idle_background_batches drops conversations
# that no longer hold receipts.
_CLOSED_SETTLEMENTS: dict[tuple[int, str], _ClosedSettlement] = {}
_FINAL_CAUSES = frozenset({'dispatch_grant_revoked', 'execution_reviewed'})


def retain_closed_settlements(conversations) -> None:
    """Keep remembered settlements only for ``conversations`` (owner, id) still holding receipts."""
    for remembered in [key for key in _CLOSED_SETTLEMENTS if key not in conversations]:
        _CLOSED_SETTLEMENTS.pop(remembered, None)


def _turn_state(turn) -> tuple:
    return (turn.get('status'), turn.get('projectionRevision'))


class CompletionOwnerAbsent(Exception):
    """queue.enqueue refused a receipt because its owner conversation is absent.

    Both storage backends commit a receipt only into an active conversation
    owned by the completion's user, so retrying the same request cannot succeed
    unless that conversation is created.
    """


def owner_absence(error: BaseException, conv_id: str, *, user_id: int) -> str | None:
    """Return why ``error`` proves the owner absent, or None while it is retryable.

    Only a ``database_not_found`` refusal confirmed by the owner-scoped
    ``conversation.get`` returning null counts. A present owner means the
    refusal concerned something else; an unanswered read is not absence.
    """
    if not isinstance(error, StorageError) or error.code != 'database_not_found':
        return None
    from lib.conversations.repository import get_conversation
    try:
        owner = get_conversation(conv_id, user_id=user_id, include_messages=False)
    except Exception as probe_error:
        logger.debug('[BackgroundDelivery] owner probe for %s failed; the refusal stays '
                     'retryable: %s: %s', conv_id, type(probe_error).__name__, probe_error)
        return None
    if owner is not None:
        return None
    return (f'owner conversation {conv_id!r} is absent for user {user_id} '
            f'({error.code}: {error.message})')


def publish_completion(enqueue: Callable[..., dict[str, Any]], *args: Any,
                       **kwargs: Any) -> dict[str, Any]:
    """Retry transient storage failures three times using identical arguments."""
    for attempt in range(3):
        try:
            return enqueue(*args, **kwargs)
        except StorageError as error:
            transient = error.retryable or error.code in {
                'database_busy', 'database_unavailable', 'database_timeout'}
            if not transient or attempt == 2:
                raise
            time.sleep(min(1.0, max(0.1 * (2 ** attempt),
                                   (error.retry_after_ms or 0) / 1000)))
    raise AssertionError('unreachable completion retry state')


# A completion starts a fresh delivery turn. These fields resume the old
# attempt and can hold multi-MiB buffers; they are not execution preferences.
_COMPLETION_ATTEMPT_FIELDS = frozenset({
    'assistantMsgId', 'excludeLast', 'resumePrefill', 'contentPrefix',
})


def completion_delivery_config(config: dict | None) -> dict:
    """Keep execution policy/preferences without copying a settled attempt."""
    original = config or {}
    cleaned = {key: value for key, value in original.items()
               if (key == DISPATCH_ADMISSION_KEY
                   or not str(key).startswith(('_', 'checkpoint')))
               and key not in _COMPLETION_ATTEMPT_FIELDS
               and key not in {'canonicalProjectPath', 'canonicalProjectPaths',
                               'workspaceRuntimeRoots'}}
    # A new task cannot inherit the settled task's registered checkout. Use
    # only the canonical roots recorded by admission, never inspect/adopt peers.
    mappings = []
    primary, canonical = original.get('projectPath'), original.get('canonicalProjectPath')
    if isinstance(primary, str) and isinstance(canonical, str) and canonical:
        mappings.append((primary, canonical))
    extras, canonical_extras = original.get('projectPaths'), original.get('canonicalProjectPaths')
    if (isinstance(extras, list) and isinstance(canonical_extras, list)
            and len(extras) == len(canonical_extras)):
        mappings.extend((source, target) for source, target in zip(extras, canonical_extras)
                        if isinstance(source, str) and isinstance(target, str) and target)
    mappings.sort(key=lambda pair: -len(pair[0]))

    def canonical_path(value):
        if not isinstance(value, str) or not Path(value).is_absolute():
            return value
        for source, target in mappings:
            if not Path(source).is_absolute() or not Path(target).is_absolute():
                continue
            try:
                suffix = Path(value).relative_to(source)
            except ValueError:
                continue
            return str(Path(target) / suffix)
        return value

    if 'projectPath' in cleaned:
        cleaned['projectPath'] = canonical_path(cleaned['projectPath'])
    for key in ('projectPaths', 'readOnlyPaths'):
        if isinstance(cleaned.get(key), list):
            cleaned[key] = [canonical_path(path) for path in cleaned[key]]
    return cleaned


def _settled(turn, attempt) -> bool:
    return (isinstance(turn, dict) and isinstance(attempt, dict)
            and isinstance(turn.get('status'), str)
            and turn['status'] not in {'pending', 'running'}
            and attempt.get('status') not in {'pending', 'running'})


def _resume_operations(turn) -> set[str]:
    """The operations of ``restart_resume.RESUME_OPERATIONS`` the settled Turn offers."""
    from lib.conversations.restart_resume import RESUME_OPERATIONS
    options = (turn.get('settlement') or {}).get('resumeOptions') or []
    return {option['operation'] for option in options
            if isinstance(option, dict) and option.get('operation') in RESUME_OPERATIONS}


def settle_closed_dispatch_completion(config, receipts, *, conv_id, user_id):
    """Attach a closed Worker's late results without starting another model turn.

    The completed/reviewed or revoked execution remains the evidence owner, and
    so does a settled Turn that offers no resume operation at all (a failed start
    leaves only Regenerate). The round-0 evidence block and the retirement of its
    queue rows commit together (``turn.projection.update`` with
    ``background_command_retirement``), so the receipts are never listed for a
    later Turn; no process-local inbox fence stands in for that.
    Results of a turn that ran without the binding in a Worker conversation are
    governed by that conversation's assignment, so they settle the same way.
    A settled Turn that offers only ``checkpoint_resume`` is not closed: restart
    or stall recovery (or its owner) resumes it, and that attempt's prelude
    delivers the receipts. A bound batch issues only Continue, so it waits.
    """
    from lib.project_dispatch.repository import DispatchGrantRevoked, ProjectDispatchRepository
    binding = (config or {}).get(DISPATCH_ADMISSION_KEY)
    unbound = not isinstance(binding, dict)
    if unbound:
        from lib.project_dispatch.recovery import worker_conversation_admission
        binding = worker_conversation_admission(
            conv_id, user_id=user_id, repository_factory=ProjectDispatchRepository)
        if binding is None:
            return False
    repository = ProjectDispatchRepository(binding['owner'], binding['projectPath'])
    remembered_key = (user_id, conv_id)
    queue_ids = frozenset(str(row.get('queueId')) for row in receipts)
    remembered = _CLOSED_SETTLEMENTS.get(remembered_key)
    if (remembered is not None and remembered.reusable and not unbound
            and remembered.queue_ids == queue_ids
            and remembered.assignment_id == binding['assignmentId']
            and (remembered.assignment_version is None or remembered.assignment_version
                 == repository.assignment_version(binding['assignmentId']))):
        latest = repository.turn_get(conv_id, remembered.turn_id, user_id=user_id,
                                     include_projection=False)
        if isinstance(latest, dict) and _turn_state(latest) == remembered.turn_state:
            return True
    # Read before deciding: a write after it only makes the remembered version stale.
    version = None if unbound else repository.assignment_version(binding['assignmentId'])
    admission = repository.admission(binding['assignmentId'])
    if any(admission[key] != binding.get(key)
           for key in ('version', 'owner', 'tenant', 'projectPath', 'assignmentId', 'recipientId')):
        return False
    assignment = repository.read(binding['assignmentId'])
    execution = assignment.get('execution')
    if execution is None and not unbound:
        return False
    revoked = False
    try:
        repository.validate_own(binding)
    except DispatchGrantRevoked:
        revoked = True
    if execution is None:
        return revoked
    from lib.turn_lifecycle import LifecycleConflict, update_turn_projection
    from lib.background_command_projection import background_command_injection_record
    # Every decision before the attachment reads Turn metadata only; a long
    # Worker Turn's projection is read once, for the attachment CAS below.
    turn = repository.turn_get(
        conv_id, execution['turnId'], user_id=user_id, include_projection=False)
    attempt = repository.get_attempt(execution['attemptId'])

    def remember(settled_turn, block_id):
        final = cause in _FINAL_CAUSES
        _CLOSED_SETTLEMENTS[remembered_key] = _ClosedSettlement(
            queue_ids, binding['assignmentId'], None if final else version, execution['turnId'],
            _turn_state(settled_turn), block_id, reusable=not unbound and (final or version is not None))

    if revoked:
        cause = 'dispatch_grant_revoked'
    elif assignment.get('review') is not None:
        cause = 'execution_reviewed'
    elif (isinstance(turn, dict) and turn.get('status') == 'completed'
            and isinstance(attempt, dict) and attempt.get('status') == 'completed'):
        cause = 'execution_completed'
    elif not unbound and _settled(turn, attempt) and not _resume_operations(turn):
        cause = 'execution_not_continuable'
    elif not unbound and _settled(turn, attempt) and 'continue' not in _resume_operations(turn):
        cause = 'execution_awaiting_resume'
        if remembered is None or remembered.turn_state != _turn_state(turn):
            logger.info('[BackgroundDelivery] %d background result(s) wait for the resume of '
                        'conv=%s turn=%s (checkpoint_resume only; no Continue issued)',
                        len(queue_ids), conv_id, execution['turnId'])
        remember(turn, None)
        return True
    else:
        return False
    if not isinstance(turn, dict) or turn.get('status') in {'pending', 'running'}:
        return True
    if not isinstance(attempt, dict) or attempt.get('status') in {'pending', 'running'}:
        return True

    deliveries = [{
        'queueId': row['queueId'],
        'commandId': (row.get('payload') or {}).get('_backgroundCommand'),
        'value': (row.get('payload') or {}).get('text') or '',
        'backgroundCommand': row.get('backgroundCommand'),
    } for row in receipts if isinstance(row.get('queueId'), str)
            and isinstance((row.get('payload') or {}).get('_backgroundCommand'), str)
            and (row.get('payload') or {}).get('text')]
    if not deliveries:
        remember(turn, None)
        return True
    receipt = background_command_injection_record({
        'roundNum': 0,
        'previews': [{
            'queueId': item['queueId'], 'commandId': item['commandId'],
            'text': item['value'][:1200], 'textTruncated': len(item['value']) > 1200,
            **({'backgroundCommand': item['backgroundCommand']}
               if isinstance(item.get('backgroundCommand'), dict) else {}),
        } for item in deliveries],
    }, attempt_id=execution['attemptId'])
    if (remembered is not None and remembered.turn_id == execution['turnId']
            and remembered.block_id == receipt['blockId']
            and remembered.turn_state[1] == turn.get('projectionRevision')):
        remember(turn, receipt['blockId'])
        return True
    turn = repository.turn_get(conv_id, execution['turnId'], user_id=user_id)
    if not isinstance(turn, dict) or turn.get('status') in {'pending', 'running'}:
        return True
    projection = dict(turn.get('projection') or {})
    previous = projection.get('_bgCommandInjects') or []
    attached = any(isinstance(row, dict) and row.get('blockId') == receipt['blockId']
                   for row in previous)
    if attached and remembered is not None and remembered.block_id == receipt['blockId']:
        # This process already sent this block's retirement and the rows stayed:
        # the authority predates retirement. Never rewrite the Turn for them again.
        remember(turn, receipt['blockId'])
        return True
    # A block attached before retirement existed keeps its unchanged projection;
    # the same command then retires its rows (one revision, once per batch).
    if not attached:
        projection['_bgCommandInjects'] = [*previous, receipt]
    retirement = {
        'block_id': receipt['blockId'], 'disposition': 'settled_as_evidence',
        'deliveries': [{'queue_id': item['queueId'], 'command_id': item['commandId']}
                       for item in deliveries],
    }
    try:
        result = update_turn_projection(
            conv_id, execution['turnId'], projection=projection,
            expected_projection_revision=turn['projectionRevision'], user_id=user_id,
            preserve_activity=True, background_retirement=retirement)
    except LifecycleConflict as error:
        if error.code in {'turn_in_progress', 'stale_projection', 'database_conflict'}:
            return True
        raise
    removed = (result or {}).get('removedQueueIds')
    if not isinstance(removed, list):
        # An authority without the member attached the evidence but kept the rows
        # (the behaviour before retirement). They stay listed; remember the written
        # revision so later ticks neither reread the projection nor rewrite it.
        logger.warning('[BackgroundDelivery] storage did not retire %d late receipt(s) of '
                       'conv=%s turn=%s; they stay pending', len(deliveries), conv_id,
                       execution['turnId'])
        written = (result or {}).get('turn')
        remember(written if isinstance(written, dict) else {
            **turn, 'projectionRevision': turn['projectionRevision'] + 1}, receipt['blockId'])
        return True
    logger.info('[BackgroundDelivery] settled %d background result(s) as evidence conv=%s '
                'assignment=%s turn=%s attempt=%s cause=%s turn_status=%s settlement_cause=%s '
                'retired=%d',
                len(deliveries), conv_id, binding['assignmentId'], execution['turnId'],
                execution['attemptId'], cause, turn.get('status'),
                (turn.get('settlement') or {}).get('cause'), len(removed))
    return True


def publish_completion_envelope(enqueue, conv_id, envelope, *, kind, user_id):
    """Replay frozen bytes first; repair legacy checkpoints only after refusal.

    The original envelope stays intact for lost-ACK recovery. A deterministic
    reduced config is attempted only after a permanent size/protocol refusal,
    under the SAME command/queue identity. The authority's payload fingerprint
    refuses any conflicting committed receipt; no second completion is minted.
    """
    config = envelope['config']
    try:
        return publish_completion(enqueue, conv_id, envelope['message'], config,
                                  kind=kind, user_id=user_id)
    except StorageError as error:
        cleaned = completion_delivery_config(config)
        if (error.retryable or error.code not in {
                'storage_payload_too_large', 'database_protocol_error'}
                or cleaned == config):
            raise
        return publish_completion(enqueue, conv_id, envelope['message'], cleaned,
                                  kind=kind, user_id=user_id)
