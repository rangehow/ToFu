"""Watchdog health snapshot: deterministic, owner-scoped facts for one review.

Responsibility: project what existing owners already record into one bounded
view, with no model call and no side effect: the Global Router and a page of
Project Routers (availability, last activity, open signals, the main lane's
newest Turn, whether the lead is stalled) with only the Workers that need their
lead (objective, phase, last activity time and the settled Turn's error) and a
count of the running ones; outbox items in ``failed`` or ``unknown``; channel
account and bridge health; and a page of open fault signals. Pages follow the
project dispatch history page bound, like get_router_projects; Workers come
from each project's newest history page.

A lead is stalled (docs/ROUTER.md R7) when its main lane is idle (the newest
main-lane Turn is settled) while a Worker of its binding needs it: a result
awaiting review, a held delivery, or a Worker that ended after the lead's newest
Turn settled. Structural facts only, no time threshold; ``lead_router_entry``
is the one predicate the snapshot and ``resume_stalled_continuation`` share.

Entry points: health_snapshot(owner, *, after_id=None, signals_after_id=None,
client=None), lead_router_entry(owner, project, binding), lead_lane(owner,
conversation_id), needs_review(snapshot).
Dependencies: project_coordination, project_dispatch (history, continuation
summaries), turn_lifecycle, channels (store, each channel's supervisor), router.home,
router.watchdog (binding, signals).
"""
from __future__ import annotations

from collections.abc import Mapping
import time

from lib.storage.errors import StorageError

from .signals import FaultSignals, UNHEALTHY_CHANNEL_STATES, _bounded

# The supervisor hosting each built-in contract channel (channels_v1 Channel
# enum); an overlay channel's comes from its installed adapter
# (lib.channels.adapters), e.g. 大象.
CHANNEL_SUPERVISORS = {'github': 'lib.channels.github.supervisor',
                       'wecom': 'lib.channels.wecom.supervisor', 'qq': 'lib.channels.qq.supervisor',
                       'wechat': 'lib.channels.wechat.supervisor'}


def _last_activity(owner: int, conversation_id: str, client) -> int | None:
    from lib.conversations.repository import ConversationRepository
    snapshot = ConversationRepository(client).get(conversation_id, user_id=owner, include_messages=False)
    metadata = getattr(snapshot, 'metadata', None) or {}
    value = metadata.get('updated_at')
    return value if isinstance(value, int) else None


def _turn_facts(repository, conversation_id: str, turn_id: str) -> dict:
    turn = repository.turn_get(conversation_id, turn_id, user_id=repository.owner,
                               include_projection=False)
    facts = {'last_activity_at': turn.get('updatedAt')}
    error = (turn.get('settlement') or {}).get('error')
    if isinstance(error, Mapping) and error.get('kind'):
        facts['error'] = {'kind': str(error['kind'])[:64], 'detail': _bounded('detail', error.get('detail')),
                          **({'hint': _bounded('hint', error['hint'])} if error.get('hint') else {})}
    return facts


# Worker phases (continuation.worker_phase) that need their lead. Each counts only when it
# is new to the lead: its result (or held receipt) settled after the lead's newest Turn did.
# An idle lead that already saw a result and left it is not stalled; its continuation's own
# stall rule owns that choice.
_NEEDS_LEAD = frozenset({'needs_review', 'held', 'ended', 'interrupted'})


def _settled_ms(attempt) -> int | None:
    stamp = attempt.get('settledAt') if isinstance(attempt, Mapping) else None
    return stamp if isinstance(stamp, int) else None


def lead_lane(owner: int, conversation_id: str) -> dict:
    """The lead's newest main-lane Turn: idle when it is settled (or there is none)."""
    from lib.turn_lifecycle import TERMINAL_STATUSES, get_attempt, iter_turns_newest_first
    latest = next((turn for turn in iter_turns_newest_first(conversation_id, user_id=owner, light=True)
                   if turn.get('laneId', 'main') == 'main'), None)
    if latest is None:
        return {'idle': True, 'turn_id': None, 'status': None, 'settled_at': None}
    idle = latest.get('status') in TERMINAL_STATUSES
    settled_at = None
    if idle:
        attempt = get_attempt(latest['currentAttemptId'], user_id=owner) if latest.get('currentAttemptId') else None
        settled_at = _settled_ms(attempt) or latest.get('updatedAt')
    return {'idle': idle, 'turn_id': latest.get('turnId'), 'status': latest.get('status'),
            'settled_at': settled_at}


