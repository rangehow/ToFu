"""Owner-scoped model context read with semantic-revision continuations.

Compact authority headers prove the prior main-lane prefix. Display-only
settled enrichment advances ordinary projection CAS without invalidating model
context. Real edits/execution changes, ownership and current-target fences stay
mandatory. Optional cold Continue planning observes an eligible source and its
anchor in the proof snapshot; it never substitutes for write acceptance.
The proof digest streams over every header in keyset pages: memory holds one
header page plus the response page, and lane length is never a ceiling. A Turn
with more than one ``turn.projection.page`` page travels as its page-0 head,
which the client completes by page (docs/STORAGE.md, "Paged Turn storage").
The native authority (packages/tofu-db/src/turn/model_context.rs) has the same
selection semantics.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from lib.storage.errors import StorageError
from lib.storage.model_continuation import settled_witness
from lib.storage.turn_projection_pages import turn_projection_page
from ._common import _required_text, _integer
from ._turns_core import _turn_public, _optional_row_value
from ._model_continuation import continuation_observe
from ._archives import resume_selection_inputs
from ._turns_write import (
    _attempt_later_turns, _attempt_resume_anchor, _attempt_resume_projection,
    _attempt_source,
)

# Rows per keyset statement, mirroring the native engine's compact index page
# (packages/tofu-db/src/turn.rs INDEX_PAGE_ROWS). It bounds one result set,
# never the walk.
_HEADER_PAGE_ROWS = 1000
_HEADER_COLUMNS = ('turn_id,parent_turn_id,status,current_attempt_id,projection_revision,'
                   'model_context_display_offset,ordinal')
_CURSOR_OUTSIDE = 'context page cursor is outside the selected suffix'


def _header_bytes(row):
    revision = int(row['projection_revision']) - int(_optional_row_value(row, 'model_context_display_offset') or 0)
    if revision < 0:
        raise StorageError('database_integrity', 'context display offset exceeds revision')
    return (json.dumps([
        row['turn_id'], row['parent_turn_id'], row['status'],
        row['current_attempt_id'], revision,
    ], ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def _context_row(turn):
    """The Turn itself, or ``{'pagedTurn': head}`` when it has more than one page."""
    first = turn_projection_page(turn, 0)
    return {'pagedTurn': first['turn']} if first['pageCount'] > 1 else turn


def _row_turn(row):
    """The Turn record of a response row; a paged Turn's row carries its head."""
    return row.get('pagedTurn', row)


def _main_lane_headers(session, conv, owner, through_ordinal, *, after_ordinal=None):
    """Main-lane headers in lane order through ``through_ordinal``, one page per statement."""
    while True:
        if after_ordinal is None:
            rows = session.fetch_all(
                f'SELECT {_HEADER_COLUMNS} FROM storage_conversation_turns '
                'WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal<=? '
                'ORDER BY ordinal,turn_id LIMIT ?',
                (conv, owner, 'main', through_ordinal, _HEADER_PAGE_ROWS))
        else:
            rows = session.fetch_all(
                f'SELECT {_HEADER_COLUMNS} FROM storage_conversation_turns '
                'WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal>? '
                'AND ordinal<=? ORDER BY ordinal,turn_id LIMIT ?',
                (conv, owner, 'main', after_ordinal, through_ordinal, _HEADER_PAGE_ROWS))
        yield from rows
        if len(rows) < _HEADER_PAGE_ROWS:
            return
        # (conversation_id, lane_id, ordinal) is unique: the ordinal is the key.
        after_ordinal = rows[-1]['ordinal']


