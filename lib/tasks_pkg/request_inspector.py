"""Request Inspector — server-authoritative per-task request fold (P2).

Design: ``docs/DEBUG_PANEL_REDESIGN.md`` (row schemas FROZEN in §3.3 — do not
rename keys). The frontend drawer renders ONLY what this module folds from
the persisted ``task_events`` log (durable-before-visible, 6h TTL). The
in-browser ``_debugRequests`` log (P1) is a live accelerator with gaps —
``sse_poll_fallback.js`` never processes ``messages_snapshot``, so a poll
window drops rounds client-side; the server log never does. The server fold
is therefore the authority for both live and finished tasks.

Row shapes
==========
``fold_request_log(task_id)`` →
    ``{taskId, requests, states, coverage, eventsAvailable, requestCount}``
    Request row (metadata ONLY — never the payload):
    ``{roundNum, ts, model, params, messageCount, toolsCount,
       approxTokens, label, legacy, attempts}``
    Attempt row (joined from ``round_usage`` by roundNum):
    ``{tag, model, tokensIn, tokensOut, traceId, streamElapsedMs,
       cacheRead, cacheWrite, ts}``
    State row: ``{roundNum, label, messageCount, ts, legacy}``
``get_request_payload(task_id, round_num)`` → full payload for ONE round
    (messages + tools + params + model) — the on-demand detail fetch.
``list_conv_tasks(conv_id)`` → ``{convId, tasks}`` — Task rows for the
    drawer (live registry + task_results, exact kind-counted tallies via
    one json_extract GROUP BY — no payload bulk).

kind classification
===================
``kind=`` (the P1 emission contract) wins. Pre-contract persisted rows
carry no kind; ONLY for those legacy rows do we fall back to the
roundNum/label markers (migration shim — the contract itself never parses
labels; see design §3.1).
"""

from __future__ import annotations

import json

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

_SNAPSHOT = 'messages_snapshot'
_ROUND_USAGE = 'round_usage'
_STATE_ROUND_LABELS = ('final', 'fallback')
# Legacy-only state markers (pre-contract snapshots carried no kind=).
_LEGACY_STATE_LABEL_MARKERS = ('工具结果后', '最终回复后', 'Fallback')


def _row_get(r, key, idx):
    """Row access tolerant of mapping vs positional rows (mirrors
    event_log.read_events)."""
    try:
        if key in r.keys():
            return r[key]
    except Exception:
        pass
    return r[idx]