def _result_settled_at(repository, row, phase: str) -> int | None:
    """When this Worker's result reached its lead: the held receipt's write, else the Attempt's settlement."""
    attempt = repository.get_attempt(row['execution']['attemptId']) if row.get('execution') else None
    if phase == 'held':
        from lib.project_dispatch.publication import delivery_workspace
        workspace = delivery_workspace(row, attempt)
        if workspace is not None:
            found = repository.get_workspace(workspace[0], workspace[1], user_id=repository.owner)
            stamp = (((found or {}).get('origin') or {}).get('delivery') or {}).get('updatedAt')
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                return int(stamp * 1000)
    return _settled_ms(attempt)


def _workers(owner: int, project: str, binding: dict, lane: dict, client) -> dict:
    """Workers of this binding epoch that need their lead, from the newest history page."""
    from lib.project_dispatch.continuation import worker_phase, worker_summaries
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = (ProjectDispatchRepository(owner, project) if client is None
                  else ProjectDispatchRepository(owner, project, client=client))
    page = repository.history_page(None)
    rows = [row for row in page['rows']
            if row['request']['coordinatorId'] == binding['conversationId']
            and row['grant'].get('bindingRevision') == binding['revision']
            and row['request'].get('profile', {}).get('mode') != 'coordinator']
    workers, running = [], 0
    for row, summary in zip(rows, worker_summaries(repository, rows)):
        phase = worker_phase(summary)
        if phase == 'running':
            running += 1
        # A running lead handles its own Workers.
        if phase not in _NEEDS_LEAD or not lane['idle']:
            continue
        settled_at = _result_settled_at(repository, row, phase)
        if lane['settled_at'] is not None and (settled_at is None or settled_at <= lane['settled_at']):
            continue
        entry = {'assignment_id': summary['assignment_id'], 'conversation_id': summary['conversation_id'],
                 'objective': summary['title'], 'status': summary['status'], 'phase': phase,
                 'request_turn_id': row['request']['authorizationSource']['turnId']}
        delivery = summary.get('delivery') or {}
        if phase == 'held':
            entry['delivery'] = {'status': delivery['status'], 'reason': delivery.get('reason') or ''}
        execution = row.get('execution')
        if execution:
            entry.update(_turn_facts(repository, summary['conversation_id'], execution['turnId']))
        entry['result_settled_at'] = settled_at
        workers.append(entry)
    return {'workers': workers, 'running_workers': running,
            'workers_coverage': 'complete' if page.get('olderCursor') is None else 'newest_page'}


def _router(owner: int, project: str, binding: dict, signals: list[dict], client) -> dict:
    entry = {'project_path': project, 'conversation_id': binding['conversationId'],
             'binding_revision': binding['revision'], 'availability': binding['availability'],
             'open_signal_ids': [signal['signalId'] for signal in signals
                                 if signal['subject'].get('conversationId') == binding['conversationId']]}
    if binding['availability'] != 'available':
        return entry
    try:
        entry['last_activity_at'] = _last_activity(owner, binding['conversationId'], client)
        lane = lead_lane(owner, binding['conversationId'])
        entry['lead_turn'] = {key: lane[key] for key in ('turn_id', 'status', 'settled_at')}
        entry.update(_workers(owner, project, binding, lane, client))
    except (StorageError, ValueError, PermissionError) as error:
        # One damaged project must not hide the others; the gap is explicit.
        entry['activity_error'] = str(error)
        return entry
    if lane['idle'] and entry['workers']:
        phases = sorted({worker['phase'] for worker in entry['workers']})
        entry['stalled'] = True
        entry['stall_reason'] = (
            f'The lead\u2019s main lane is idle (newest Turn {lane["turn_id"] or "none"} is '
            f'{lane["status"] or "absent"}) while {len(entry["workers"])} Worker(s) need it '
            f'({", ".join(phases)}).')
    return entry


def lead_router_entry(owner: int, project: str, binding: dict, *, client=None) -> dict:
    """One lead's snapshot entry, read fresh from durable state (``stalled`` when it is)."""
    return _router(owner, project, binding, [], client)


