"""Persisted SSE event log — durable Last-Event-ID replay.

Every event that goes through ``manager.append_event`` is mirrored into the
``task_events`` table.  This decouples event replay from in-memory task
state, so SSE reconnection survives:

  * task removal by ``cleanup_old_tasks`` (1h threshold)
  * server restart
  * cross-process readers (when a future deployment fans tasks across
    multiple Flask workers)

Two read paths exist on the SSE handler:

  1. **Hot path** — when the task is still in ``tasks`` dict, replay reads
     directly from ``task['events']`` (no DB hit, lower latency).
  2. **Cold path** — when the task is gone (cleanup or restart), the SSE
     handler falls back to ``read_events`` here.

Pruning is opportunistic: every Nth ``append_event`` call performs a TTL
sweep on the table, deleting rows whose task is terminal and older than
``EVENT_TTL_MS``.  This keeps the table bounded without a background
thread.
"""

import json
import random
import threading
import time

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

# 6 hours — generous enough to span any realistic SSE reconnect window
# (page refresh, network blip, proxy timeout) for a finished task.
EVENT_TTL_MS = 6 * 3600 * 1000

# Coalesce successive content/thinking deltas into a single row to keep
# the table bounded for token-level streams.  Final flush happens when:
#   - 250 ms elapses since the last delta of this kind
#   - a non-delta event arrives (forcing flush)
#   - the task transitions to terminal state
_DELTA_FLUSH_MS = 250

# Sample-based pruning: every ~Nth call runs a TTL sweep
_PRUNE_PROBABILITY = 1 / 1024

_pending_lock = threading.Lock()
# task_id -> {'kind': 'content'|'thinking', 'text': str, 'first_ts': float}
_pending_deltas = {}


def _row_payload_to_json(payload):
    """Serialize a payload dict for storage; tolerant of non-dict events."""
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[EventLog] payload serialize failed: %s', e)
        return json.dumps({'type': 'error', 'detail': 'unserializable'})


def _flush_pending_locked(task_id, db):
    """Caller holds _pending_lock. Persist the buffered delta if any.

    The merged row is stored under the LAST coalesced event_id so a cold-path
    client reconnecting with Last-Event-ID=N will only filter it out when
    N >= last_id — i.e. they already saw everything inside it. This trades
    occasional content re-delivery for avoiding silent loss.
    """
    p = _pending_deltas.pop(task_id, None)
    if not p or not p.get('text'):
        return
    payload = {'type': 'delta', p['kind']: p['text']}
    eid = p.get('last_event_id', p['event_id'])
    try:
        db_execute_with_retry(
            db,
            'INSERT OR IGNORE INTO task_events (task_id, event_id, ts_ms, type, payload) VALUES (?,?,?,?,?)',
            (task_id, eid, int(p['first_ts'] * 1000),
             'delta', _row_payload_to_json(payload))
        )
    except Exception as e:
        logger.debug('[EventLog] flush delta failed for task=%s: %s', task_id[:8], e)


def append_persistent_event(task_id, event_id, event):
    """Persist one event to the task_events table.

    Coalesces consecutive same-kind delta events (content↔content,
    thinking↔thinking) within a 250 ms window to reduce row count for
    token-streaming workloads.  All other event types flush any pending
    delta first and then write themselves.

    This function MUST be cheap — it runs on every SSE delta.  It uses
    the per-thread DB connection (get_thread_db) and never throws.
    """
    if not task_id:
        return
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[EventLog] thread db unavailable: %s', e)
        return

    etype = (event or {}).get('type', '')
    now = time.time()

    # Delta coalescing path
    if etype == 'delta':
        kind = 'content' if 'content' in event else ('thinking' if 'thinking' in event else None)
        if kind is None:
            return
        chunk = event.get(kind) or ''
        if not chunk:
            return
        with _pending_lock:
            p = _pending_deltas.get(task_id)
            if p and p['kind'] == kind and (now - p['first_ts']) * 1000 < _DELTA_FLUSH_MS:
                p['text'] += chunk
                p['last_event_id'] = event_id  # row will be stored at the LAST id
                return
            # Flush whatever was buffered (different kind or stale)
            if p:
                _flush_pending_locked(task_id, db)
            _pending_deltas[task_id] = {
                'kind': kind, 'text': chunk,
                'first_ts': now, 'event_id': event_id,
                'last_event_id': event_id,
            }
        return

    # Non-delta: flush any buffered delta with its own id, then write self
    with _pending_lock:
        if task_id in _pending_deltas:
            _flush_pending_locked(task_id, db)
    try:
        db_execute_with_retry(
            db,
            'INSERT OR IGNORE INTO task_events (task_id, event_id, ts_ms, type, payload) VALUES (?,?,?,?,?)',
            (task_id, event_id, int(now * 1000), etype or 'unknown',
             _row_payload_to_json(event))
        )
    except Exception as e:
        logger.debug('[EventLog] persist event failed for task=%s type=%s: %s',
                     task_id[:8], etype, e)

    if random.random() < _PRUNE_PROBABILITY:
        try:
            _opportunistic_prune(db)
        except Exception as e:
            logger.debug('[EventLog] prune failed (non-fatal): %s', e)


