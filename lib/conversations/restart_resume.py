"""Conversations a restart interrupted resume from their checkpoint by themselves.

Design authority: docs/RUN_CONTINUITY.md, "Restart-paused conversations resume
by themselves". A server restart settles the Turns it interrupts
``interrupted`` with cause ``server_restart`` (the shutdown quiesce, or boot
recovery for a killed server). After the restart, the next server generation
continues each such Turn that no later input superseded and that no other
owner resumes, through the ordinary resume command exactly as the owner's
Continue click would: the operation its settlement offers (``continue``, else
``checkpoint_resume``, with its anchor), the Turn's own accepted execution
identity and public config, and the command id
``restart-resume:<interrupted Attempt>``, so repeated passes, processes and
restarts resume each interruption once and redo no work.

A resume completes the interrupted Turn in place under a new Attempt and
preserves every later Turn of its lane on both authorities (only regenerate
discards later Turns, and only on the owner's acknowledgement); the resumed
model context reads the lane prefix through the Turn's ordinal. So the Turn
resumes in place, keeping its own authority, unless a later input Turn
superseded it (outcome ``superseded``: newer input owns the lane). Later
appended records alone (the reports a Coordinator posts during its long Turn)
never stop it. ``lane_tail_verdict`` is that one rule, shared with project
dispatch recovery. Nothing is written to a Turn that is not resumed.

Left alone: Turns other owners resume (dispatched Workers and Coordinator
wakes: project dispatch recovery; Goal and Flow runs: orchestration
continuity), a Turn waiting for the owner's answer (``answer_guidance``), a
Turn whose settlement names calls the restart cut off with an unknown side
effect (``unknown_effects_to_review``: its owner checks them first), a
Turn that offers only Regenerate, anything the owner stopped (cause
``user_abort``), a Turn a later input superseded, a Turn that ended before
the stop, and everything after a manual shutdown or in a restart storm.

Candidates: the Turns each stopping process quiesced (one durable record per
stopping process, written before its tasks settle; namespace
``RESTART_PAUSED_NAMESPACE``; each entry names the interrupted Turn and
Attempt), the conversations boot recovery settled for a killed server, and
those an open restart notice lists as interrupted. The interrupted Turn comes
from the record, never from the lane tail: a Coordinator appends reports after
its long Turn. A conversation named without a Turn stands for its newest
main-lane Turn that did work (``newest_work_turn``). Every candidate is
validated against its durable Turn before anything starts.

The same ordinary resume command also recovers a Global Router notice whose
review exhausted the terminal-response budget (empty_stop) and settled failed
with no answer. ``failed_notice_resume_option`` admits only that class — a
durable nonhuman parent of any kind, no current-Turn owner steer, no
deliverable answer or deliberate-silence note, and a settlement that already
offers continue/checkpoint_resume — and ``resume_failed_notice_turn`` issues
it with command id ``notice-review-resume:<failed Attempt>``. The failed Turn
keeps its evidence and completed tool results; the notice is never marked read
or reviewed, owner-authored Turns and genuine provider errors are left alone,
and the Router's terminal observer (lib/channels/autoreply.py) is the only
caller, as a bounded follow-up rather than a model call inside settlement.

Entry points: record_quiesced_conversations (shutdown quiesce),
set_boot_recovered (boot recovery), resume_restart_paused_turns (post-ready),
resume_recovered_turn (a serving process's recovery settlement drain),
resume_conversation, resume_failed_notice_turn, failed_notice_resume_option,
describe_conversation, newest_work_turn, pass_finished, last_results.
Dependencies: lib.storage (record.*), lib.turn_lifecycle,
lib.conversation_sync (runtime command service, execution identity),
lib.project_dispatch.admission, lib.lifecycle_approval, lib.boot_identity.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import logging
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

RESTART_PAUSED_NAMESPACE = 'conversation.restart_paused.v1'
RESUME_OPERATIONS = ('continue', 'checkpoint_resume')
# The conversation_sync_v3 round-actor enum of input Turns.
INPUT_ACTORS = frozenset({'human', 'critic', 'virtual_user'})


def unknown_effects_to_review(turn: Mapping | None) -> list[dict]:
    """The calls a restart cut off with an unknown side effect, as the settlement names them.

    Durable worker recovery names them (``error.unknownEffects``) when it
    settles an Attempt whose process died mid-call. No automated path continues
    such a Turn in place: its owner (a Worker's Coordinator, or the human)
    checks them first. Every other restart interruption resumes by itself.
    """
    settlement = turn.get('settlement') if isinstance(turn, Mapping) else None
    error = settlement.get('error') if isinstance(settlement, Mapping) else None
    effects = error.get('unknownEffects') if isinstance(error, Mapping) else None
    return [dict(item) for item in effects if isinstance(item, Mapping)] if isinstance(effects, list) else []


def describe_unknown_effects(effects) -> str:
    return ('the restart cut off ' + ', '.join(f"{item.get('toolName')} {item.get('toolCallId')}"
                                              for item in effects)
            + ' with an unknown side effect; check what each changed before continuing it')


def lane_tail_verdict(turn: Mapping, later_turns, *, interrupted_at=None) -> dict:
    """Whether a later input moved past ``turn``, and how many Turns follow it.

    ``later_turns`` are the Turns after ``turn`` on its lane, oldest first.
    ``supersededBy`` is the first later input Turn that is not an appended
    (ingested) record: the newer input owns the lane, so the Turn is not
    resumed. Without one the Turn resumes in place, whatever appended records
    follow it: a resume preserves every later Turn. ``tail`` and ``later``
    describe the lane; ``queued`` says the superseding input was accepted
    before the interruption (``interrupted_at``: the interrupted Attempt's
    settledAt).
    Pure: both resume owners (this pass and project dispatch recovery) supply
    the later Turns through their own read ports.
    """
    later = list(later_turns)
    superseding = next((row for row in later if row.get('actor') in INPUT_ACTORS
                        and row.get('kind') != 'ingested'
                        and (row.get('settlement') or {}).get('cause') != 'ingested'), None)
    accepted = (superseding or {}).get('createdAt')
    return {'tail': not later, 'later': len(later),
            'supersededBy': superseding.get('turnId') if superseding else None,
            'queued': (superseding is not None and interrupted_at is not None
                       and isinstance(accepted, (int, float)) and accepted < interrupted_at)}
# The storage record.list protocol maximum per page.
_RECORD_LIST_LIMIT = 1000

_PASS_FINISHED = False
_LAST_RESULTS: list[dict] = []
_BOOT_RECOVERED: list[dict] = []
# How the previous generation stopped, classified at boot: the stand-down rule of
# this generation's automatic resumes, the boot pass and recovered Turns alike.
_PREVIOUS_SHUTDOWN: dict | None = None


def pass_finished() -> bool:
    """Whether this process ran its post-restart resume pass (restart notices wait for it)."""
    return _PASS_FINISHED


def last_results() -> list[dict]:
    """This process's resume pass results, one per candidate conversation."""
    return deepcopy(_LAST_RESULTS)


def set_boot_recovered(recovery_result, *, previous_shutdown=None) -> None:
    """Hand boot recovery's settled orphans (a killed server's Turns) to the post-ready pass,
    with the previous generation's shutdown classification."""
    global _BOOT_RECOVERED, _PREVIOUS_SHUTDOWN
    if isinstance(recovery_result, Mapping):
        _BOOT_RECOVERED = [dict(item) for item in recovery_result.get('recoveredTasks') or ()
                           if isinstance(item, Mapping)]
    _PREVIOUS_SHUTDOWN = dict(previous_shutdown) if isinstance(previous_shutdown, Mapping) else None


def resume_recovered_turn(owner: int, conversation_id: str, turn_id: str, attempt_id: str) -> dict:
    """Continue a Turn that recovery in this serving process settled ``interrupted``.

    Durable worker recovery settles an Attempt whose process died, or whose
    claim storage fenced while its process lived (a storage restart or outage),
    and no boot pass ever lists it. Its settlement drain calls this, after
    queued input had its turn: the same rule as the boot pass
    (``resume_conversation``, command id ``restart-resume:<Attempt>``, so a
    Coordinator recovery resuming the same Turn replays it), and the same
    stand-down of this generation after a manual shutdown or in a restart storm.
    """
    stand_down = _stand_down(_PREVIOUS_SHUTDOWN)
    if stand_down:
        return _result(owner, conversation_id, 'not_resumed', stand_down,
                       turnId=turn_id, attemptId=attempt_id)
    return resume_conversation(owner, conversation_id, turn_id=turn_id, attempt_id=attempt_id)


def _client(write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=True) if write else get_storage_client()


def record_quiesced_conversations(tasks) -> None:
    """Record the Turns a stopping process quiesced, before they settle. Contained: never raises."""
    from lib import boot_identity
    from lib.identity import require_user_id
    entries = []
    for task in tasks or ():
        try:
            owner = require_user_id(task.get('_userId'), context='restart quiesce record')
        except (TypeError, ValueError):
            continue
        identity = [task.get(name) for name in ('convId', '_turnId', '_attemptId')]
        if all(isinstance(value, str) and value for value in identity):
            entries.append({'owner': owner, 'conversationId': identity[0], 'turnId': identity[1],
                            'attemptId': identity[2]})
    if not entries:
        return
    key = f'{boot_identity.BOOT_ID}:{uuid.uuid4().hex[:12]}'
    try:
        _client(write=True).command('record.put', {
            'namespace': RESTART_PAUSED_NAMESPACE, 'key': key, 'expected_version': 0,
            'value': {'version': 1, 'bootId': boot_identity.BOOT_ID, 'recordedAt': time.time(),
                      'conversations': entries}}, 'restart-paused:' + key)
    except Exception:
        logger.warning('[RestartResume] quiesced conversations were not recorded; boot recovery '
                       'and restart notices still name them', exc_info=True)


def _quiesce_records() -> list[dict]:
    """Records of earlier generations: ``{'key', 'version', 'conversations'}``."""
    from lib import boot_identity
    records, after = [], ''
    while True:
        rows = _client().query('record.list', {
            'namespace': RESTART_PAUSED_NAMESPACE, 'prefix': '', 'after_key': after,
            'limit': _RECORD_LIST_LIMIT})
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            value = row.get('value') if isinstance(row, Mapping) else None
            if (not isinstance(value, Mapping) or value.get('bootId') == boot_identity.BOOT_ID
                    or not isinstance(value.get('conversations'), list)):
                continue
            records.append({'key': row['key'], 'version': row['version'],
                            'conversations': [item for item in value['conversations']
                                              if isinstance(item, Mapping)]})
        if len(rows) < _RECORD_LIST_LIMIT:
            return records
        after = rows[-1]['key']


def _delete_record(record: dict) -> None:
    from lib.storage.errors import StorageError
    try:
        _client(write=True).command('record.delete', {
            'namespace': RESTART_PAUSED_NAMESPACE, 'key': record['key'],
            'expected_version': record['version']}, 'restart-paused-done:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise


def _task_owner(task_id: str) -> int | None:
    """The owner of one boot-recovered task, from its durable result record."""
    row = _client().query('record.get', {'namespace': 'task_results', 'key': task_id})
    value = row.get('value') if isinstance(row, Mapping) else None
    owner = value.get('user_id') if isinstance(value, Mapping) else None
    text = str(owner or '')
    return int(text) if text.isdigit() and int(text) > 0 else None


def _candidates(records: list[dict], boot_recovered: list[dict]) -> list[dict]:
    """One candidate per interrupted Turn: ``owner``, ``conversationId``, ``turnId``, ``attemptId``.

    A quiesce record names the Turn and Attempt; boot recovery and restart
    notices name only the conversation (``turnId`` None), which is dropped once
    a record names one of its Turns.
    """
    from lib import boot_identity
    from lib import lifecycle_approval as gate
    named = [{'owner': int(item['owner']), 'conversationId': str(item['conversationId']),
              'turnId': item.get('turnId') or None, 'attemptId': item.get('attemptId') or None}
             for record in records for item in record['conversations']
             if item.get('owner') and item.get('conversationId')]
    for item in boot_recovered:
        if item.get('taskId') and item.get('conversationId'):
            owner = _task_owner(str(item['taskId']))
            if owner is not None:
                named.append({'owner': owner, 'conversationId': str(item['conversationId'])})
    for notice in gate.restart_notices():
        accepted = notice.get('acceptedAt')
        if isinstance(accepted, (int, float)) and accepted < boot_identity.BOOT_TS:
            named.extend({'owner': int(notice['owner']), 'conversationId': str(conversation)}
                         for conversation in notice.get('interrupted') or ())
    recorded = {(item['owner'], item['conversationId']) for item in named if item.get('turnId')}
    candidates: dict[tuple, dict] = {}
    for item in named:
        pair = (item['owner'], item['conversationId'])
        if item.get('turnId') or pair not in recorded:
            candidates.setdefault((*pair, item.get('turnId')), {'turnId': None, 'attemptId': None, **item})
    return list(candidates.values())


def _pause(previous_shutdown) -> dict | None:
    """Why this generation's automatic resume stands down: ``{'guard', 'reason'}``, or None."""
    if not isinstance(previous_shutdown, Mapping):
        return None
    if previous_shutdown.get('restart_storm'):
        return {'guard': 'restart_storm',
                'reason': ('restart storm: the server is in a crash loop, so automatic resume stands '
                           'down and cannot re-run paid turns into it; Continue it when the server is stable')}
    if previous_shutdown.get('verdict') == 'clean' and previous_shutdown.get('reason') == 'manual':
        return {'guard': 'manual_shutdown',
                'reason': 'manual shutdown: the owner stopped the server, so nothing resumes by itself'}
    return None


def _stand_down(previous_shutdown) -> str:
    pause = _pause(previous_shutdown)
    return pause['reason'] if pause else ''


def automatic_resume_pause() -> dict | None:
    """This generation's stand-down of automatic resume (``{'guard', 'reason'}``), or None.

    The settlement of a Turn this generation's recovery interrupts carries it
    (``worker_recovery``), and the restart notice reports it, so a paused
    resume is never silent.
    """
    return _pause(_PREVIOUS_SHUTDOWN)


def _result(owner, conversation_id, outcome, reason='', **fields) -> dict:
    return {'owner': owner, 'conversationId': conversation_id, 'outcome': outcome, 'reason': reason,
            'turnId': fields.get('turnId'), 'attemptId': fields.get('attemptId'),
            'operation': fields.get('operation'), 'supersededBy': fields.get('supersededBy')}


def _lane_tail(owner: int, conversation_id: str) -> dict | None:
    """The newest main-lane Turn's metadata (no projection body)."""
    from lib import turn_lifecycle as turns
    from lib.swarm.restart_recovery import read_recovery_turns
    page = read_recovery_turns(conversation_id, user_id=owner, lane_id='main', limit=1)
    headers = page.get('turns') or []
    if not headers:
        return None
    return turns.get_turn(conversation_id, headers[-1]['turnId'], user_id=owner, include_projection=False)


def _appended(turn: Mapping) -> bool:
    """An appended settled record, such as a Coordinator report: it never ran on the lane."""
    return turn.get('kind') == 'ingested' or (turn.get('settlement') or {}).get('cause') == 'ingested'


def _later_turns(owner: int, conversation_id: str, turn: Mapping) -> list[dict]:
    """The Turns after ``turn`` on its lane, oldest first (read newest first, down to it)."""
    from lib import turn_lifecycle as turns
    later = []
    for row, _attempt in turns.lane_turns_newest_first(
            conversation_id, turn.get('laneId') or 'main', user_id=owner):
        if row.get('turnId') == turn.get('turnId') or row.get('ordinal', -1) <= turn.get('ordinal', -1):
            break
        later.append(row)
    return later[::-1]


def read_lane_tail_verdict(owner: int, conversation_id: str, turn: Mapping, *, interrupted_at=None) -> dict:
    """``lane_tail_verdict`` over the durable Turns after ``turn`` on its lane."""
    return lane_tail_verdict(turn, _later_turns(owner, conversation_id, turn), interrupted_at=interrupted_at)


def newest_work_turn(owner: int, conversation_id: str) -> tuple[dict | None, list[dict]]:
    """The newest main-lane Turn that did work and the Turns after it, oldest first.

    Appended records (Coordinator reports), queued pairs and input Turns never
    ran on the lane, so they are passed over: they neither describe nor settle
    its work. Restart resume and the Router's lead check (docs/ROUTER.md R9
    ``project_repair``) share this one rule.
    """
    from lib import turn_lifecycle as turns
    later = []
    for row, attempt in turns.lane_turns_newest_first(conversation_id, 'main', user_id=owner):
        if (row.get('actor') not in INPUT_ACTORS and not _appended(row)
                and not (attempt or {}).get('queueBinding')):
            return row, later[::-1]
        later.append(row)
    return None, later[::-1]


def _interrupted_turn(owner: int, conversation_id: str, turn_id: str | None) -> tuple[dict | None, list[dict]]:
    """The interrupted Turn and the Turns after it on its lane, oldest first.

    A Turn the interruption record names is read by identity. Without one it is
    the newest main-lane Turn that did work (``newest_work_turn``).
    """
    from lib import turn_lifecycle as turns
    if turn_id:
        try:
            turn = turns.get_turn(conversation_id, turn_id, user_id=owner, include_projection=False)
        except turns.LifecycleNotFound:
            return None, []
        return turn, _later_turns(owner, conversation_id, turn)
    return newest_work_turn(owner, conversation_id)


def _superseded(verdict: Mapping) -> str:
    return f'superseded by {"queued" if verdict["queued"] else "newer"} input {verdict["supersededBy"]}'


def _reply_state(later: list[dict], input_id: str) -> str:
    """How the reply to the superseding input stands, when the lane holds one."""
    after = False
    for row in later:
        if row.get('turnId') == input_id:
            after = True
        elif after and row.get('actor') not in INPUT_ACTORS and not _appended(row):
            return f'; its reply is {row.get("status")}'
    return ''


def _resume_accepted_turn(owner: int, conversation_id: str, turn: Mapping, option: Mapping,
                          config: Mapping, *, command_id: str, log_prefix: str) -> dict:
    """Issue the ordinary resume command exactly as the owner's Continue click."""
    from lib.conversation_sync.execution_identity import restore_execution_identity
    from lib.conversation_sync.runtime import conversation_turn_commands
    principal, _ = restore_execution_identity({**config, '_turnOwnerUserId': owner})
    body = {'commandId': command_id, 'operation': option['operation'],
            'expectedProjectionRevision': turn['projectionRevision'],
            'config': {name: deepcopy(value) for name, value in config.items() if not name.startswith('_')}}
    if option.get('anchor') is not None:
        # The exact anchor the settlement offers, as a browser Continue sends it.
        body['resumeAnchor'] = deepcopy(option['anchor'])
    result = conversation_turn_commands.create_attempt(
        conversation_id, turn['turnId'], owner, body, request_started_at=time.time(),
        principal=principal).value
    resumed = (result.get('attempt') or {}).get('attemptId')
    logger.info('%s %s resumed in place (%s, attempt %s%s)', log_prefix, conversation_id,
                option['operation'], resumed, ', replayed' if result.get('idempotentReplay') else '')
    return _result(owner, conversation_id, 'resumed', turnId=turn['turnId'], attemptId=resumed,
                   operation=option['operation'])


def resume_conversation(owner: int, conversation_id: str, *, turn_id: str | None = None,
                        attempt_id: str | None = None) -> dict:
    """Continue one conversation's restart-paused Turn in place, or say why not.

    ``turn_id``/``attempt_id`` name the interrupted Turn and Attempt (a quiesce
    record, or Coordinator recovery's request Turn); without them the newest
    main-lane Turn that did work stands in. A Turn a later input superseded is
    not resumed; a Turn whose current Attempt is not the interrupted one was
    already continued.
    """
    from lib import turn_lifecycle as turns
    from lib.project_dispatch.admission import turn_dispatch

    def skip(reason, outcome='not_resumed', **fields):
        return _result(owner, conversation_id, outcome, reason, **fields)

    turn, later = _interrupted_turn(owner, conversation_id, turn_id)
    if turn is None:
        return skip('its interrupted Turn no longer exists' if turn_id else 'no Turn on its main lane',
                    turnId=turn_id, attemptId=attempt_id)
    ids = {'turnId': turn.get('turnId'), 'attemptId': turn.get('currentAttemptId')}
    if (turn.get('laneId') or 'main') != 'main':
        return skip(f'it runs on lane {turn.get("laneId")}, not the main lane', **ids)
    if attempt_id and turn.get('currentAttemptId') != attempt_id:
        return skip(f'already continued in Attempt {turn.get("currentAttemptId")}', **ids)
    settlement = turn.get('settlement') or {}
    if turn.get('status') != 'interrupted':
        return skip(f'its Turn is {turn.get("status")}, not interrupted', **ids)
    if settlement.get('cause') != 'server_restart':
        return skip(f'interrupted by {settlement.get("cause") or "an unknown cause"}, not by the restart', **ids)
    attempt = turns.get_attempt(turn['currentAttemptId'], user_id=owner, include_execution_config=True)
    verdict = lane_tail_verdict(turn, later, interrupted_at=attempt.get('settledAt'))
    if verdict['supersededBy']:
        return skip(_superseded(verdict), 'superseded', supersededBy=verdict['supersededBy'], **ids)
    review = unknown_effects_to_review(turn)
    if review:
        return skip(describe_unknown_effects(review), unknownEffects=review, **ids)
    options = [option for option in settlement.get('resumeOptions') or () if isinstance(option, Mapping)]
    offered = [option.get('operation') for option in options]
    if 'answer_guidance' in offered:
        return skip('waiting for the owner\u2019s answer to its question (answer_guidance)', **ids)
    option = next((option for option in options if option.get('operation') in RESUME_OPERATIONS), None)
    if option is None:
        return skip('only ' + (', '.join(map(str, offered)) or 'nothing')
                    + ' is offered; regenerate would redo the work', **ids)
    config = attempt.get('config')
    if not isinstance(config, Mapping):
        return skip('its accepted execution config is unavailable', **ids)
    if turn_dispatch(conversation_id, turn, config, user_id=owner) is not None:
        return skip('a project dispatch grant runs it; project dispatch recovery continues it', **ids)
    if config.get('autopilot') is True:
        return skip('a Goal run owns it; Goal continuity resumes it', **ids)
    if any(config.get(key) for key in ('activeFlow', 'flowDefinition', 'flowBuiltin', 'flowId')):
        return skip('a Flow run owns it; Flow runs resume on request (orchestration continuity)', **ids)
    return _resume_accepted_turn(owner, conversation_id, turn, option, config,
                                 command_id='restart-resume:' + turn['currentAttemptId'],
                                 log_prefix='[RestartResume]')


def failed_notice_resume_option(turn: Mapping | None) -> tuple[Mapping | None, str]:
    """The existing resume option of one failed nonhuman notice review, or why not.

    This is a gate, never a verdict rewrite: the failed Turn keeps its status,
    error and evidence, and the returned option is exactly what its settlement
    already offered the owner. A valid ``[SILENT]`` declaration never reaches
    here (it settles completed); a Turn that did produce a deliverable answer
    is not an unreviewed notice either.
    """
    from lib.turn_initiation import (
        is_nonhuman_notice_turn, require_human_turn, steer_author_turn,
    )
    if not isinstance(turn, Mapping):
        return None, 'no Turn was supplied'
    if turn.get('status') != 'failed':
        return None, f'this Turn is {turn.get("status")}, not a failed notice review'
    if not turn.get('currentAttemptId'):
        return None, 'this failed Turn has no Attempt to resume'
    settlement = turn.get('settlement') or {}
    error = settlement.get('error') or {}
    if not (isinstance(error, Mapping) and error.get('kind') == 'abnormal_stop'
            and 'empty_stop' in str(error.get('raw') or '')):
        return None, ('only an exhausted empty_stop review is recoverable here; '
                      'other failures keep their own next action')
    projection = turn.get('projection') or {}
    if projection.get('content'):
        return None, 'this Turn already has an answer; it is not an unreviewed notice'
    if any(isinstance(segment, Mapping) and segment.get('noteKind') == 'deliberate-silence'
           for segment in projection.get('segments') or ()):
        return None, 'this notice was already declared reviewed; never resume it'
    source = (turn.get('parentTurn') or turn.get('_parentNoticeTurn')
              or turn.get('_noticeSourceTurn'))
    if not isinstance(source, Mapping) or not source.get('turnId'):
        return None, 'the notice input Turn is required to prove nonhuman authorship'
    if not is_nonhuman_notice_turn(source):
        return None, 'only a nonhuman notice resumes here; owner-authored Turns keep Continue'
    for record in projection.get('_userSteerInjects') or ():
        if not isinstance(record, Mapping) or not record.get('author'):
            continue
        try:
            require_human_turn(steer_author_turn(record['author']))
        except ValueError:
            continue
        return None, 'a current-Turn owner steer must be answered; it is not a silent notice'
    options = [option for option in settlement.get('resumeOptions') or () if isinstance(option, Mapping)]
    offered = [option.get('operation') for option in options]
    if 'answer_guidance' in offered:
        return None, 'this Turn waits for the owner\u2019s answer (answer_guidance)'
    option = next((option for option in options if option.get('operation') in RESUME_OPERATIONS), None)
    if option is None:
        return None, ('its settlement offers no continue/checkpoint resume; regenerate would '
                      'replay completed operations')
    return option, ''


def resume_failed_notice_turn(owner: int, conversation_id: str, turn_id: str | None = None) -> dict:
    """Resume one failed nonhuman notice review in place, preserving its evidence.

    Bounded terminal recovery (empty_stop) settles the review failed with no
    answer; without this the notice was stranded as an unread terminal card.
    The existing resume command continues the same Turn from its checkpoint,
    so completed tool results are replayed and never re-executed, and the
    command id ``notice-review-resume:<failed Attempt>`` makes a replayed pass
    idempotent. Nothing is marked read or reviewed; the review still has to
    produce its own terminal decision.
    """
    from lib import turn_lifecycle as turns

    def skip(reason, **fields):
        return _result(owner, conversation_id, 'not_resumed', reason, **fields)

    if turn_id is None:
        tail = _lane_tail(owner, conversation_id)
        turn_id = str((tail or {}).get('turnId') or '')
    if not turn_id:
        return skip('no Turn on its main lane')
    turn = turns.get_turn(conversation_id, turn_id, user_id=owner)
    ids = {'turnId': turn_id, 'attemptId': (turn or {}).get('currentAttemptId')}
    if not isinstance(turn, dict) or turn.get('actor') != 'assistant':
        return skip('this is not a failed notice review Turn', **ids)
    source = turns.get_turn(conversation_id, turn.get('parentTurnId'), user_id=owner) \
        if turn.get('parentTurnId') else None
    option, reason = failed_notice_resume_option({**turn, '_parentNoticeTurn': source})
    if option is None:
        return skip(reason, **ids)
    verdict = read_lane_tail_verdict(owner, conversation_id, turn)
    if verdict['supersededBy']:
        return skip(_superseded(verdict), **ids)
    attempt = turns.get_attempt(turn['currentAttemptId'], user_id=owner, include_execution_config=True)
    config = attempt.get('config')
    if not isinstance(config, Mapping):
        return skip('its accepted execution config is unavailable', **ids)
    return _resume_accepted_turn(owner, conversation_id, turn, option, config,
                                 command_id='notice-review-resume:' + turn['currentAttemptId'],
                                 log_prefix='[NoticeResume]')


def _log_summary(results: list[dict]) -> None:
    """One line naming every candidate: each resumed conversation, each skipped one with
    its reason. A skip is usually a hand-off (project dispatch recovery, Goal continuity)
    or a settled/superseded Turn; without the reason it reads as lost work."""
    resumed = [item['conversationId'] for item in results if item['outcome'] == 'resumed']
    skipped = [f"{item['conversationId']}: {item['reason'] or item['outcome']}"
               for item in results if item['outcome'] != 'resumed']
    logger.log(logging.WARNING if skipped else logging.INFO,
               '[RestartResume] summary: %d of %d interrupted conversation(s) resumed; '
               'resumed=[%s] skipped=[%s]', len(resumed), len(results),
               ', '.join(resumed), ', '.join(skipped))


def resume_restart_paused_turns(*, previous_shutdown=None, boot_recovered=None) -> list[dict]:
    """The post-ready pass of a server generation; one result per interrupted Turn.

    A transient storage failure keeps that candidate's quiesce record and is
    raised after the other candidates ran, so the boot step's existing retry
    repeats the pass; a repeat replays the same resume commands.
    """
    global _PASS_FINISHED, _LAST_RESULTS
    from lib.storage.errors import StorageError
    retry = None
    try:
        records = _quiesce_records()
        candidates = _candidates(records, _BOOT_RECOVERED if boot_recovered is None else boot_recovered)
        stand_down = _stand_down(previous_shutdown)
        results, unsettled = [], set()
        for candidate in candidates:
            owner, conversation_id = candidate['owner'], candidate['conversationId']
            ids = {'turnId': candidate['turnId'], 'attemptId': candidate['attemptId']}
            if stand_down:
                results.append(_result(owner, conversation_id, 'not_resumed', stand_down, **ids))
                continue
            try:
                results.append(resume_conversation(owner, conversation_id, turn_id=candidate['turnId'],
                                                   attempt_id=candidate['attemptId']))
            except Exception as error:
                if isinstance(error, StorageError) and error.retryable:
                    retry, unsettled = error, unsettled | {(owner, conversation_id)}
                logger.warning('[RestartResume] %s was not resumed: %s', conversation_id, error,
                               exc_info=not isinstance(error, (PermissionError, ValueError, StorageError)))
                results.append(_result(owner, conversation_id, 'not_resumed',
                                       f'resume failed: {type(error).__name__}: {error}', **ids))
        for record in records:
            if not any((int(item['owner']), str(item['conversationId'])) in unsettled
                       for item in record['conversations']):
                _delete_record(record)
        _LAST_RESULTS = results
        if results:
            _log_summary(results)
    finally:
        _PASS_FINISHED = True
    if retry is not None:
        raise retry
    return results


def describe_conversation(owner: int, conversation_id: str, since) -> str:
    """Where one conversation stands after a restart accepted at ``since`` (durable Turn state).

    It describes the interrupted Turn this process's pass selected, else the
    newest main-lane Turn that did work, never a report appended after it.
    """
    from lib import turn_lifecycle as turns
    selected = next((item for item in _LAST_RESULTS if item.get('owner') == owner
                     and item['conversationId'] == conversation_id and item.get('turnId')), {})
    turn, later = _interrupted_turn(owner, conversation_id, selected.get('turnId'))
    if turn is None:
        return 'no Turn found'
    status, settlement = turn.get('status'), turn.get('settlement') or {}
    attempt = (turns.get_attempt(turn['currentAttemptId'], user_id=owner)
               if turn.get('currentAttemptId') else {})
    operation = attempt.get('operation')
    after = isinstance(since, (int, float)) and (attempt.get('createdAt') or 0) / 1000 >= since
    resumed = operation in RESUME_OPERATIONS and after
    if status in ('pending', 'running'):
        if resumed:
            return f'resumed from its checkpoint ({operation}), running'
        return 'continuing in a newer Turn, running' if after else 'kept running through the restart'
    if status == 'interrupted' and settlement.get('cause') == 'server_restart':
        verdict = lane_tail_verdict(turn, later, interrupted_at=attempt.get('settledAt'))
        if verdict['supersededBy']:
            return (_superseded(verdict) + ', not resumed'
                    + _reply_state(later, verdict['supersededBy']))
        reason = selected.get('reason', '') if selected.get('outcome') == 'not_resumed' else ''
        return 'still interrupted, not resumed' + (f': {reason}' if reason else '')
    if resumed:
        return f'resumed from its checkpoint ({operation}), then {status}'
    settled = attempt.get('settledAt')
    if (not after and isinstance(settled, (int, float)) and isinstance(since, (int, float))
            and settled / 1000 < since):
        ended = {'completed': 'completed', 'failed': 'failed'}.get(status, f'was {status}')
        return f'not interrupted by this restart: its latest work {ended} before it'
    cause = settlement.get('cause')
    return f'{status} ({cause})' if cause else str(status)


__all__ = ['INPUT_ACTORS', 'RESTART_PAUSED_NAMESPACE', 'RESUME_OPERATIONS', 'automatic_resume_pause',
           'describe_conversation',
           'describe_unknown_effects', 'lane_tail_verdict', 'last_results', 'pass_finished',
           'read_lane_tail_verdict', 'record_quiesced_conversations', 'resume_conversation',
           'resume_recovered_turn', 'resume_restart_paused_turns', 'set_boot_recovered',
           'unknown_effects_to_review']
