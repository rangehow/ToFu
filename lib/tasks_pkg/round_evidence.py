"""Shared root/child Inspector evidence construction; persistence stays with callers.

The existing event-log delta writer owns size bounds and incremental storage.
A request-specific turn token makes the existing authorized payload API exact,
including restarted invocations with the same displayed round number.
"""
from __future__ import annotations

from uuid import uuid4

from lib.agent_core.events import EventType, build_event


def begin_round_evidence(task: dict, task_id: str, round_num: int,
                         phase: str = '') -> dict:
    previous = task.get('_inspectorRef') or {}
    turn = previous.get('turn') if (
        previous.get('taskId') == task_id
        and round_num > previous.get('roundNum', 0)) else None
    ref = {'taskId': task_id, 'roundNum': round_num,
           'turn': turn or f'{phase or "request"}:{uuid4().hex}'}
    task['_inspectorRef'] = ref
    return dict(ref)


def round_evidence_turn(task: dict, round_num: int | None) -> str:
    """Join diagnostics only to the current task's exact round reference.

    Legacy callers without a matching reference retain their phase identity.
    Never borrow a stale round or another task's Inspector reference.
    """
    ref = task.get('_inspectorRef') or {}
    task_id = task.get('_inspectorTaskId') or task.get('id')
    if (round_num is not None and ref.get('roundNum') == round_num
            and ref.get('taskId') == task_id and ref.get('turn')):
        return str(ref['turn'])
    return str(task.get('_flow_phase') or '')


_SNAPSHOT_ROW_KEY = ('round-evidence', 'snapshot-row')
# Fields the snapshot projection removes (``_strip_base64_for_snapshot``).
_SNAPSHOT_DROPPED_FIELDS = ('_responses_items', '_anthropic_content_blocks', '_tofuSameRoleSeam')


def _projects_to_itself(message) -> bool:
    """Whether the snapshot projection of a settled row equals the row.

    True when nothing the projection rewrites is present: no dropped sidecar,
    no image block, no text over its 100,000-character cut and no tool-call
    arguments over their 50,000-character cut. The projection then copies
    the containers without changing a value.
    """
    if any(field in message for field in _SNAPSHOT_DROPPED_FIELDS):
        return False
    content = message.get('content')
    if isinstance(content, str):
        if len(content) > 100000:
            return False
    elif isinstance(content, list):
        if any(isinstance(block, dict) and block.get('type') == 'image_url'
               for block in content):
            return False
    calls = message.get('tool_calls')
    if calls is not None:
        if not isinstance(calls, list):
            return False
        for call in calls:
            if not isinstance(call, dict):
                return False
            arguments = (call.get('function') or {}).get('arguments', '')
            if isinstance(arguments, str) and len(arguments) > 50000:
                return False
    return True


def _snapshot_rows(wire: list, project) -> list:
    """``project(wire)``; a settled request row projects once into a settled twin.

    The snapshot projection of one message is a pure function of it, so a
    settled row of the canonical request (``build_round_request``) keeps its
    settled projection in its memo slot and unchanged history is not copied.
    """
    from lib.conversation_sync.settled_rows import is_settled, memo_get, memo_put, settle_or_none
    rows = []
    for message in wire:
        if not is_settled(message):
            rows.extend(project([message]))
            continue
        if _projects_to_itself(message):
            rows.append(message)
            continue
        held = memo_get(message, _SNAPSHOT_ROW_KEY)
        if held is None:
            projected = project([message])[0]
            settled = settle_or_none(projected)
            if settled is None:
                rows.append(projected)
                continue
            held = memo_put(message, _SNAPSHOT_ROW_KEY, settled)
        rows.append(held)
    return rows


def build_round_evidence(task: dict, messages: list, *, ref: dict,
                         kind: str, model: str, prepared_messages=None,
                         params=None, tools=None, prepared_lineage=None) -> dict:
    from lib.conversation_sync.settled_rows import settle_items
    from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
    from lib.tasks_pkg.wire_messages import apply_wire_sanitize
    canonical = isinstance(prepared_messages, list)
    lineage = prepared_lineage if canonical else {}
    wire = prepared_messages if canonical else apply_wire_sanitize(
        messages, conv_id=task.get('convId', ''),
        provider_id=task.get('provider_id') or '',
        user_id=task.get('_userId') or None, lineage=lineage)
    # Snapshot messages are settled and an unchanged message keeps the previous
    # snapshot's twin, whose memoized digest the event-log delta projector
    # (snapshot_delta) reuses instead of re-encoding the whole history.
    # Each kind settles against its own previous snapshot: the request wire
    # carries injections and cache markers at positions the post-tool state
    # lacks, so alternating kinds left no row interchangeable at its position.
    previous_key = '_snapshotMessages' if kind == 'request' else '_stateSnapshotMessages'
    snapshot = settle_items(_snapshot_rows(wire, _strip_base64_for_snapshot),
                            previous=task.get(previous_key))
    task[previous_key] = snapshot
    label = '请求前' if kind == 'request' else '工具结果后'
    event = build_event(
        EventType.MESSAGES_SNAPSHOT, kind=kind, model=model,
        roundNum=ref['roundNum'], turn=ref['turn'], inspectorRef=dict(ref),
        label=f'Round {ref["roundNum"]} {label} · {len(snapshot)}条',
        messages=snapshot, contextManifest=list(task.get('_contextManifest') or []))
    if lineage and lineage.get('sources'):
        from lib.llm_sanitize.lineage import copy_trace, verify_lineage
        event['contextLineage'] = copy_trace(lineage)
        verify_lineage(event['contextLineage'], {'messages': snapshot}, 'snapshot_projection')
    if params is not None:
        event['params'] = params
    if tools:
        # Settled against the previous snapshot's schemas of the same kind, so
        # the unchanged catalog is shared by the frame's detachment, not copied.
        tools_key = '_snapshotTools' if kind == 'request' else '_stateSnapshotTools'
        event['tools'] = task[tools_key] = settle_items(
            list(tools), previous=task.get(tools_key))
    if kind == 'request' and not canonical:
        event.update(degraded=True, degradedReason='request-build-fallback')
    # Watermark for the reduced-scope post-tool dedup (FINDING C): the
    # request snapshot for this round just sanitized + copied the full wire.
    # A post-tool snapshot over an unchanged tail would be byte-equal, so the
    # pipeline skips re-copying it. Identity is length + id() of the tail
    # object — enough to prove no append/edit without another O(history) scan.
    task['_snapshotWatermark'] = {
        'roundNum': ref['roundNum'], 'len': len(messages),
        'tail_id': id(messages[-1]) if messages else 0,
    }
    return event


def snapshot_unchanged_since_request(task: dict, messages: list) -> bool:
    """True when the message list is unchanged since this round's snapshot.

    Conservative identity check used to skip a duplicate post-tool snapshot:
    same recorded round, same list length, and the tail message object is the
    identical object the request snapshot copied. Any append, edit, or
    compaction changes one of those and forces a fresh snapshot.
    """
    wm = task.get('_snapshotWatermark')
    if not isinstance(wm, dict):
        return False
    ref = task.get('_inspectorRef') or {}
    if wm.get('roundNum') != ref.get('roundNum'):
        return False
    if wm.get('len') != len(messages):
        return False
    tail_id = id(messages[-1]) if messages else 0
    return wm.get('tail_id') == tail_id