class _Page:
    """One bounded response page of the selected suffix, after the caller's cursor."""

    def __init__(self, session, conv, owner, after_turn_id, continuation, target_row):
        self.session, self.conv, self.owner = session, conv, owner
        self.target_row = target_row
        self.after = after_turn_id
        continuation_bytes = len(json.dumps(continuation, ensure_ascii=False,
                                            separators=(',', ':')).encode('utf-8')) if continuation else 0
        self.limit = min(67_104_768 - 8 * 1024 * 1024 - 128 * 1024,
                         67_104_768 - continuation_bytes - 128 * 1024)
        self.turns = []
        self.bytes = 0
        self.full = False

    def offer(self, header, *, target):
        if self.after is not None:
            if header['turn_id'] == self.after:
                if target:
                    raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
                self.after = None
            return
        if self.full:
            return
        row = self.target_row if target else self.session.fetch_one(
            'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
            'AND user_id=? AND turn_id=?', (self.conv, self.owner, header['turn_id']))
        if row is None:
            raise StorageError('database_internal', 'Model context Turn disappeared')
        turn = _context_row(_turn_public(self.session, row))
        size = len(json.dumps(turn, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if self.turns and self.bytes + size > self.limit:
            self.full = True
            return
        self.bytes += size
        self.turns.append(turn)


def _continue_plan_request(payload, replay, after_turn_id):
    if 'continue_plan' not in payload:
        return None
    request = payload['continue_plan']
    if not isinstance(request, Mapping) or replay or after_turn_id is not None:
        raise StorageError('database_protocol_error', 'Continue planning needs a cold context head')
    if set(request) - {'attempt_id', 'projection_revision', 'history_digest', 'resume_anchor', 'selection'}:
        raise StorageError('database_protocol_error', 'Invalid Continue planning fields')
    attempt_id = _required_text(request, 'attempt_id', 128)
    revision = _integer(request, 'projection_revision', minimum=0, maximum=2**63 - 1)
    digest = request.get('history_digest')
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in '0123456789abcdef' for char in digest)):
        raise StorageError('database_protocol_error', 'Invalid Continue planning history digest')
    requested_anchor = request.get('resume_anchor')
    if requested_anchor is not None and not isinstance(requested_anchor, Mapping):
        raise StorageError('database_protocol_error', 'Requested resume anchor must be an object')
    selection = request.get('selection', False)
    if type(selection) is not bool:
        raise StorageError('database_protocol_error', 'Invalid Continue planning selection')
    return attempt_id, revision, digest, requested_anchor, selection


def _continue_plan_result(session, target, public, request, digest):
    attempt_id, revision, expected_digest, requested_anchor, _ = request
    if expected_digest != digest or public['currentAttemptId'] != attempt_id:
        raise StorageError('turn_projection_stale', 'The Continue source or history changed.')
    current_id, current = _attempt_source(session, target, public, revision)
    if (current is None or current['status'] not in ('interrupted', 'truncated', 'failed')
            or target['status'] != current['status']):
        raise StorageError('database_conflict', 'The Continue source is not resumable.')
    anchor = _attempt_resume_anchor(public, 'continue', requested_anchor)
    _attempt_later_turns(session, target)
    projection = _attempt_resume_projection(session, target, public, current_id, current)
    return {'historyDigest': digest, 'anchor': anchor, 'projection': projection}


