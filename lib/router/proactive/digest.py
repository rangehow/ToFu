"""Digest: a deterministic daily summary for the owner, zero model requests.

Owner: docs/ROUTER.md R10 reporting rules. At the owner's digest time (default
21:00), assemble from durable records: open decisions (blocking first, then
oldest), overdue commitments, open faults, verified deliveries since the last
digest, projects active but silent, and daily figures. Skipped when empty.
Delivered through the outbox to every enabled command chat (大象, 微信, …), or
as an in-app report Turn in the Global Router conversation.

Entry points: assemble_digest, run_digest.
Dependencies: router.proactive.questions, conversations.coordinator_progress,
router.watchdog.signals, channels.store, channels.outbox,
conversations.coordinator_reports.
"""
from __future__ import annotations

from datetime import datetime
import time

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)


def _fault_section(signals):
    """Open faults grouped by producer and error kind, so repeats do not flood the digest."""
    groups = {}
    for signal in signals:
        error = signal.get('error') or {}
        key = (str(signal.get('source') or '?'), str(error.get('kind') or '?'))
        group = groups.setdefault(key, {'count': 0, 'detail': '', 'conversations': set()})
        group['count'] += 1
        if not group['detail']:
            group['detail'] = str(error.get('detail') or '')
        subject = signal.get('subject') or {}
        if subject.get('conversationId'):
            group['conversations'].add(str(subject['conversationId']))
    if not groups:
        return None
    lines = [f'**{len(signals)} open fault(s), {len(groups)} group(s):**']
    for (source, kind), group in sorted(groups.items(), key=lambda item: (-item[1]['count'], item[0])):
        scope = f', {len(group["conversations"])} conversation(s)' if len(group['conversations']) > 1 else ''
        detail = group['detail'][:120]
        lines.append(f'- {source} ×{group["count"]} ({kind}{scope})' + (f': {detail}' if detail else ''))
    return '\n'.join(lines)


def assemble_digest(owner, *, client=None, now=None, task=None):
    """Build the digest text from durable records. Returns (text, counts) or (None, {})."""
    owner = require_user_id(owner, context='digest')
    from lib.router.proactive.questions import open_questions
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.router.projects import router_projects

    now = now or datetime.now()
    sections = []

    # 1. Open decisions (blocking first, then oldest).
    questions = open_questions(owner, client=client)
    if questions:
        blocking = [q for q in questions if q.get('blocking')]
        nonblocking = [q for q in questions if not q.get('blocking')]
        ordered = blocking + nonblocking
        lines = [f'**{len(questions)} decision(s) waiting for you:**']
        for q in ordered:
            age_h = int((time.time() * 1000 - q.get('createdAt', 0)) / 3_600_000)
            age = f'{age_h}h' if age_h < 48 else f'{age_h // 24}d'
            prefix = '⏸ ' if q.get('blocking') else '  '
            lines.append(f'{prefix}{q["question"]} ({age})')
            if q.get('default'):
                lines.append(f'   Default: {q["default"]}')
        sections.append('\n'.join(lines))

    # 2. Overdue commitments.
    overdue = []
    try:
        projects = router_projects(owner, {}, client=client)
        for entry in projects.get('projects', []):
            if entry.get('availability') != 'available':
                continue
            progress = CoordinatorTaskProgress(owner, entry['project_path'], client=client)
            row = progress._head()
            if row is None:
                continue
            head = row['value']
            for receipt in head.get('commitments', []):
                if receipt['state'] != 'open':
                    continue
                if receipt['dueAt'] < time.time() * 1000 and 'wake' in receipt:
                    overdue.append({'title': receipt['title'], 'project': entry['project_path'],
                                    'dueAt': receipt['dueAt'], 'slips': receipt.get('slips', 0)})
    except Exception:
        logger.warning('[Digest] commitment scan failed', exc_info=True)
    if overdue:
        lines = [f'**{len(overdue)} overdue promise(s):**']
        for item in overdue:
            from lib.conversations.coordinator_commitments import iso_ms
            lines.append(f'- "{item["title"]}" — was due {iso_ms(item["dueAt"])}'
                         + (f' (moved {item["slips"]}x)' if item['slips'] else ''))
        sections.append('\n'.join(lines))

    # 3. Open faults (Watchdog signals), across every open-signal page.
    faults = []
    try:
        from lib.router.watchdog.signals import FaultSignals
        from lib.router.watchdog.binding import router_watchdog
        watchdog = router_watchdog(owner, create=False, client=client)
        if watchdog['availability'] == 'available':
            faults = list(FaultSignals(owner, client=client).iter_open())
            section = _fault_section(faults)
            if section:
                sections.append(section)
    except Exception:
        logger.debug('[Digest] fault scan unavailable', exc_info=True)

    # 4. Verified deliveries since the last digest.
    deliveries = []
    try:
        for entry in projects.get('projects', []):
            last_report = entry.get('last_report')
            if last_report and last_report.get('kind') == 'delivery' and last_report.get('unread'):
                deliveries.append({'title': last_report['title'], 'project': entry['project_path']})
    except Exception as error:
        logger.debug('[Digest] verified-delivery scan unavailable: %s', error)
    if deliveries:
        lines = [f'**{len(deliveries)} verified delivery(ies):**']
        for item in deliveries:
            lines.append(f'- {item["title"]}')
        sections.append('\n'.join(lines))

    # 5. Projects active but silent (no report in the last 24h).
    silent = []
    try:
        for entry in projects.get('projects', []):
            if entry.get('active_workers', 0) > 0 and not entry.get('last_report'):
                silent.append(entry['project_path'].rsplit('/', 1)[-1])
    except Exception as error:
        logger.debug('[Digest] active-but-silent scan unavailable: %s', error)
    if silent:
        sections.append(f'**Active but silent:** {", ".join(silent)}')

    # 6. Portfolio value: approvals only the owner can give, starts with their
    # promised outcomes, and measured (or negative) results since the last digest.
    try:
        from lib.router.portfolio.view import portfolio_digest
        previous_run = (task or {}).get('last_run')
        section, portfolio_counts = portfolio_digest(owner, client=client, since=previous_run)
        if section:
            sections.append(section)
    except Exception:
        logger.debug('[Digest] portfolio section unavailable', exc_info=True)
        portfolio_counts = {}

    if not sections:
        return None, {}
    text = f'📋 Daily digest — {now.strftime("%Y-%m-%d %H:%M")}\n\n' + '\n\n'.join(sections)
    counts = {'questions': len(questions), 'overdue': len(overdue), 'faults': len(faults),
              'deliveries': len(deliveries), 'silent_projects': len(silent),
              **{f'portfolio_{key}': value for key, value in portfolio_counts.items()}}
    return text, counts