def _read_events(task_id: str) -> list:
    """Return [{event_id, type, payload, ts_ms}] ordered by event_id.

    Separate from ``event_log.read_events`` (which omits ``ts_ms`` — the
    request-row schema carries ``ts``). Read-only; never throws.
    """
    if not task_id:
        return []
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[RequestInspector] thread db unavailable: %s', e)
        return []
    try:
        rows = db.execute(
            'SELECT event_id, type, payload, ts_ms FROM task_events '
            'WHERE task_id=? ORDER BY event_id ASC LIMIT 10000',
            (task_id,)).fetchall()
    except Exception as e:
        logger.warning('[RequestInspector] read failed for task=%s: %s',
                       task_id[:8], e)
        return []
    out = []
    for r in rows:
        payload = _row_get(r, 'payload', 2)
        if not isinstance(payload, dict):
            try:
                payload = json.loads(payload or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
        try:
            out.append({
                'event_id': int(_row_get(r, 'event_id', 0)),
                'type': _row_get(r, 'type', 1),
                'payload': payload,
                'ts_ms': int(_row_get(r, 'ts_ms', 3) or 0),
            })
        except (TypeError, ValueError) as e:
            logger.debug('[RequestInspector] row skipped for task=%s: %s',
                         task_id[:8], e)
    return out


def _snapshot_kind(payload: dict) -> str:
    """'request' | 'state'. The P1 ``kind=`` contract wins; pre-contract
    rows (no kind) fall back to roundNum/label markers — the ONLY place
    label parsing is allowed (migration shim, NOT the contract)."""
    kind = payload.get('kind')
    if kind in ('request', 'state'):
        return kind
    rn = payload.get('roundNum')
    if isinstance(rn, str) and rn in _STATE_ROUND_LABELS:
        return 'state'
    label = payload.get('label') or ''
    if any(m in label for m in _LEGACY_STATE_LABEL_MARKERS):
        return 'state'
    return 'request'


def _msg_chars(msg: dict) -> int:
    """Char count of one message (mirrors the frontend _debugMsgChars)."""
    c = msg.get('content')
    n = 0
    if isinstance(c, str):
        n = len(c)
    elif isinstance(c, list):
        for b in c:
            if isinstance(b, dict):
                if b.get('type') == 'text':
                    n += len(b.get('text') or '')
                elif b.get('type') == 'image_url':
                    n += len((b.get('image_url') or {}).get('url') or '')
    for tc in (msg.get('tool_calls') or []):
        args = ((tc or {}).get('function') or {}).get('arguments')
        if isinstance(args, str):
            n += len(args)
        elif args:
            n += len(json.dumps(args))
    return n


def _est_tokens(messages: list) -> int:
    """Rough token estimate (chars/3.5, diagnostic not billing — mirrors
    the frontend _debugMsgTokens)."""
    chars = sum(_msg_chars(m) for m in messages if isinstance(m, dict))
    return max(1, round(chars / 3.5)) if chars else 0


def fold_request_log(task_id: str) -> dict:
    """Fold a task's persisted events into the Request Inspector rows.

    Request rows are METADATA-ONLY (no ``messages``/``tools`` bulk) —
    payloads are served on demand via :func:`get_request_payload`.
    """
    events = _read_events(task_id)
    requests = []
    states = []
    attempts: dict[str, list] = {}
    endpoint_seen = False
    for e in events:
        p = e['payload']
        et = e['type']
        if et == _SNAPSHOT:
            msgs = p.get('messages') or []
            if _snapshot_kind(p) == 'state':
                states.append({
                    'roundNum': p.get('roundNum'),
                    'label': p.get('label') or '',
                    'messageCount': len(msgs),
                    'ts': e['ts_ms'],
                    'legacy': 'kind' not in p,
                })
            else:
                requests.append({
                    'roundNum': p.get('roundNum'),
                    'ts': e['ts_ms'],
                    'model': p.get('model') or '',
                    'params': p.get('params') or {},
                    'messageCount': len(msgs),
                    'toolsCount': len(p.get('tools') or []),
                    'approxTokens': _est_tokens(msgs),
                    'label': p.get('label') or '',
                    'legacy': 'kind' not in p,
                })
        elif et == _ROUND_USAGE:
            u = p.get('usage') or {}
            try:
                from lib.cost import normalize_usage
                nu = normalize_usage(u)
            except Exception as _e:
                logger.debug('[RequestInspector] normalize_usage failed: %s', _e)
                nu = {}
            attempts.setdefault(str(p.get('roundNum')), []).append({
                'tag': p.get('tag') or '',
                'model': p.get('model') or '',
                'tokensIn': int(p.get('tokensIn') or 0),
                'tokensOut': int(p.get('tokensOut') or 0),
                'traceId': u.get('trace_id') or '',
                'streamElapsedMs': int(u.get('stream_elapsed_ms') or 0),
                'cacheRead': int(nu.get('cache_read') or 0),
                'cacheWrite': int(nu.get('cache_write') or 0),
                'ts': e['ts_ms'],
            })
        elif et.startswith('endpoint_'):
            # Endpoint-driven task: worker rounds run run_task (covered),
            # Planner/Critic turns call the LLM directly (NOT covered).
            endpoint_seen = True
    for row in requests:
        row['attempts'] = attempts.get(str(row['roundNum']), [])
    return {
        'taskId': task_id,
        'requests': requests,
        'states': states,
        'coverage': 'partial' if endpoint_seen else 'full',
        'eventsAvailable': bool(events),
        'requestCount': len(requests),
    }


def get_request_payload(task_id: str, round_num) -> dict | None:
    """Full payload for ONE request round (the on-demand detail fetch).

    Returns None when no request-kind snapshot exists for that round
    (expired log, state-only round, or unknown task).
    """
    best = None
    for e in _read_events(task_id):
        p = e['payload']
        if e['type'] != _SNAPSHOT or _snapshot_kind(p) != 'request':
            continue
        if str(p.get('roundNum')) != str(round_num):
            continue
        best = (e, p)  # last wins (a re-emitted round supersedes)
    if best is None:
        return None
    e, p = best
    return {
        'taskId': task_id,
        'roundNum': p.get('roundNum'),
        'ts': e['ts_ms'],
        'model': p.get('model') or '',
        'params': p.get('params') or {},
        'label': p.get('label') or '',
        'messages': p.get('messages') or [],
        'tools': p.get('tools') or [],
    }


def list_conv_tasks(conv_id: str, limit: int = 15) -> dict:
    """Task rows for the drawer: live chat registry + persisted
    task_results, newest first, each annotated with EXACT kind-counted
    snapshot tallies (one json_extract GROUP BY — translates to PG jsonb
    accessors via the dialect bridge, no payload bulk).

    VU sub-tasks run with convId='' and are therefore NOT returned here;
    they remain reachable per-task via the bubble anchor (P3).
    """
    rows: dict[str, dict] = {}
    try:
        from lib.tasks_pkg.manager import _chat_runtime
        with _chat_runtime._lock:  # type: ignore[attr-defined]
            live = [t for t in _chat_runtime._tasks.values()  # type: ignore[attr-defined]
                    if t.get('convId') == conv_id]
        for t in live:
            rows[t['id']] = {
                'taskId': t['id'],
                'status': t.get('status') or 'running',
                'createdAt': int(t.get('created_at', 0) * 1000),
                'completedAt': None,
                'live': True,
            }
    except Exception as e:
        logger.debug('[RequestInspector] live registry read failed: %s', e)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        trs = db.execute(
            'SELECT task_id, status, created_at, completed_at '
            'FROM task_results WHERE conv_id=? '
            'ORDER BY created_at DESC LIMIT ?',
            (conv_id, limit)).fetchall()
        for r in trs:
            tid = _row_get(r, 'task_id', 0)
            if tid in rows:
                continue
            rows[tid] = {
                'taskId': tid,
                'status': _row_get(r, 'status', 1),
                'createdAt': int(_row_get(r, 'created_at', 2) or 0),
                'completedAt': _row_get(r, 'completed_at', 3),
                'live': False,
            }
    except Exception as e:
        logger.warning('[RequestInspector] task_results read failed for '
                       'conv=%s: %s', (conv_id or '')[:8], e)
    tasks = sorted(rows.values(), key=lambda x: x['createdAt'] or 0,
                   reverse=True)[:limit]
    ids = [t['taskId'] for t in tasks]
    if ids:
        try:
            db = get_thread_db(DOMAIN_CHAT)
            placeholders = ','.join(['?'] * len(ids))
            counts = db.execute(
                "SELECT task_id, json_extract(payload, '$.kind') AS k, "
                "COUNT(*) AS n FROM task_events "
                f"WHERE task_id IN ({placeholders}) "
                "AND type='messages_snapshot' GROUP BY task_id, k",
                tuple(ids)).fetchall()
            by_id = {t['taskId']: t for t in tasks}
            for c in counts:
                row = by_id.get(_row_get(c, 'task_id', 0))
                if row is None:
                    continue
                k = _row_get(c, 'k', 1)
                n = int(_row_get(c, 'n', 2) or 0)
                if k == 'state':
                    row['stateCount'] = row.get('stateCount', 0) + n
                elif k == 'request':
                    row['requestCount'] = row.get('requestCount', 0) + n
                else:
                    # Pre-contract rows (no kind) — exact request/state split
                    # needs the label shim; surface as a legacy tally so the
                    # UI can mark the count approximate.
                    row['legacyCount'] = row.get('legacyCount', 0) + n
        except Exception as e:
            logger.debug('[RequestInspector] kind tally failed: %s', e)
    for t in tasks:
        t.setdefault('requestCount', 0)
        t.setdefault('stateCount', 0)
        t.setdefault('legacyCount', 0)
        t['hasEvents'] = bool(
            t['requestCount'] or t['stateCount'] or t['legacyCount'])
    return {'convId': conv_id, 'tasks': tasks}


__all__ = ['fold_request_log', 'get_request_payload', 'list_conv_tasks']
