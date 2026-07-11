"""lib.conversations.project_board — the coordination BOARD (Pillar #3).

This is the piece that turns PERCEPTION (the Activity Feed) and shared INTENT
(the Charter) into actual AUTO-COORDINATION: a per-project board of coarse,
human-meaningful epics that conversations POST, CLAIM, and COMPLETE — so two
conversations of the same project stop colliding / duplicating work.

Locked design (owner, 2026-06-30):

  • **Soft, TTL-expiring lease — advisory, never a hard lock.** ``claim_task``
    sets ``owner_conv_id`` + ``lease_expires_at = now + TTL``. The lease is
    NOT enforced by a write-lock; it's a HINT injected into every sibling's
    prompt ("X is being worked by conversation …, avoid duplicating"). A
    crashed/abandoned conversation can NEVER deadlock the board because the
    lease expiry is evaluated AT READ TIME — an expired claim reads as
    ``open`` with no background reaper, no global cleaner thread.
  • **Per ``project_path``, never a process-global.** Every call addresses its
    project explicitly (the read/write-badge thrash guard).
  • **Coarse granularity.** Epics only — fine agent sub-steps belong to the
    Activity Feed, not the board.
  • **Feed-coupled.** post→(no feed; quiet), claim→``claimed``,
    complete→``completed``, block→``blocked`` (the last dead kind finally
    gets a producer here).

``status`` is the STORED column (open/claimed/done); ``effective_status`` is
what a reader sees after the at-read-time lease check (a stored ``claimed``
whose lease has expired is reported ``open``).
"""

from __future__ import annotations

import json
import uuid

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

# Default soft-lease TTL (ms). A claim is advisory for this long; after it the
# epic reads as open again so no abandoned conversation can hold it forever.
DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000  # 30 minutes

# ── Block cooldown (self-expiring escalating backoff) ──
# When an epic hits a genuine external gate (a sibling must commit first; a
# human §10 infra sign-off), block_task stamps blocked_until = now + an
# ESCALATING cooldown so select_dispatchable stops re-dispatching it (which
# burned a billed agent turn every ~30 min to re-discover the same unmet dep).
# The cooldown is exponential in the block count and CAPPED, so a perpetually
# human-gated epic converges to a long sleep after a FEW retries (owner: "few
# retries then long sleep") instead of churning at fixed cadence forever. It is
# NOT the removed park shelf: it self-expires at READ time (no reaper) and needs
# NO human action to release, so it can never deadlock the board.
BLOCK_COOLDOWN_BASE_MS = 60 * 60 * 1000       # 1 h after the first block
BLOCK_COOLDOWN_MAX_MS = 24 * 60 * 60 * 1000   # capped at 1 day
_BLOCK_COOLDOWN_FACTOR = 4                     # x4 per block -> cap by block #4


def _block_cooldown_ms(block_count: int) -> int:
    """Return the cooldown window (ms) for a row that has now been blocked
    ``block_count`` times. 0 blocks -> 0 (never blocked). Otherwise
    ``BASE * FACTOR**(count-1)`` clamped to ``BLOCK_COOLDOWN_MAX_MS`` — so the
    1st block sleeps BASE (1 h), and with FACTOR=4 the cap (1 day) is reached by
    the 4th block: 1 h -> 4 h -> 16 h -> 24 h(cap). That is the owner's "few
    retries then a long sleep" — a perpetually human-gated epic costs ~3 more
    billed turns before settling to one retry/day, instead of ~48/day at the old
    30-min lease cadence. Pure + side-effect-free."""
    n = int(block_count or 0)
    if n <= 0:
        return 0
    # Clamp the exponent so FACTOR**(n-1) can't build a huge int before min()
    # (n is small in practice, but stay safe against a runaway block_count).
    exp = min(n - 1, 20)
    return min(BLOCK_COOLDOWN_MAX_MS, BLOCK_COOLDOWN_BASE_MS * (_BLOCK_COOLDOWN_FACTOR ** exp))


# The block CLASS tag that means "auto-resolves when a sibling commits" — the
# ONLY class that auto-populates a wait-on-path hold (a [human-gated] block
# cannot self-resolve from a lease, so it never derives a path wait).
_SIBLING_TAG = '[sibling]'


def _parse_sibling_wait_paths(reason: str) -> list:
    """Extract the wait-on-path list a ``[sibling]`` block reason declares.

    PARSE CONTRACT (deliberately strict — free-text scraping is FORBIDDEN so a
    worker's prose can neither accidentally populate nor be required to populate
    a path hold):
      • Paths are read ONLY from a STRUCTURED token ``path=<p1>,<p2>,...`` — a
        bare mention of a filename in prose yields NOTHING.
      • The token value is comma-separated; each path is trimmed; the value ends
        at the first whitespace run (so trailing prose after a space is NOT
        consumed into the last path).
      • Paths are returned ONLY when the reason carries the ``[sibling]`` class
        tag. A ``[human-gated]`` or untagged reason yields ``[]`` — a
        human-gated block must never auto-hold on a path.
      • De-duped, order-preserving.

    Pure + side-effect-free. Returns ``[]`` on any non-match.
    """
    import re
    if not reason or _SIBLING_TAG not in reason.lower():
        return []
    m = re.search(r'path=(\S+)', reason)
    if not m:
        return []
    out = []
    for p in m.group(1).split(','):
        s = p.strip()
        if s and s not in out:
            out.append(s)
    return out

_TITLE_MAX_CHARS = 2000  # epics carry multi-sentence design descriptions; a
                         # tight cap silently clipped titles mid-word (both in
                         # the board panel and the injected prompt block)
_MAX_BOARD_TASKS = 200  # coarse epics only — a guard against runaway posting


_now_ms = now_ms