def _push_command_chats(owner, client):
    """Every enabled channel's command chat: enabling an account chooses a push channel."""
    from lib.channels.model import CHANNEL_TRAITS
    from lib.channels.store import ChannelStore
    store = ChannelStore(owner) if client is None else ChannelStore(owner, client=client)
    chats = []
    for channel, traits in CHANNEL_TRAITS.items():
        if not traits.get('commandChat'):
            continue
        account = store.account(channel) or {}
        if account.get('enabled') and account.get('identity') and account.get('commandChat'):
            chats.append((channel, str(traits.get('label') or channel), str(account['commandChat'])))
    return chats


def run_digest(task, *, client=None, now=None):
    """Scheduler handler for the daily digest. Returns (ok, result)."""
    owner = require_user_id(task.get('user_id'), context='digest')
    text, counts = assemble_digest(owner, client=client, now=now, task=task)
    if text is None:
        return True, 'Nothing to digest; skipped.'
    # Deliver through the outbox to every enabled command chat; a failing
    # channel never blocks the others, and no channel falls back in-app.
    day = (now or datetime.now()).strftime('%Y-%m-%d')
    from lib.channels.outbox import enqueue
    sent, failed = [], []
    for channel, label, command_chat in _push_command_chats(owner, client):
        try:
            enqueue(owner, command_chat, text,
                    idempotency_key=f'digest:{owner}:{day}:{channel}',
                    origin={'initiator': 'system', 'kind': 'digest'},
                    authority='owner')
            sent.append(label)
        except Exception:
            logger.warning('[Digest] outbox enqueue failed channel=%s', channel, exc_info=True)
            failed.append(label)
    if sent:
        result = f'Digest sent to {", ".join(sent)}: {counts}'
        if failed:
            result += f' (failed: {", ".join(failed)})'
        return True, result
    # In-app fallback: a report Turn in the Global Router conversation.
    from lib.router.binding import global_router
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.router.home import router_home_path
    router = global_router(owner, create=False, client=client)
    if router['availability'] != 'available':
        return False, 'No Global Router and no enabled command chat; digest not delivered.'
    home = router_home_path(owner)
    reports = CoordinatorReports(owner, home, client=client)
    scope = {'owner': owner, 'projectPath': home,
             'coordinatorId': router['conversationId'], 'bindingRevision': router['revision']}
    reports.publish(scope, {'kind': 'progress', 'title': f'Daily digest {day}',
                            'message': text, 'idempotency_key': f'digest:{owner}:{day}'})
    result = f'Digest published in-app: {counts}'
    if failed:
        result += f' (failed: {", ".join(failed)})'
    return True, result


__all__ = ['assemble_digest', 'run_digest']