def _channels(owner: int, client) -> tuple[list[dict], list[dict]]:
    import importlib
    from lib.channels.model import CHANNELS
    from lib.channels.store import ChannelStore
    store = ChannelStore(owner) if client is None else ChannelStore(owner, client=client)
    channels = []
    for channel in CHANNELS:
        account = store.account(channel)
        if not account:
            continue
        status = account.get('status') or {}
        if channel in CHANNEL_SUPERVISORS:
            supervisor = importlib.import_module(CHANNEL_SUPERVISORS[channel])
        else:
            from lib.channels.adapters import channel_adapter
            adapter = channel_adapter(channel)
            if adapter is None:
                continue
            supervisor = adapter.supervisor
        channels.append({'channel': channel, 'enabled': bool(account.get('enabled')),
                         'state': status.get('state') or 'disabled', 'detail': status.get('detail') or '',
                         'since_ms': status.get('updatedAtMs'), 'connected_at_ms': status.get('connectedAtMs'),
                         'bridge': supervisor.bridge_state(owner)})
    outbox = [{'item_id': item.id, 'thread': item.thread, 'state': item.state, 'error': item.error or '',
               'attempt_at_ms': item.attempt_at_ms, 'queued_at_ms': item.queued_at_ms}
              for item in store.active_outbox() if item.state in ('failed', 'unknown')] if channels else []
    return channels, outbox


def health_snapshot(owner: int, *, after_id: str | None = None, signals_after_id: str | None = None,
                    client=None) -> dict:
    """One page of the owner's router health; the first page also carries the Global Router."""
    from lib.conversations.project_coordination import NAMESPACE, ProjectCoordinationRepository
    from lib.project_dispatch.history import PAGE_ROWS
    from lib.router.home import is_router_home, router_home_location
    from .binding import is_watchdog_location
    bindings = (ProjectCoordinationRepository(owner) if client is None
                else ProjectCoordinationRepository(owner, client=client))
    owner = bindings.user_id
    storage = client
    if storage is None:
        from lib.storage import get_storage_client
        storage = get_storage_client
    signal_page = FaultSignals(owner, client=client).open_page(signals_after_id)
    signals = signal_page['signals']
    snapshot = {'generated_at': int(time.time() * 1000), 'global_router': None}
    if after_id is None:
        home = router_home_location(owner)
        router = bindings.read(home)
        if router['availability'] != 'unbound':
            snapshot['global_router'] = _router(owner, home, router, signals, client)
    prefix = f'{owner}:'
    rows = storage().query('record.list', {'namespace': NAMESPACE, 'prefix': prefix,
                                           'after_key': prefix + after_id if after_id else '',
                                           'limit': PAGE_ROWS + 1})
    if not isinstance(rows, list) or len(rows) > PAGE_ROWS + 1:
        raise StorageError('database_integrity', 'Invalid coordination binding page')
    projects = []
    for row in rows[:PAGE_ROWS]:
        value = row.get('value') if isinstance(row, Mapping) else None
        if (not isinstance(row.get('key'), str) or not row['key'].startswith(prefix)
                or not isinstance(value, Mapping) or value.get('user_id') != owner
                or not isinstance(value.get('projectPath'), str)):
            raise StorageError('database_integrity', 'Coordination binding page escaped its owner')
        project = value['projectPath']
        if is_router_home(owner, project) or is_watchdog_location(owner, project):
            continue
        projects.append(_router(owner, project, bindings.read(project), signals, client))
    snapshot['projects'] = projects
    snapshot['next_cursor'] = rows[PAGE_ROWS - 1]['key'][len(prefix):] if len(rows) > PAGE_ROWS else None
    snapshot['channels'], snapshot['outbox'] = _channels(owner, client)
    snapshot['open_signals'] = signals
    snapshot['open_signals_next_cursor'] = signal_page['next_cursor']
    return snapshot


def needs_review(snapshot: dict) -> dict:
    """Counts that make a periodic review worth a model turn; all zero means skip."""
    routers = ([snapshot['global_router']] if snapshot.get('global_router') else []) + snapshot['projects']
    # Healthy progress is not a fault: a running lead handles its own Workers.
    return {
        'stalled_leads': sum(bool(router.get('stalled')) for router in routers),
        'open_signals': len(snapshot['open_signals']) + bool(snapshot['open_signals_next_cursor']),
        'outbox_attention': len(snapshot['outbox']),
        'unhealthy_channels': sum(channel['enabled'] and channel['state'] in UNHEALTHY_CHANNEL_STATES
                                  for channel in snapshot['channels']),
        'unreadable_routers': sum('activity_error' in router for router in routers),
    }


__all__ = ['CHANNEL_SUPERVISORS', 'health_snapshot', 'lead_lane', 'lead_router_entry', 'needs_review']