def _prune_expired_leases(db, project_path: str, now_ms: int) -> int:
    """Lazy garbage-collect this project's DEAD path-lease rows (``kind='lease'``
    with a non-zero ``lease_expires_at <= now``).

    This is the WRITE-side analogue of the board's at-read-time lease expiry —
    NOT the background reaper the lease design explicitly rejects. An expired
    lease is already invisible to every READER (``_effective_status`` reports it
    ``open``, so ``render_board_block`` drops it from the Held lane and
    ``select_dispatchable`` never picks it), but the ROW persists forever:
    ``claim_lease``/``release_lease`` only delete on explicit release, and
    ``_effective_status`` merely downgrades at read time. Left unpruned, dead
    leases accumulate and (a) count against ``post_task``'s ``_MAX_BOARD_TASKS``
    cap — a real epic-posting-budget leak, not mere clutter — and (b) bloat the
    ``project_tasks`` scan.

    Deleting a row whose lease has expired is a semantic no-op to every reader,
    so this is safe to piggyback on the two board WRITE seams that already
    mutate + commit (``post_task``, ``claim_lease``) — no new thread, no read
    turned into a writer (``read_board`` stays a pure, never-raises read so its
    contract and the load-bearing NC guards that force-expire a lease then read
    it back are untouched). NEVER prunes an epic (only ``kind='lease'``), never
    a live lease. Best-effort: the caller owns the surrounding transaction, so
    this only issues the DELETE; a failure is logged and swallowed. Returns the
    number of rows deleted (0 on error)."""
    try:
        cur = db.execute(
            "DELETE FROM project_tasks WHERE project_path=? AND kind='lease' "
            'AND lease_expires_at>0 AND lease_expires_at<=?',
            (project_path, now_ms))
        n = cur.rowcount if cur is not None and cur.rowcount is not None else 0
        if n:
            logger.debug('[Board] pruned %d expired lease(s) proj=%.40r', n, project_path)
        return n
    except Exception as e:
        logger.warning('[Board] expired-lease prune failed proj=%.40r: %s',
                       project_path, e)
        return 0


def _effective_status(stored_status: str, lease_expires_at: int,
                      now_ms: int) -> str:
    """The status a READER sees. A stored 'claimed' whose lease has expired is
    reported 'open' — this single function is the anti-deadlock core: it is the
    ONLY place an expired soft-lease is reclaimed (at read time, no reaper)."""
    if stored_status == 'claimed' and lease_expires_at and lease_expires_at <= now_ms:
        return 'open'
    return stored_status


def _row_to_task(r, now_ms: int) -> dict:
    try:
        depends_on = json.loads(r['depends_on']) if r['depends_on'] else []
        if not isinstance(depends_on, list):
            depends_on = []
    except (TypeError, ValueError):
        depends_on = []
    stored = r['status'] or 'open'
    lease = int(r['lease_expires_at'] or 0)
    eff = _effective_status(stored, lease, now_ms)
    try:
        dispatched = bool(r['dispatched'])
    except (KeyError, IndexError, TypeError):
        dispatched = False
    # kind is nullable-safe: a pre-migration row (no column / NULL) reads as
    # 'epic' so it is NEVER silently dropped off the dispatch board.
    try:
        kind = r['kind'] or 'epic'
    except (KeyError, IndexError, TypeError):
        kind = 'epic'
    # Block-cooldown fields are nullable-safe: a pre-migration row (no column)
    # reads as never-blocked (0/'') so it is NEVER wrongly cooldown-suppressed.
    try:
        blocked_until = int(r['blocked_until'] or 0)
    except (KeyError, IndexError, TypeError):
        blocked_until = 0
    try:
        block_count = int(r['block_count'] or 0)
    except (KeyError, IndexError, TypeError):
        block_count = 0
    try:
        block_reason = r['block_reason'] or ''
    except (KeyError, IndexError, TypeError):
        block_reason = ''
    # wait_paths is nullable-safe: a pre-migration row (no column) reads as an
    # empty list -> no wait -> never wrongly held. Malformed JSON also -> [].
    try:
        wait_paths = json.loads(r['wait_paths'] or '[]')
        if not isinstance(wait_paths, list):
            wait_paths = []
    except (KeyError, IndexError, TypeError, ValueError):
        wait_paths = []
    # dispatch_target is nullable-safe: a pre-migration row (no column) reads as
    # '' -> dispatch routes to created_by_conv (unchanged).
    try:
        dispatch_target = r['dispatch_target'] or ''
    except (KeyError, IndexError, TypeError):
        dispatch_target = ''
    return {
        'id': r['id'], 'title': r['title'] or '', 'status': eff,
        'kind': kind,
        'stored_status': stored,
        'owner_conv_id': r['owner_conv_id'] if eff == 'claimed' else '',
        'lease_expires_at': lease if eff == 'claimed' else 0,
        # dispatched badge only meaningful while the (live) claim stands.
        'dispatched': dispatched and eff == 'claimed',
        'created_by_conv': r['created_by_conv'] or '',
        'depends_on': depends_on,
        # Block cooldown: blocked_until is the at-read-time-expiring retry gate;
        # a row is "on cooldown" iff blocked_until > now (evaluated by the
        # reader — select_dispatchable / render). block_count drives escalation.
        'blocked_until': blocked_until,
        'block_count': block_count,
        'block_reason': block_reason,
        # wait-on-path: paths this epic waits on; a reader (select_dispatchable /
        # render) resolves them against live lease rows via _paths_waited_but_held.
        'wait_paths': wait_paths,
        # dispatch_target: mutable routing override (idle-sibling migration).
        # created_by_conv is immutable authorship; this is who runs it NEXT.
        'dispatch_target': dispatch_target,
        'created_at': int(r['created_at'] or 0),
        'updated_at': int(r['updated_at'] or 0),
    }


