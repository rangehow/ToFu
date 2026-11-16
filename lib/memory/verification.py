"""Evidence-driven lesson trust: a Worker's verdict settles when its result is accepted.

Responsibility: promote a lesson to ``verified`` or demote it to ``disproved``
from verification evidence, using the existing trust classes and lifecycle
(``lib.memory.contracts``). A Worker that ran something confirming or
contradicting a lesson records a verdict with ``update_memory(verdict=...)``.
The verdict is only pending: it changes nothing a role reads until a reviewer
(the Coordinator's ``review_project_work`` or the owner's review) accepts that
Worker's exact result. Then a confirmation stamps ``provenance.verified_by``
(trust ``verified``) and a contradiction sets ``status=disproved`` with the
evidence as ``superseded_by``. A rejected or reworked result never settles.

Authority: the Worker's verdict is an agent report; the acceptance is a
reviewer's judgment of the whole result. Neither is owner speech, so
``owner-stated`` stays the owner's alone. A Worker cannot verdict a lesson
written in its own Turn, and only a Worker of a project assignment can record
one (nothing else is ever accepted).

Storage: one pending ledger per Worker conversation,
``<data>/memories/verdicts/<conversation_id>.json`` mapping a Turn id to its
verdicts. Settlement reads that one file, never the lesson corpus.

Entry points: ``record_verdict`` (the memory tool handler),
``settle_accepted_result`` (project dispatch review).
Dependencies: ``lib.memory.storage``, ``lib.json_store``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from lib.error_envelope import BadRequest
from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger
from lib.memory.contracts import (
    MEMORY_VERIFIED_BY_ACCEPTED_RESULT, normalize_memory_verdict, validate_memory_id,
)

logger = get_logger(__name__)

VERDICT_LEDGER_SUBPATH = os.path.join('memories', 'verdicts')


def _ledger_path(conversation_id: str) -> str:
    from lib.memory.storage import _dirs
    name = validate_memory_id(conversation_id, field='conversation_id')
    return os.path.join(_dirs._server_data_dir(), VERDICT_LEDGER_SUBPATH, f'{name}.json')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def record_verdict(memory_id, verdict, *, project_path, extra_paths, conversation_id,
                   turn_id) -> dict:
    """Record one pending verdict of this Worker Turn; returns the lesson it names."""
    from lib.memory.storage import get_memory, is_active_lesson
    verdict = normalize_memory_verdict(verdict)
    memory_id = validate_memory_id(memory_id)
    if not conversation_id or not turn_id:
        raise BadRequest('A verdict needs the recording Turn', field='verdict')
    lesson = get_memory(memory_id, project_path=project_path, extra_paths=extra_paths)
    if lesson is None or lesson.get('is_package'):
        raise BadRequest(f'No lesson has id {memory_id!r}', field='memory_id')
    if not is_active_lesson(lesson):
        raise BadRequest(f'Lesson {memory_id!r} is {lesson.get("status")}; only an active '
                         'lesson can be verified or disproved', field='memory_id')
    if (lesson.get('provenance') or {}).get('turn_id') == turn_id:
        raise BadRequest('A Turn cannot verify a lesson it wrote itself', field='memory_id')
    entry = {'memory_id': memory_id, **verdict, 'at': _now(),
             'project_path': project_path or '', 'extra_paths': list(extra_paths or [])}

    def add(ledger):
        ledger = dict(ledger or {})
        rows = [row for row in ledger.get(turn_id, []) if row.get('memory_id') != memory_id]
        ledger[turn_id] = [*rows, entry]
        return ledger

    path = _ledger_path(conversation_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    update_json_atomic(path, add, default={})
    return lesson


def pending_verdicts(conversation_id: str) -> dict:
    """The ledger of one Worker conversation: Turn id -> pending verdicts."""
    return read_json(_ledger_path(conversation_id), default={}) or {}


def settle_accepted_result(conversation_id: str, turn_id: str, *, accepted_by: dict) -> list[dict]:
    """Apply the verdicts of one accepted Worker Turn; return what each became.

    ``accepted_by`` names the acceptance (``actor``, ``conversation_id``,
    ``assignment_id``). A verdict whose lesson is gone or no longer active
    settles as ``stale``. Settled verdicts leave the ledger; a failure keeps
    them, so a repeated review settles them again.
    """
    from lib.memory.storage import get_memory, is_active_lesson, update_memory, verify_memory
    if not conversation_id or not turn_id:
        return []
    path = _ledger_path(conversation_id)
    rows = (read_json(path, default={}) or {}).get(turn_id) or []
    outcomes = []
    for row in rows:
        memory_id, roots = row['memory_id'], (row.get('project_path') or None,
                                              row.get('extra_paths') or [])
        if row['result'] == 'confirmed':
            verified = verify_memory(memory_id, {
                'by': MEMORY_VERIFIED_BY_ACCEPTED_RESULT, 'conversation_id': conversation_id,
                'turn_id': turn_id, 'evidence': row['evidence'], 'at': _now(),
                'accepted_by': ':'.join(str(accepted_by.get(key) or '') for key in
                                        ('actor', 'conversation_id', 'assignment_id'))},
                project_path=roots[0], extra_paths=roots[1])
            outcome = 'verified' if verified else 'stale'
        else:
            current = get_memory(memory_id, project_path=roots[0], extra_paths=roots[1])
            if current is None or not is_active_lesson(current):
                outcome = 'stale'
            else:
                update_memory(memory_id, {
                    'status': 'disproved', 'status_reason': 'disproved',
                    'superseded_by': {'evidence': row['evidence']}},
                    project_path=roots[0], extra_paths=roots[1],
                    provenance={'source_class': 'agent_report', 'conversation_id': conversation_id,
                                'turn_id': turn_id, 'at': _now()})
                outcome = 'disproved'
        outcomes.append({'memory_id': memory_id, 'result': row['result'], 'outcome': outcome})

    def drop(ledger):
        ledger = dict(ledger or {})
        if turn_id not in ledger:
            return None
        ledger.pop(turn_id)
        return ledger

    if rows:
        update_json_atomic(path, drop, default={})
    return outcomes


__all__ = ['VERDICT_LEDGER_SUBPATH', 'pending_verdicts', 'record_verdict',
           'settle_accepted_result']