def flush_pending(task_id):
    """Force flush of any buffered delta for a task. Called on terminal events."""
    if not task_id:
        return
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[EventLog] flush thread db unavailable: %s', e)
        return
    with _pending_lock:
        if task_id in _pending_deltas:
            _flush_pending_locked(task_id, db)


def read_events(task_id, since_event_id=None, limit=10000):
    """Read persisted events for a task, ordered by event_id.

    Args:
        task_id: task identifier.
        since_event_id: if set, returns only events with event_id > N.
        limit: maximum rows to return (defensive cap).

    Returns:
        list of dicts: [{'event_id': N, 'type': ..., 'payload': {...}}, ...]
    """
    if not task_id:
        return []
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[EventLog] read thread db unavailable: %s', e)
        return []
    try:
        if since_event_id is not None:
            rows = db.execute(
                'SELECT event_id, type, payload FROM task_events '
                'WHERE task_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?',
                (task_id, int(since_event_id), int(limit))
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT event_id, type, payload FROM task_events '
                'WHERE task_id=? ORDER BY event_id ASC LIMIT ?',
                (task_id, int(limit))
            ).fetchall()
    except Exception as e:
        logger.warning('[EventLog] read failed for task=%s: %s', task_id[:8], e)
        return []
    out = []
    for r in rows:
        try:
            payload_raw = r['payload'] if 'payload' in r.keys() else r[2]
        except Exception:
            payload_raw = r[2]
        if isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            try:
                payload = json.loads(payload_raw or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {'type': r['type'] if 'type' in r.keys() else r[1]}
        try:
            eid = int(r['event_id'] if 'event_id' in r.keys() else r[0])
        except Exception:
            continue
        out.append({'event_id': eid, 'payload': payload})
    return out


def has_terminal_event(task_id):
    """Return True if a 'done' event has been persisted for this task."""
    if not task_id:
        return False
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND type='done' LIMIT 1",
            (task_id,)
        ).fetchone()
        return bool(row)
    except Exception as e:
        logger.debug('[EventLog] has_terminal_event failed for task=%s: %s', task_id[:8], e)
        return False


def _opportunistic_prune(db):
    """Delete events for tasks that have been terminal for > EVENT_TTL_MS.

    Uses task_results.completed_at as the terminal timestamp.
    """
    cutoff = int((time.time() * 1000) - EVENT_TTL_MS)
    try:
        cur = db.execute(
            "DELETE FROM task_events WHERE task_id IN ("
            "  SELECT te.task_id FROM task_events te "
            "  JOIN task_results tr ON tr.task_id = te.task_id "
            "  WHERE tr.status IN ('done','error','aborted','interrupted') "
            "    AND tr.completed_at IS NOT NULL "
            "    AND tr.completed_at < ? "
            "  GROUP BY te.task_id"
            ")",
            (cutoff,)
        )
        db.commit()
        rc = getattr(cur, 'rowcount', 0) or 0
        if rc > 0:
            logger.info('[EventLog] Pruned %d stale event row(s) (cutoff=%d)', rc, cutoff)
    except Exception as e:
        logger.debug('[EventLog] prune query failed: %s', e)
        try:
            db.rollback()
        except Exception:
            pass