def claims_by_conv(board_tasks: list) -> dict:
    """Map ``owner_conv_id`` → claimed-epic title, for epics whose EFFECTIVE
    status is ``claimed`` (i.e. a live, unexpired lease).

    This is the SINGLE source of the "which conversation is advancing which
    epic" join. ``read_board`` already reclaimed expired leases to ``open``, so
    every entry here is a live claim — never a deadlocked one. Both
    ``build_brain_summary`` (collab bar) and ``build_peer_status`` (the peer
    introspection tool) consume it so the two views can never drift. Pure +
    side-effect-free; safe on any task list.
    """
    out = {}
    for t in (board_tasks or []):
        if not isinstance(t, dict):
            continue
        if t.get('status') == 'claimed' and t.get('owner_conv_id'):
            out[t['owner_conv_id']] = t.get('title', '')
    return out


def read_board(project_path: str) -> dict:
    """Return the board for ``project_path`` with leases evaluated at read time.

    ``{'tasks': [...], 'open': N, 'claimed': N, 'done': N}`` where each task's
    ``status`` is its EFFECTIVE status (an expired claim → open). Never raises.
    """
    out = {'tasks': [], 'open': 0, 'claimed': 0, 'done': 0}
    if not project_path:
        return out
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT id, title, status, owner_conv_id, lease_expires_at, '
            '       created_by_conv, depends_on, dispatched, kind, '
            '       blocked_until, block_count, block_reason, wait_paths, '
            '       dispatch_target, created_at, updated_at '
            'FROM project_tasks WHERE project_path=? '
            'ORDER BY created_at ASC', (project_path,)).fetchall()
    except Exception as e:
        logger.warning('[Board] read failed proj=%.40r: %s', project_path, e)
        return out
    now = _now_ms()
    for r in rows:
        t = _row_to_task(r, now)
        out['tasks'].append(t)
        out[t['status']] = out.get(t['status'], 0) + 1
    return out


