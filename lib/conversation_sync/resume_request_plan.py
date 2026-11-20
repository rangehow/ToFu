"""Bounded cold Continue candidate from the canonical Turn context query.

This internal read path does not enter task startup or authorize a request.
Opt-in selection is only an observation against detached policy inputs.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal
from dataclasses import dataclass, field, replace
import hashlib
import json

from lib.conversation_sync.dispatch_contract import ResumeSourceSnapshot, read_resume_source_snapshot


@dataclass(frozen=True)
class SelectionWitness:
    status: Literal['selected', 'verified_absent', 'indeterminate']
    candidate_digest: str
    content_digest: str | None = None


@dataclass(frozen=True)
class ColdResumeCandidate:
    source: ResumeSourceSnapshot
    messages_json: bytes = field(repr=False)
    history_digest: str
    anchor_json: bytes = field(repr=False)
    archive_witness: SelectionWitness | None = field(default=None, repr=False)
    continuation_witness: SelectionWitness | None = field(default=None, repr=False)
    readiness: str = field(default='unknown', init=False)

    @property
    def archive_presence(self):
        if self.archive_witness is not None:
            return {'selected': 'present', 'verified_absent': 'absent'}.get(self.archive_witness.status, 'unknown')
        return 'unknown'

    @property
    def continuation_selection(self):
        return self.continuation_witness.status if self.continuation_witness else 'unknown'

    @property
    def missing_witnesses(self):
        unavailable = tuple(name for name, witness in (
            ('continuation', self.continuation_witness), ('archive', self.archive_witness),
        ) if witness is None or witness.status == 'indeterminate')
        return (*unavailable, 'policy', 'tools', 'route_subscription', 'window')


def read_cold_resume_candidate(*, user_id: int, conversation_id: str, turn_id: str,
                               attempt_id: str, projection_revision: int,
                               history_digest: str, resume_anchor: dict | None = None,
                               selection_config: dict | None = None
                               ) -> ColdResumeCandidate | str:
    """Render one complete text-only cold context page, or explicitly refuse.

    The second query must independently witness the same target pin in its own
    snapshot. A page cursor or detached Turn head is not complete evidence.
    Passing a trusted, detached normal replay configuration opts in; it is
    comparison input, not proof of current serving policy or permission.
    """
    from lib.agent_core.storage_errors import StorageError
    from lib.storage import get_storage_client

    if (not isinstance(history_digest, str) or len(history_digest) != 64
            or any(char not in '0123456789abcdef' for char in history_digest)):
        return 'history_unavailable'
    source = read_resume_source_snapshot(user_id=user_id, conversation_id=conversation_id,
        turn_id=turn_id, attempt_id=attempt_id, projection_revision=projection_revision)
    if isinstance(source, str):
        return source
    selection_config = deepcopy(selection_config)
    pin = {'attempt_id': attempt_id, 'projection_revision': projection_revision,
           'history_digest': history_digest, 'resume_anchor': resume_anchor}
    if selection_config is not None:
        pin['selection'] = True
    try:
        context = get_storage_client().query('turn.context.read', {
            'user_id': user_id, 'conversation_id': conversation_id, 'turn_id': turn_id,
            'replay': False,
            'continue_plan': pin,
        }, deadline=5.0)
    except StorageError as exc:
        if exc.code == 'turn_projection_stale':
            return 'stale_context'
        if exc.code == 'database_conflict':
            return 'continue_ineligible'
        if (exc.code in ('unknown_operation', 'operation_not_implemented')
                or (exc.code == 'database_protocol_error' and str(exc) == 'Unknown storage operation')):
            return 'history_unavailable'
        raise
    if (not isinstance(context, dict) or context.get('supported') is not True
            or 'nextTurnId' not in context or context['nextTurnId'] is not None
            or type(context.get('covered')) is not int or context['covered'] != 0
            or 'continuation' not in context or context['continuation'] is not None):
        return 'history_unavailable'
    rows = context.get('turns')
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        return 'history_unavailable'
    target = rows[-1]
    if (target.get('turnId') != turn_id or target.get('currentAttemptId') != attempt_id
            or type(target.get('projectionRevision')) is not int
            or target['projectionRevision'] != projection_revision
            or target.get('status') != source.source_status):
        return 'stale_source' if target.get('turnId') == turn_id else 'history_unavailable'
    proof = context.get('historyProof')
    digest = proof.get('digest') if isinstance(proof, dict) else None
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in '0123456789abcdef' for char in digest)
            or type(proof.get('version')) is not int or proof['version'] != 2):
        return 'history_unavailable'
    if digest != history_digest:
        return 'stale_context'
    plan = context.get('continuePlan')
    if (not isinstance(plan, dict) or set(plan) != {'historyDigest', 'anchor', 'projection'}
            or plan['historyDigest'] != digest or not isinstance(plan['anchor'], dict)
            or not isinstance(plan['projection'], dict)):
        return 'continue_eligibility_unavailable'
    normalized_rows = [*rows[:-1], {**target, 'projection': plan['projection']}]
    candidate = _build_text_candidate(source, normalized_rows, history_digest=digest,
                                      anchor=plan['anchor'], user_id=user_id)
    if not isinstance(candidate, ColdResumeCandidate) or selection_config is None:
        return candidate
    return _observe_selection(candidate, rows, normalized_rows[-1], context, selection_config,
                              user_id=user_id, conversation_id=conversation_id)


def _selection_digest(value):
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    digest = hashlib.sha256()
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode('utf-8'))
    return digest.hexdigest()


def _selection_inputs(context, row_count):
    inputs = context.get('continueSelectionInputs')
    if (not isinstance(inputs, dict)
            or inputs.keys() != {'version', 'continuationStatus', 'covered', 'continuation', 'archives'}
            or type(inputs['version']) is not int or inputs['version'] != 1
            or type(inputs['covered']) is not int or not 0 <= inputs['covered'] < row_count
            or type(context.get('proofEligible')) is not bool):
        return None
    status, cached, covered = inputs['continuationStatus'], inputs['continuation'], inputs['covered']
    if status == 'available':
        if not isinstance(cached, dict) or not covered:
            return None
    elif (status not in ('checkpoint_missing', 'invalid_checkpoint', 'legacy_checkpoint',
                         'legacy_proof', 'checkpoint_not_before_target', 'history_changed', 'terminal_changed')
          or cached is not None or covered != 0):
        return None
    if inputs['archives'] is not None and not isinstance(inputs['archives'], list):
        return None
    return inputs


def _observe_selection(candidate, rows, normalized_target, context, config, *, user_id, conversation_id):
    from lib.conversation_sync.command_service import continue_retained_config
    from lib.tasks_pkg.compaction._resume_projection import (
        resume_execution_digest, select_retained_text_archive,
    )
    from lib.tasks_pkg.model_replay import select_retained_text_continuation
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state

    source = candidate.source
    binding = _selection_digest([user_id, conversation_id, source.turn_id, source.attempt_id,
                                 source.projection_revision, candidate.history_digest,
                                 json.loads(candidate.anchor_json)])
    inputs = _selection_inputs(context, len(rows))
    archive = continuation = ('indeterminate', None)
    if inputs is not None:
        proof = context['historyProof']
        validated = {**inputs, 'historyProof': proof, 'proofEligible': context.get('proofEligible')}
        continuation = select_retained_text_continuation(
            rows, config, validated, conversation_id=conversation_id, user_id=user_id)
        retained = prepare_resume_state(continue_retained_config(normalized_target))
        projection = normalized_target['projection']
        config.pop('_resumeBackgroundDigest', None)  # plain candidate has no background deliveries
        config.update(_modelReplayHistoryProof=proof, _resumeExecutionDigest=resume_execution_digest(
            projection.get('content'), projection.get('thinking')))
        archive = select_retained_text_archive(
            inputs['archives'], config, retained.checkpoint_tool_rounds,
            conversation_id=conversation_id, user_id=user_id, turn_id=source.turn_id)

    def witness(observation):
        status, record = observation
        return SelectionWitness(status, binding, _selection_digest(record) if record is not None else None)

    return replace(candidate, archive_witness=witness(archive), continuation_witness=witness(continuation))


def _build_text_candidate(source, rows, *, history_digest, anchor, user_id):
    from lib.tasks_pkg.orchestrator._resume_state import (
        ContinueResumeStateProtocolError, prepare_resume_state,
    )
    from lib.tasks_pkg.message_semantics import has_nested_model_media
    from lib.conversation_sync.command_service import continue_retained_config
    from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix
    from lib.turn_lifecycle import project_turn_context
    from lib.turn_verdict import PAUSE_SETTLEMENT_CAUSES

    # The canonical builder may hydrate attachments, resolve provider-specific
    # reasoning policy, or handle fresh/flow modes. Do not enter those paths.
    if rows[-1].get('actor') != 'assistant' or any(not _plain_row(row) for row in rows):
        return 'unsupported_history'
    for row in rows:
        projection = row['projection']
        rounds = projection.get('toolRounds', [])
        if (has_nested_model_media(projection) or not isinstance(rounds, list)
                or (row is not rows[-1]
                    and scan_replayable_tool_round_prefix(rounds).blocked_position is not None)):
            return 'unsupported_history'
    if any(row.get('status') != 'completed' for row in rows[:-1]):
        return 'unsupported_history'
    target = rows[-1]
    if (target.get('settlement') or {}).get('cause') in PAUSE_SETTLEMENT_CAUSES:
        return 'unsupported_history'
    try:
        prepared = prepare_resume_state(continue_retained_config(target))
    except ContinueResumeStateProtocolError:
        return 'unsupported_history'
    messages = project_turn_context(rows, {'excludeLast': True}, turn_id=source.turn_id,
                                    user_id=user_id)
    messages.extend(prepared.checkpoint_messages)
    if not _closed_tool_occurrences(messages):
        return 'unsupported_history'
    if not messages:
        return 'history_unavailable'
    return ColdResumeCandidate(source, json.dumps(messages, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf-8'), history_digest,
        json.dumps(anchor, allow_nan=False, separators=(',', ':')).encode('utf-8'))


def _closed_tool_occurrences(messages):
    """Match announced and returned IDs within each contiguous tool batch."""
    pending = set()
    for message in messages:
        if message.get('role') == 'tool':
            call_id = message.get('tool_call_id')
            if call_id not in pending:
                return False
            pending.remove(call_id)
            continue
        if pending:
            return False
        calls = message.get('tool_calls') or ()
        for call in calls:
            call_id = call.get('id')
            if not isinstance(call_id, str) or not call_id or call_id in pending:
                return False
            pending.add(call_id)
    return not pending


def _plain_row(row):
    projection = row.get('projection')
    return (row.get('laneId') == 'main' and row.get('actor') in ('human', 'assistant')
            and isinstance(row.get('turnId'), str) and isinstance(projection, dict)
            and isinstance(projection.get('content', ''), str)
            and not any(projection.get(key) for key in (
                'attachments', 'images', 'videos', 'thinking', '_responsesItems',
                '_anthropicContentBlocks', 'planExecution', 'usageRecovery',
                '_bgCommandInjects', '_isCompactionSummary', '_isFlowReview',
                '_isFlowPlanner', '_flowIteration', '_isAutopilotSummary',
            )))