def read_model_context(session, payload):
    conv = _required_text(payload, 'conversation_id', 256)
    target_id = _required_text(payload, 'turn_id', 128)
    owner = _integer(payload, 'user_id', minimum=1)
    replay = payload.get('replay', True)
    if type(replay) is not bool:
        raise StorageError('database_protocol_error', 'replay must be boolean')
    after_turn_id = payload.get('after_turn_id')
    if after_turn_id is not None:
        if not isinstance(after_turn_id, str) or not 0 < len(after_turn_id) <= 128:
            raise StorageError('database_protocol_error', 'invalid context page cursor')
    plan_request = _continue_plan_request(payload, replay, after_turn_id)
    target = session.fetch_one(
        'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
        'AND user_id=? AND turn_id=?', (conv, owner, target_id))
    if target is None:
        return None
    if target['lane_id'] != 'main':
        return {'supported': False}
    target_ordinal = int(target['ordinal'])
    selection = plan_request is not None and plan_request[-1]
    identity = {'conversation_id': conv, 'user_id': owner}
    present, cached = (continuation_observe(session, identity | {'lane_id': 'main'}, selection=selection)
                       if replay or selection else (False, None))
    proof = (cached or {}).get('historyProof')
    proof = proof if isinstance(proof, dict) else {}
    count = proof.get('count')
    version2 = type(proof.get('version')) is int and proof.get('version') == 2
    terminal = (cached or {}).get('turns') or []
    # Lane index of the checkpoint terminal; it can cover a prefix only when
    # the walk reaches it before the target.
    anchor = (count if version2 and type(count) is int and count >= 0 and len(terminal) == 1
              else None)
    if not (replay or selection):
        continuation_status = 'replay_disabled'
    elif not cached:
        continuation_status = 'invalid_checkpoint' if selection and present else 'checkpoint_missing'
    elif 'historyProof' not in cached:
        continuation_status = 'legacy_checkpoint'
    elif proof.get('version') != 2:
        continuation_status = 'legacy_proof'
    else:
        continuation_status = 'checkpoint_not_before_target'
    legacy = replay and bool(cached) and 'historyProof' not in cached
    # Uncovered history is paged as it streams by. A possible anchor defers
    # the page until the walk proves or refutes it.
    page = (_Page(session, conv, owner, after_turn_id, cached if legacy else None, target)
            if anchor is None or selection else None)
    digest = hashlib.sha256()
    index = 0
    covered = 0
    cursor_ordinal = None
    reached_target = False
    for header in _main_lane_headers(session, conv, owner, target_ordinal):
        is_target = header['turn_id'] == target_id
        if is_target and int(header['ordinal']) != target_ordinal:
            raise StorageError('database_internal', 'Model context target order changed')
        if page is not None:
            page.offer(header, target=is_target)
        elif after_turn_id is not None and header['turn_id'] == after_turn_id:
            if is_target:
                raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
            cursor_ordinal = int(header['ordinal'])
        if is_target:
            reached_target = True
            break
        if index == anchor:
            continuation_status = 'history_changed'
            if digest.hexdigest() == proof.get('digest'):
                continuation_status = 'terminal_changed'
                final_id = terminal[0][0]
                if header['turn_id'] == final_id:
                    row = session.fetch_one(
                        'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
                        'AND user_id=? AND turn_id=?', (conv, owner, final_id))
                    if row is not None and settled_witness(_turn_public(session, row)) == terminal[0]:
                        covered = index + 1
                        continuation_status = 'available'
            if covered and replay:
                if cursor_ordinal is not None:
                    raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
                page = _Page(session, conv, owner, after_turn_id, cached, target)
        digest.update(_header_bytes(header))
        index += 1
    if not reached_target:
        raise StorageError('database_internal', 'Model context target order changed')
    if page is None:
        # The anchor covered nothing: walk only the page again, from the lane
        # head or after the cursor. A proof-bearing checkpoint is never legacy.
        if after_turn_id is not None and cursor_ordinal is None:
            raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
        page = _Page(session, conv, owner, None, None, target)
        for header in _main_lane_headers(session, conv, owner, target_ordinal,
                                         after_ordinal=cursor_ordinal):
            page.offer(header, target=header['turn_id'] == target_id)
            if page.full:
                break
    if page.after is not None:
        raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
    continuation = cached if covered or legacy else None
    turns = page.turns
    history_digest = digest.hexdigest()
    result = {
        'supported': True, 'covered': covered if replay else 0, 'turns': turns,
        'nextTurnId': _row_turn(turns[-1])['turnId'] if page.full else None,
        'continuation': continuation if replay else None,
        'continuationStatus': continuation_status if replay else 'replay_disabled',
        'historyProof': {'version': 2, 'count': index, 'digest': history_digest},
        'proofEligible': all(not (_row_turn(row).get('projection') or {}).get('attachments')
                             for row in turns),
    }
    # Cold planning cannot authorize a partial history or detached Turn head.
    # The ordinary page contract remains intact, but such a page gets no plan.
    if plan_request is not None and not page.full and all('pagedTurn' not in turn for turn in turns):
        plan = _continue_plan_result(session, target, turns[-1], plan_request, history_digest)
        plan_bytes = len(json.dumps(plan, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if page.bytes + plan_bytes <= page.limit:
            result['continuePlan'] = plan
            if selection:
                inputs = {'version': 1, 'continuationStatus': continuation_status, 'covered': covered,
                          'continuation': continuation, 'archives': resume_selection_inputs(session, identity)}
                inputs_bytes = len(json.dumps(inputs, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
                if page.bytes + plan_bytes + inputs_bytes <= page.limit:
                    result['continueSelectionInputs'] = inputs
    return result