def post_task(project_path: str, conv_id: str, title: str, *,
              depends_on: list | None = None) -> dict:
    """Post a new OPEN epic to the board. Returns ``{'ok', 'id'?, 'error'?}``."""
    title = (title or '').strip()[:_TITLE_MAX_CHARS]
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not title:
        return {'ok': False, 'error': 'empty title'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        # GC dead path-leases FIRST so they never falsely inflate the cap count
        # (an expired lease is invisible to readers but still a row).
        _prune_expired_leases(db, project_path, _now_ms())
        n = db.execute('SELECT COUNT(*) AS c FROM project_tasks WHERE project_path=?',
                       (project_path,)).fetchone()
        if n and int(n['c']) >= _MAX_BOARD_TASKS:
            return {'ok': False, 'error': 'board full (coarse epics only)'}
        task_id = 'pt_' + uuid.uuid4().hex[:16]
        ts = _now_ms()
        deps = json.dumps([str(d) for d in (depends_on or [])], ensure_ascii=False)
        db.execute(
            'INSERT INTO project_tasks '
            '(id, project_path, title, status, owner_conv_id, lease_expires_at, '
            ' created_by_conv, depends_on, created_at, updated_at) '
            "VALUES (?, ?, ?, 'open', '', 0, ?, ?, ?, ?)",
            (task_id, project_path, title, conv_id or '', deps, ts, ts))
        db.commit()
    except Exception as e:
        logger.error('[Board] post failed proj=%.40r: %s', project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('board_post', project_path=project_path, task_id=task_id, conv_id=conv_id)
    return {'ok': True, 'id': task_id}


def claim_task(project_path: str, conv_id: str, task_id: str, *,
               ttl_ms: int = DEFAULT_LEASE_TTL_MS,
               dispatched: bool = False) -> dict:
    """Claim an epic with a SOFT TTL lease (advisory). Succeeds if the epic is
    open OR its existing claim has EXPIRED (at-read-time reclaim) OR it's
    already claimed by THIS conversation (lease refresh). Fails only if a
    DIFFERENT conversation holds an UNEXPIRED lease — and even then it's
    advisory: the caller can still proceed, but the board tells it not to.

    Returns ``{'ok', 'lease_expires_at'?, 'error'?, 'owner'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT status, owner_conv_id, lease_expires_at FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        now = _now_ms()
        eff = _effective_status(row['status'] or 'open',
                                int(row['lease_expires_at'] or 0), now)
        owner = row['owner_conv_id'] or ''
        if eff == 'claimed' and owner and owner != (conv_id or ''):
            # Held by someone else, lease still valid → advisory refusal.
            return {'ok': False, 'error': 'already_claimed', 'owner': owner}
        if (row['status'] or '') == 'done':
            return {'ok': False, 'error': 'already_done'}
        lease = now + max(60_000, int(ttl_ms or DEFAULT_LEASE_TTL_MS))
        db.execute(
            "UPDATE project_tasks SET status='claimed', owner_conv_id=?, "
            'lease_expires_at=?, dispatched=?, updated_at=? '
            'WHERE id=? AND project_path=?',
            (conv_id or '', lease, 1 if dispatched else 0, now,
             task_id, project_path))
        db.commit()
        title = _task_title(db, project_path, task_id)
    except Exception as e:
        logger.error('[Board] claim failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('claimed', project_path, conv_id, f'Claimed: {title}',
          payload={'taskId': task_id})
    audit_log('board_claim', project_path=project_path, task_id=task_id, conv_id=conv_id)
    return {'ok': True, 'lease_expires_at': lease}


def complete_task(project_path: str, conv_id: str, task_id: str) -> dict:
    """Mark an epic done. Returns ``{'ok', 'error'?}``."""
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        title = _task_title(db, project_path, task_id)
        if title is None:
            return {'ok': False, 'error': 'task not found'}
        db.execute(
            "UPDATE project_tasks SET status='done', lease_expires_at=0, "
            "dispatched=0, blocked_until=0, block_count=0, block_reason='', "
            "wait_paths='[]', dispatch_target='', updated_at=? "
            'WHERE id=? AND project_path=?',
            (_now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] complete failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('completed', project_path, conv_id, f'Completed: {title}',
          payload={'taskId': task_id})
    audit_log('board_complete', project_path=project_path, task_id=task_id, conv_id=conv_id)
    # ── Brain-driven dispatch trigger (Pillar #5): completing this epic may
    #    unblock dependents → autonomously kick them off. Best-effort, never
    #    raises into the completion path; no new thread (reuses the queue). ──
    try:
        from lib.conversations.project_dispatch import on_epic_completed
        on_epic_completed(project_path, completed_conv_id=conv_id)
    except Exception as e:
        logger.debug('[Board] post-complete dispatch trigger skipped: %s', e)
    return {'ok': True}


def block_task(project_path: str, conv_id: str, task_id: str, reason: str) -> dict:
    """Report an epic BLOCKED — stamp a SELF-EXPIRING escalating cooldown so it
    stops being re-dispatched while its external gate is unmet, and emit the
    ``blocked`` feed kind.

    This does NOT change the board status (a block is still not a status — the
    row stays ``open``). What it DOES: increment ``block_count`` and set
    ``blocked_until = now + _block_cooldown_ms(block_count)`` + record the
    ``block_reason``. ``select_dispatchable`` skips a row whose ``blocked_until``
    is still in the future, so the ~30-min lease-expiry re-dispatch churn (a
    billed agent turn each cycle to re-discover the same unmet dep) stops. The
    cooldown escalates (exponential, capped) so a perpetually human-gated epic
    converges to a long sleep after a few retries; it expires at READ time (no
    reaper, no human un-block gate) so it can never deadlock and a resolved dep
    IS retried once the window lapses.

    The ``reason`` should record the block CLASS for HUMAN visibility, e.g.
    ``[human-gated] …`` (only a human action can satisfy it — escalate to the
    long interval fast) vs ``[sibling] …`` (will auto-resolve when a sibling
    commits — retry-after-cooldown is right). The escalation itself is
    class-agnostic; the tag is surfaced on the board card, not branched on.
    Returns ``{'ok', 'blocked_until'?, 'block_count'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    reason = (reason or '').strip()[:_TITLE_MAX_CHARS]
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title, block_count FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        title = row['title'] or ''
        new_count = int(row['block_count'] or 0) + 1
        now = _now_ms()
        blocked_until = now + _block_cooldown_ms(new_count)
        # A '[sibling] … path=<p>' reason ALSO auto-populates the wait-on-path
        # hold (the precise mechanism) on the SAME row — the cooldown is the
        # fallback for the interim when no live lease exists yet. One tool for
        # the worker: it reports the sibling blocker once, the brain derives the
        # path hold. Only set when the parse yields paths (never clobber an
        # existing wait with []). See _parse_sibling_wait_paths for the contract.
        wait_paths = _parse_sibling_wait_paths(reason)
        if wait_paths:
            db.execute(
                'UPDATE project_tasks SET blocked_until=?, block_count=?, '
                'block_reason=?, wait_paths=?, updated_at=? '
                'WHERE id=? AND project_path=?',
                (blocked_until, new_count, reason, json.dumps(wait_paths),
                 now, task_id, project_path))
        else:
            db.execute(
                'UPDATE project_tasks SET blocked_until=?, block_count=?, '
                'block_reason=?, updated_at=? WHERE id=? AND project_path=?',
                (blocked_until, new_count, reason, now, task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] block failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    cooldown_min = _block_cooldown_ms(new_count) // 60_000
    _emit('blocked', project_path, conv_id,
          f'Blocked: {title}' + (f' — {reason}' if reason else '')
          + f' (retry in ~{cooldown_min}m, block #{new_count})',
          payload={'taskId': task_id, 'reason': reason,
                   'blockedUntil': blocked_until, 'blockCount': new_count})
    audit_log('board_block', project_path=project_path, task_id=task_id,
              conv_id=conv_id, block_count=new_count)
    return {'ok': True, 'blocked_until': blocked_until, 'block_count': new_count}


def reopen_task(project_path: str, conv_id: str, task_id: str) -> dict:
    """Reopen an epic (done|claimed → open) — a HUMAN override / revive lever.

    Note: there is deliberately NO parked/deferred state to un-park (the
    shelving mechanism was removed — the project pushes every open epic forward
    at full speed rather than holding work pending a human decision).

    A direct status write, NOT a lease mutation: it sets ``status='open'`` and
    CLEARS ``owner_conv_id`` + ``lease_expires_at`` (+ the dispatched flag), so
    the epic becomes claimable again. Permitted from both ``done`` (revive
    finished work) and ``claimed`` (break a wrongly-held or stuck live claim) —
    the ONE human lever to free an epic without a background reaper.

    A ``note`` feed event is emitted so the transition is OBSERVABLE (never a
    silent yank). Note the coordination consequence when reopening a live
    ``claimed`` epic: the previous owner's injected ``[PROJECT BOARD]`` block
    flips that epic from "(you)" to a plain open epic on its NEXT prompt
    assembly (the block is re-read per turn, so the owner is not interrupted
    mid-turn — it simply sees the epic as reclaimable next time, and the feed
    note records who held it). ``{'ok', 'from'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title, status, owner_conv_id, blocked_until FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        prev_status = row['status'] or 'open'
        prev_owner = row['owner_conv_id'] or ''
        title = row['title'] or ''
        # A blocked epic is stored status='open' (block never changes status)
        # but carries a live cooldown. Reopen must still act on it — clearing
        # the cooldown for an IMMEDIATE retry (owner constraint) — so 'open' is
        # only "already open" when it also has NO live block cooldown.
        has_live_block = int(row['blocked_until'] or 0) > _now_ms()
        if prev_status == 'open' and not has_live_block:
            return {'ok': False, 'error': 'already_open'}
        db.execute(
            "UPDATE project_tasks SET status='open', owner_conv_id='', "
            "lease_expires_at=0, dispatched=0, blocked_until=0, block_count=0, "
            "block_reason='', wait_paths='[]', dispatch_target='', updated_at=? "
            'WHERE id=? AND project_path=?',
            (_now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] reopen failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    summary = f'Reopened: {title}'
    if prev_status == 'claimed' and prev_owner:
        summary += f' (was claimed by {prev_owner})'
    _emit('note', project_path, conv_id, summary,
          payload={'taskId': task_id, 'reopened': True, 'from': prev_status,
                   'prevOwner': prev_owner})
    audit_log('board_reopen', project_path=project_path, task_id=task_id,
              conv_id=conv_id, from_status=prev_status, prev_owner=prev_owner)
    return {'ok': True, 'from': prev_status}


def claim_lease(project_path: str, conv_id: str, resource: str, *,
                ttl_ms: int = DEFAULT_LEASE_TTL_MS) -> dict:
    """Claim a durational RESOURCE/PATH lease — "I'm actively editing these
    paths, hold off." Complementary to (not a duplicate of)
    ``lib.presence.conflict.detect_overlaps``: that is a REACTIVE, active-peers-
    only, file-level collision REPORT (two live peers already touching the same
    ``currentFile``); a lease is a PROACTIVE, path-level RESERVATION posted
    BEFORE the edit that reaches EVERY sibling — including an idle one the
    heartbeat wakes later — via the ambient ``[PROJECT BOARD]`` block. The lease
    PREVENTS the collision the overlap detector would otherwise later report.

    A lease is a ``project_tasks`` row with ``kind='lease'``: it reuses the SAME
    soft TTL-lease + at-read-time expiry (``_effective_status``) as an epic
    claim, but is EXCLUDED from ``select_dispatchable`` (never auto-dispatched
    as work) and rendered in its own "Held" section. Re-claiming the SAME
    resource by the SAME conversation REFRESHES the lease (the every-turn board
    re-read keeps a live holder's reservation alive at zero cost; a crash lets
    it expire in one TTL). A DIFFERENT conversation's live lease on the same
    resource is an advisory refusal (like an epic claim).

    Returns ``{'ok', 'id'?, 'lease_expires_at'?, 'error'?, 'owner'?}``.
    """
    resource = (resource or '').strip()[:_TITLE_MAX_CHARS]
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not resource:
        return {'ok': False, 'error': 'empty resource'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        now = _now_ms()
        # Lazy GC: sweep this project's dead leases on any lease write, so
        # orphaned reservations (claimed on a distinct path, never released)
        # don't accumulate forever. If THIS resource's own lease is expired it
        # is pruned here too — the lookup below then misses and we recreate it
        # via the INSERT branch, which is exactly the "expired → reclaimable"
        # semantic (just as a fresh row rather than an in-place UPDATE).
        _prune_expired_leases(db, project_path, now)
        # Find an existing lease for this exact resource (title match on a
        # kind='lease' row). One reservation per resource string.
        row = db.execute(
            "SELECT id, owner_conv_id, lease_expires_at FROM project_tasks "
            "WHERE project_path=? AND kind='lease' AND title=?",
            (project_path, resource)).fetchone()
        lease = now + max(60_000, int(ttl_ms or DEFAULT_LEASE_TTL_MS))
        if row:
            owner = row['owner_conv_id'] or ''
            eff = _effective_status('claimed', int(row['lease_expires_at'] or 0), now)
            if eff == 'claimed' and owner and owner != (conv_id or ''):
                # Held by a different conversation, lease still valid → advisory
                # refusal (the board still tells the caller who holds it).
                return {'ok': False, 'error': 'already_held', 'owner': owner}
            # Ours (refresh) or expired (reclaim) → take/renew it.
            db.execute(
                "UPDATE project_tasks SET status='claimed', owner_conv_id=?, "
                'lease_expires_at=?, updated_at=? WHERE id=? AND project_path=?',
                (conv_id or '', lease, now, row['id'], project_path))
            db.commit()
            task_id = row['id']
        else:
            task_id = 'pt_' + uuid.uuid4().hex[:16]
            db.execute(
                'INSERT INTO project_tasks '
                '(id, project_path, title, status, owner_conv_id, lease_expires_at, '
                " created_by_conv, depends_on, kind, created_at, updated_at) "
                "VALUES (?, ?, ?, 'claimed', ?, ?, ?, '[]', 'lease', ?, ?)",
                (task_id, project_path, resource, conv_id or '', lease,
                 conv_id or '', now, now))
            db.commit()
    except Exception as e:
        logger.error('[Board] claim_lease failed proj=%.40r res=%.60r: %s',
                     project_path, resource, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('note', project_path, conv_id, f'Holding path(s): {resource}',
          payload={'taskId': task_id, 'lease': True, 'resource': resource})
    audit_log('board_claim_lease', project_path=project_path, task_id=task_id,
              conv_id=conv_id, resource=resource[:120])
    return {'ok': True, 'id': task_id, 'lease_expires_at': lease}


def release_lease(project_path: str, conv_id: str, resource: str) -> dict:
    """Release a resource/path lease held by THIS conversation (delete the
    ``kind='lease'`` row). A no-op advisory error if no matching lease exists.
    Only the holder may release (a different conversation's live lease is left
    untouched — releasing it would be a silent yank). Returns ``{'ok','error'?}``.
    """
    resource = (resource or '').strip()[:_TITLE_MAX_CHARS]
    if not project_path or not resource:
        return {'ok': False, 'error': 'missing project/resource'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            "SELECT id, owner_conv_id FROM project_tasks "
            "WHERE project_path=? AND kind='lease' AND title=?",
            (project_path, resource)).fetchone()
        if not row:
            return {'ok': False, 'error': 'no such lease'}
        owner = row['owner_conv_id'] or ''
        if owner and owner != (conv_id or ''):
            return {'ok': False, 'error': 'held_by_other', 'owner': owner}
        db.execute('DELETE FROM project_tasks WHERE id=? AND project_path=?',
                   (row['id'], project_path))
        db.commit()
        task_id = row['id']
    except Exception as e:
        logger.error('[Board] release_lease failed proj=%.40r res=%.60r: %s',
                     project_path, resource, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('note', project_path, conv_id, f'Released path(s): {resource}',
          payload={'taskId': task_id, 'lease': True, 'released': True,
                   'resource': resource})
    audit_log('board_release_lease', project_path=project_path, task_id=task_id,
              conv_id=conv_id, resource=resource[:120])
    return {'ok': True}


def _paths_waited_but_held(epic: dict, board_tasks: list, now_ms: int) -> list:
    """The wait-on-path RESOLVER (pure, side-effect-free).

    Given an epic's ``wait_paths`` and the board's task list, return the subset
    of those paths currently held by a LIVE lease owned by a DIFFERENT
    conversation. An empty result means the epic is NOT waiting (dispatchable
    as far as wait-on-path is concerned).

    This is the INVERSE READ of the path-lease — NOT a new lock namespace. It
    reads the SAME ``kind='lease'`` rows ``claim_lease`` writes, and uses
    ``_effective_status`` so an EXPIRED lease no longer holds the path (the
    at-read-time self-expiry that keeps this out of park-2.0 territory: a
    crashed/abandoned holder releases within one lease TTL, no reaper).

    Fail-open by construction: no wait_paths, a path nobody leases, an expired
    lease, or the epic's OWN lease → that path is NOT held → never strands.
    """
    want = epic.get('wait_paths') or []
    if not want:
        return []
    epic_conv = (epic.get('created_by_conv') or '').strip()
    # Build path -> is-held-by-another map from live lease rows.
    held_by_other = set()
    for t in (board_tasks or []):
        if not isinstance(t, dict) or t.get('kind') != 'lease':
            continue
        eff = _effective_status('claimed', int(t.get('lease_expires_at') or 0), now_ms)
        if eff != 'claimed':
            continue  # expired lease no longer holds anything
        owner = (t.get('owner_conv_id') or '').strip()
        # Only a DIFFERENT conversation's live lease holds this epic. The epic's
        # own lease on a path must never self-deadlock it.
        if owner and owner != epic_conv:
            held_by_other.add(t.get('title') or '')
    return [p for p in want if p in held_by_other]


def set_wait_paths(project_path: str, conv_id: str, task_id: str,
                   paths: list) -> dict:
    """Declare (or clear) the PATHS an epic must wait on — the wait-on-path
    commit-dependency. ``paths`` is a list of path/resource strings matching the
    lease ``title`` a sibling would claim; an EMPTY list clears the wait.

    This does NOT change board status. ``select_dispatchable`` (wired later)
    holds the epic while any listed path is under a live lease held by another
    conversation (resolved by ``_paths_waited_but_held``), and releases when
    that lease expires — so the brain HOLDS precisely while a sibling is
    actively editing the path, instead of the block-then-cooldown dance. Reset
    to ``[]`` on complete / reopen. Returns ``{'ok', 'wait_paths'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    # Normalize: strings only, trimmed, de-duped, bounded.
    clean = []
    for p in (paths or []):
        s = (str(p) or '').strip()[:_TITLE_MAX_CHARS]
        if s and s not in clean:
            clean.append(s)
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        title = _task_title(db, project_path, task_id)
        if title is None:
            return {'ok': False, 'error': 'task not found'}
        db.execute(
            'UPDATE project_tasks SET wait_paths=?, updated_at=? '
            'WHERE id=? AND project_path=?',
            (json.dumps(clean), _now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] set_wait_paths failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    if clean:
        _emit('note', project_path, conv_id,
              f'Waiting on path(s): {", ".join(clean)}',
              payload={'taskId': task_id, 'waitPaths': clean})
    else:
        _emit('note', project_path, conv_id, 'Cleared path wait',
              payload={'taskId': task_id, 'waitPaths': []})
    audit_log('board_wait_on_path', project_path=project_path, task_id=task_id,
              conv_id=conv_id, wait_count=len(clean))
    return {'ok': True, 'wait_paths': clean}


def _task_title(db, project_path: str, task_id: str):
    row = db.execute('SELECT title FROM project_tasks WHERE id=? AND project_path=?',
                     (task_id, project_path)).fetchone()
    return None if not row else (row['title'] or '')


def _emit(kind: str, project_path: str, conv_id: str, summary: str,
          *, payload: dict | None = None) -> None:
    """Best-effort feed emission — never raises into the board caller."""
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(project_path, conv_id or '', kind, summary, payload=payload)
    except Exception as e:
        logger.debug('[Board] feed emit (%s) skipped: %s', kind, e)


def render_board_block(project_path: str, current_conv_id: str = '') -> str:
    """Render the board for system-context injection — the AUTO-COORDINATION
    surface. Lists open epics + a per-claimed-epic explicit "avoid duplication"
    hint when ANOTHER conversation holds an UNEXPIRED lease (this is what makes
    a reading conversation step aside instead of redoing the work). Returns ''
    when the board is empty (no prompt weight for an unused board).
    """
    board = read_board(project_path)
    tasks = board['tasks']
    if not tasks:
        return ''
    # Leases (kind='lease') are path RESERVATIONS, not epics — partition them
    # out of every epic section and render them in their own "Held" block. Only
    # a LIVE lease (effective status still 'claimed') is a held reservation; an
    # expired one reads 'open' and is simply dropped (it holds nothing).
    epics = [t for t in tasks if t.get('kind') not in ('lease', 'ready')]
    held_t = [t for t in tasks if t.get('kind') == 'lease' and t['status'] == 'claimed']
    now = _now_ms()
    # An epic whose block cooldown is still LIVE (blocked_until > now) is
    # partitioned into its own "Blocked" lane — NOT the Open lane (where it
    # would read as "claim me" and get re-dispatched). Once the cooldown lapses
    # it falls back to Open automatically (at-read-time, no reaper).
    blocked_t = [t for t in epics
                 if t['status'] == 'open' and int(t.get('blocked_until') or 0) > now]
    blocked_ids = {t['id'] for t in blocked_t}
    open_t = [t for t in epics if t['status'] == 'open' and t['id'] not in blocked_ids]
    claimed_t = [t for t in epics if t['status'] == 'claimed']
    done_t = [t for t in epics if t['status'] == 'done']
    # Ready-to-land MARKERS (kind='ready') — the continuous-atomic-slice-landing
    # queue. Partitioned into their own "Landing" section (never an epic lane):
    # the human perceives which slices are pending, which land cleanly (file-set
    # disjoint from every other pending marker → the heartbeat lands them), and
    # which OVERLAP a sibling slice and so are HELD for the human to sequence.
    try:
        from lib.conversations.project_ready import (
            held_markers, landable_markers)
        _land_markers = landable_markers(project_path)
        _held_markers = held_markers(project_path)
    except Exception as e:
        logger.debug('[Board] ready-marker read failed proj=%.40r: %s',
                     project_path, e)
        _land_markers, _held_markers = [], []
    if not (open_t or claimed_t or done_t or held_t or blocked_t
            or _land_markers or _held_markers):
        return ''
    lines = ['[PROJECT BOARD] — shared coordination board for this project. '
             'Before starting work, CHECK it: claim an open epic so siblings '
             'know you own it, and do NOT duplicate an epic another '
             'conversation is already advancing.']
    if held_t:
        lines.append('')
        lines.append('Held (do NOT edit — a sibling is actively changing these '
                     'paths; coordinate or wait for the hold to lift):')
        for t in held_t:
            owner = t['owner_conv_id'] or 'another conversation'
            mine = ' (you)' if current_conv_id and owner == current_conv_id else ''
            lines.append(f'  • {t["title"]} — held by {owner}{mine}')
    if claimed_t:
        lines.append('')
        lines.append('In progress (claimed by a conversation — AVOID DUPLICATING):')
        for t in claimed_t:
            owner = t['owner_conv_id'] or 'another conversation'
            mine = ' (you)' if current_conv_id and owner == current_conv_id else ''
            hint = '' if mine else ' — another conversation is advancing this; ' \
                   'pick a different epic or coordinate, do not redo it'
            lines.append(f'  • [{t["id"]}] {t["title"]} — claimed by {owner}{mine}{hint}')
    # Precompute, for each epic, which of its wait_paths are held live by
    # ANOTHER conversation (who holds each) — the wait-on-path annotation. An
    # epic with a non-empty result is held on those paths until the sibling's
    # lease clears (surfaced so the human sees the PRECISE hold, not just a
    # cooldown timer).
    def _wait_annotation(t):
        held = _paths_waited_but_held(t, tasks, now)
        if not held:
            return ''
        holders = {}
        for lt in held_t:
            title = lt.get('title') or ''
            if title in held:
                holders[title] = lt.get('owner_conv_id') or 'another conversation'
        parts = [f'{p} (held by {holders.get(p, "?")})' for p in held]
        return ' — waiting on ' + ', '.join(parts)

    if open_t:
        lines.append('')
        lines.append('Open (unclaimed — claim one with project_board_claim before working it):')
        for t in open_t:
            dep = f' (depends on {", ".join(t["depends_on"])})' if t['depends_on'] else ''
            lines.append(f'  • [{t["id"]}] {t["title"]}{dep}{_wait_annotation(t)}')
    if blocked_t:
        lines.append('')
        lines.append('Blocked (waiting on an external gate — auto-retries after a '
                     'cooldown, do NOT re-dispatch until then):')
        for t in blocked_t:
            mins = max(0, (int(t.get('blocked_until') or 0) - now) // 60_000)
            reason = (t.get('block_reason') or '').strip()
            why = f' — {reason}' if reason else ''
            cnt = int(t.get('block_count') or 0)
            lines.append(f'  • [{t["id"]}] {t["title"]}{why} '
                         f'(retry in ~{mins}m, blocked {cnt}×){_wait_annotation(t)}')
    if _land_markers or _held_markers:
        lines.append('')
        lines.append('Landing (green slices awaiting autonomous commit — the '
                     'continuous-atomic-slice-landing queue):')
        for m in _land_markers:
            owner = m.get('conv') or 'a conversation'
            mine = ' (you)' if current_conv_id and owner == current_conv_id else ''
            files = ', '.join(m.get('files') or []) or '(no files)'
            lines.append(f'  • [{m["id"]}] {files} — by {owner}{mine} — READY, '
                         f'file-set disjoint; the heartbeat will land it '
                         f'automatically')
        if _held_markers:
            # Group held markers by their overlapping file so the human sees the
            # exact collision they must SEQUENCE (two green slices touching the
            # same file are each self-consistent but conflict if landed together).
            lines.append('  Held for human sequencing (these green slices '
                         'OVERLAP on shared files — authorize a landing order; '
                         'the heartbeat will NOT auto-land an overlapping pair):')
            for m in _held_markers:
                owner = m.get('conv') or 'a conversation'
                mine = ' (you)' if current_conv_id and owner == current_conv_id else ''
                files = ', '.join(m.get('files') or []) or '(no files)'
                lines.append(f'    • [{m["id"]}] {files} — by {owner}{mine}')
    if done_t:
        lines.append('')
        lines.append('Recently done:')
        for t in done_t[-8:]:
            lines.append(f'  • {t["title"]}')
    return '\n'.join(lines)


def execute_board_tool(fn_name: str, fn_args: dict, *,
                       current_conv_id: str = '', project_path: str = '') -> str:
    """Execute a board agent tool → human-readable string."""
    try:
        if not project_path:
            return ('Error: the project board is only available in project mode '
                    '(open a project first).')
        if fn_name == 'project_board_read':
            block = render_board_block(project_path, current_conv_id)
            return block or ('The project board is empty. If you discover a '
                             'project-level epic, post it with project_board_post '
                             'so sibling conversations can coordinate.')
        if fn_name == 'project_board_post':
            res = post_task(project_path, current_conv_id,
                            fn_args.get('title') or '',
                            depends_on=fn_args.get('depends_on'))
            return (f'Posted epic {res["id"]} to the board.' if res.get('ok')
                    else f'Error posting epic: {res.get("error", "unknown")}.')
        if fn_name == 'project_board_claim':
            res = claim_task(project_path, current_conv_id,
                             fn_args.get('task_id') or '')
            if res.get('ok'):
                return ('Claimed. Siblings now see you own this epic; complete it '
                        'with project_board_complete when done.')
            if res.get('error') == 'already_claimed':
                return (f'NOT claimed — epic is already being advanced by '
                        f'conversation {res.get("owner", "?")}. Avoid duplicating '
                        f'it; pick a different open epic or coordinate.')
            return f'Error claiming epic: {res.get("error", "unknown")}.'
        if fn_name == 'project_board_complete':
            res = complete_task(project_path, current_conv_id,
                                fn_args.get('task_id') or '')
            return ('Marked done.' if res.get('ok')
                    else f'Error completing epic: {res.get("error", "unknown")}.')
        if fn_name == 'project_board_block':
            res = block_task(project_path, current_conv_id,
                             fn_args.get('task_id') or '',
                             fn_args.get('reason') or '')
            if res.get('ok'):
                mins = _block_cooldown_ms(res.get('block_count', 1)) // 60_000
                return ('Reported blocked. This epic is now on a self-expiring '
                        f'cooldown (~{mins}m, block #{res.get("block_count", 1)}) '
                        'so the autonomous heartbeat will NOT re-dispatch it '
                        'until the external gate has had time to clear. The '
                        'cooldown escalates on repeated blocks and auto-expires '
                        '(no human un-block needed); a human reopen resets it '
                        'for an immediate retry. Tag the reason with the block '
                        'class ([human-gated] vs [sibling]) so it is visible on '
                        'the board.')
            return f'Error reporting block: {res.get("error", "unknown")}.'
        if fn_name == 'project_claim_path':
            res = claim_lease(project_path, current_conv_id,
                              fn_args.get('resource') or '',
                              ttl_ms=int(fn_args.get('ttl_ms') or DEFAULT_LEASE_TTL_MS))
            if res.get('ok'):
                return ('Path(s) held. Siblings now see a "Held — do NOT edit" '
                        'notice on the board (including a freshly-woken idle '
                        'conversation on its next turn). Re-call to refresh the '
                        'hold; release it with project_release_path when done. '
                        'This is advisory — the lease auto-expires so it can '
                        'never deadlock the project.')
            if res.get('error') == 'already_held':
                return (f'NOT held — path(s) are already held by conversation '
                        f'{res.get("owner", "?")}. Coordinate or wait for the '
                        f'hold to lift; do not edit concurrently.')
            return f'Error holding path(s): {res.get("error", "unknown")}.'
        if fn_name == 'project_release_path':
            res = release_lease(project_path, current_conv_id,
                                fn_args.get('resource') or '')
            if res.get('ok'):
                return 'Released. The "Held" notice is cleared for siblings.'
            if res.get('error') == 'held_by_other':
                return (f'Not released — held by conversation '
                        f'{res.get("owner", "?")}, not you.')
            if res.get('error') == 'no such lease':
                return 'No matching hold to release (already expired or released).'
            return f'Error releasing path(s): {res.get("error", "unknown")}.'
        if fn_name == 'project_commit':
            from lib.conversations.project_commit import execute_commit_tool
            return execute_commit_tool(
                fn_args, current_conv_id=current_conv_id,
                project_path=project_path)
        if fn_name == 'project_ready_land':
            from lib.conversations.project_ready import execute_ready_land_tool
            return execute_ready_land_tool(
                fn_args, current_conv_id=current_conv_id,
                project_path=project_path)
        return f"Error: Unknown board tool '{fn_name}'"
    except Exception as e:
        logger.warning('[Board] tool %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'read_board', 'post_task', 'claim_task', 'complete_task', 'block_task',
    'reopen_task', 'claim_lease', 'release_lease',
    'render_board_block', 'execute_board_tool',
    '_effective_status',
    'claims_by_conv', 'DEFAULT_LEASE_TTL_MS',
    'BLOCK_COOLDOWN_BASE_MS', 'BLOCK_COOLDOWN_MAX_MS', '_block_cooldown_ms',
    'set_wait_paths', '_paths_waited_but_held',
]
