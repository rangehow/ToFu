"""lib.conversations.project_ready — the ready-to-land marker + autonomous
landing loop (step 3 of continuous atomic-slice landing).

The north-star gap this closes
-------------------------------
``project_acceptance.run_acceptance_gate`` is a CALLABLE — a human still has to
invoke it and then run ``project_commit`` by hand. That is the same manual
ritual with a nicer signature; it does not "minimize human involvement". This
module wires green → landing WITHOUT a human operating the crank:

1. ``gate_and_post`` — run the acceptance gate for a candidate slice; iff it is
   ``ok`` (green AND self-consistent), post a board MARKER recording the slice.
2. ``auto_land_ready`` — the brain's landing pass: RE-GATE each pending marker
   (a marker whose HEAD moved is never landed blind), land the maximal set of
   markers whose file-sets are pairwise DISJOINT via ``project_commit`` (agent
   author), and HOLD any file-set-overlapping markers for human authorization
   (two independently-green slices touching the same file still conflict at
   merge). A marker that ``do_commit`` refuses (a sibling raced into its files
   → contaminated) is left in place to re-gate next sweep (self-healing, no
   billed churn — landing is a git commit, not an LLM turn).

Storage — NO schema change (the schema is owned by a sibling right now)
-----------------------------------------------------------------------
A marker is a ``project_tasks`` row with ``kind='ready'`` reusing existing
columns, so it needs no new DDL and is DENYLISTED from ``select_dispatchable``
exactly like ``kind='lease'`` (it is not work):

  • ``title``          → a human-readable "ready: <files>" label
  • ``created_by_conv``→ the posting conversation (whose slice it is)
  • ``dispatch_target``→ the ``at_ref`` the gate ran against
  • ``wait_paths``     → the slice ``files[]`` (a path list; never interpreted
    as a wait because a ready row is never dispatched / wait-resolved)
  • ``block_reason``   → a small JSON descriptor {testPaths, green,
    selfConsistent, gateAt}

The overlap check + landing are pure reads of these rows; the whole surface is
best-effort and never raises into a sweep.
"""
from __future__ import annotations

import json
import uuid

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_READY_KIND = 'ready'


def _now_ms() -> int:
    from lib.timeutil import now_ms
    return now_ms()


def _norm(project_path: str) -> str:
    from lib.conversations.project_feed import normalize_project_path
    return normalize_project_path(project_path)


def post_ready_marker(project_path: str, conv_id: str, *, files: list[str],
                      test_paths: list[str], at_ref: str,
                      gate_result: dict) -> str:
    """Insert a ``kind='ready'`` marker row. Returns the marker id ('' on
    failure). Pure DB write — the gate must already have passed (caller's job).
    """
    files = [str(f) for f in (files or []) if f]
    test_paths = [str(t) for t in (test_paths or []) if t]
    if not project_path or not files:
        return ''
    norm = _norm(project_path)
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        mid = 'pt_' + uuid.uuid4().hex[:16]
        ts = _now_ms()
        title = ('ready: ' + ', '.join(files))[:200]
        descriptor = json.dumps({
            'testPaths': test_paths,
            'green': bool(gate_result.get('green')),
            'selfConsistent': bool(gate_result.get('selfConsistent')),
            'gateAt': ts,
        }, ensure_ascii=False)
        files_json = json.dumps(files, ensure_ascii=False)
        db.execute(
            'INSERT INTO project_tasks '
            '(id, project_path, title, status, owner_conv_id, lease_expires_at, '
            ' created_by_conv, depends_on, kind, wait_paths, dispatch_target, '
            ' block_reason, created_at, updated_at) '
            "VALUES (?, ?, ?, 'open', '', 0, ?, '[]', ?, ?, ?, ?, ?, ?)",
            (mid, norm, title, conv_id or '', _READY_KIND, files_json,
             (at_ref or 'HEAD'), descriptor, ts, ts))
        db.commit()
    except Exception as e:
        logger.error('[Ready] post marker failed proj=%.40r: %s', norm, e,
                     exc_info=True)
        return ''
    audit_log('ready_marker_post', project_path=norm, marker_id=mid,
              conv_id=conv_id, files=len(files))
    return mid


def _row_to_marker(r) -> dict:
    try:
        files = json.loads(r['wait_paths'] or '[]')
        if not isinstance(files, list):
            files = []
    except (TypeError, ValueError, KeyError, IndexError) as e:
        logger.debug('[Ready] marker files parse failed, defaulting: %s', e)
        files = []
    try:
        desc = json.loads(r['block_reason'] or '{}')
        if not isinstance(desc, dict):
            desc = {}
    except (TypeError, ValueError, KeyError, IndexError) as e:
        logger.debug('[Ready] marker desc parse failed, defaulting: %s', e)
        desc = {}
    return {
        'id': r['id'],
        'conv': (r['created_by_conv'] or ''),
        'files': [str(f) for f in files],
        'atRef': (r['dispatch_target'] or 'HEAD'),
        'testPaths': [str(t) for t in (desc.get('testPaths') or [])],
        'green': bool(desc.get('green')),
        'selfConsistent': bool(desc.get('selfConsistent')),
        'gateAt': int(desc.get('gateAt') or 0),
    }


def read_ready_markers(project_path: str) -> list[dict]:
    """Every pending ``kind='ready'`` marker for the project, oldest first."""
    if not project_path:
        return []
    norm = _norm(project_path)
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT id, created_by_conv, wait_paths, dispatch_target, '
            '       block_reason, created_at '
            "FROM project_tasks WHERE project_path=? AND kind=? "
            'ORDER BY created_at ASC', (norm, _READY_KIND)).fetchall()
    except Exception as e:
        logger.warning('[Ready] read markers failed proj=%.40r: %s', norm, e)
        return []
    return [_row_to_marker(r) for r in rows]


def _partition_by_overlap(markers: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split markers into (landable, held). A marker is LANDABLE iff its
    file-set is DISJOINT from EVERY other marker's file-set; any marker sharing
    at least one file with another is HELD (both sides of every overlap).

    Conservative by design: it does NOT try to compute a maximum independent
    set (NP-hard) — it holds the entire overlapping cluster for human
    authorization, which is the safe merge-conflict guard the owner specified.
    """
    landable: list[dict] = []
    held: list[dict] = []
    for i, m in enumerate(markers):
        fs = set(m['files'])
        overlaps = False
        for j, other in enumerate(markers):
            if i == j:
                continue
            if fs & set(other['files']):
                overlaps = True
                break
        (held if overlaps else landable).append(m)
    return landable, held


def landable_markers(project_path: str) -> list[dict]:
    """Pending markers whose file-set is disjoint from every other pending
    marker (eligible for autonomous landing)."""
    land, _ = _partition_by_overlap(read_ready_markers(project_path))
    return land


def held_markers(project_path: str) -> list[dict]:
    """Pending markers held for human authorization (file-set overlaps a
    sibling marker)."""
    _, held = _partition_by_overlap(read_ready_markers(project_path))
    return held


def _delete_marker(norm: str, marker_id: str) -> None:
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks WHERE id=? AND project_path=?',
                   (marker_id, norm))
        db.commit()
    except Exception as e:
        logger.warning('[Ready] delete marker %s failed: %s', marker_id, e)


def gate_and_post(project_path: str, conv_id: str, *, files: list[str],
                  test_paths: list[str], at_ref: str = 'HEAD') -> dict:
    """Run the acceptance gate for a candidate slice; iff ok, post a marker.

    Returns ``{posted, markerId?, gate}`` where ``gate`` is the full gate
    result (so the caller can surface WHY a non-ok slice was not posted).
    """
    from lib.conversations import project_acceptance as pa
    gate = pa.run_acceptance_gate(project_path, files=files,
                                  test_paths=test_paths, at_ref=at_ref)
    if not gate.get('ok'):
        logger.info('[Ready] gate not ok for conv=%s (green=%s consistent=%s) '
                    '— not posting marker', (conv_id or '-')[:8],
                    gate.get('green'), gate.get('selfConsistent'))
        return {'posted': False, 'gate': gate}
    mid = post_ready_marker(project_path, conv_id, files=files,
                            test_paths=test_paths, at_ref=at_ref,
                            gate_result=gate)
    return {'posted': bool(mid), 'markerId': mid, 'gate': gate}


def auto_land_ready(project_path: str) -> dict:
    """The brain's autonomous landing pass over pending ready markers.

    For the maximal DISJOINT set (``landable_markers``): RE-GATE each marker at
    HEAD (never land a stale marker blind), then land it via
    ``project_commit.do_commit`` under the marker's own conversation + agent
    author. A marker whose re-gate is no longer ok is SKIPPED (left for the next
    sweep). A marker ``do_commit`` refuses (contaminated by a sibling race) is
    left in place too — never marked landed, so it self-heals on re-gate.
    Overlapping markers are HELD for human authorization.

    Returns ``{landed[], skipped[], held[], errors[]}`` (lists of convs).
    Best-effort; never raises.
    """
    out: dict = {'landed': [], 'skipped': [], 'held': [], 'errors': []}
    if not project_path:
        return out
    norm = _norm(project_path)
    try:
        markers = read_ready_markers(norm)
        land, held = _partition_by_overlap(markers)
        out['held'] = [m['conv'] for m in held]
        if not land:
            return out
        from lib.conversations import project_acceptance as pa
        from lib.conversations import project_commit as pc
        for m in land:
            conv = m['conv']
            # ── RE-GATE at HEAD: a marker gated at an older ref may now be
            #    stale (HEAD moved, an orphan appeared). Never land blind. ──
            regate = pa.run_acceptance_gate(
                norm, files=m['files'], test_paths=m['testPaths'],
                at_ref='HEAD')
            if not regate.get('ok'):
                out['skipped'].append(conv)
                logger.info('[Ready] re-gate stale for conv=%s marker=%s '
                            '(green=%s consistent=%s) — held for next sweep',
                            (conv or '-')[:8], m['id'], regate.get('green'),
                            regate.get('selfConsistent'))
                continue
            # ── Land via project_commit (agent author; byte-identity gate holds
            #    any sibling hunk that raced into these files). ──
            msg = (f"feat: land ready slice ({', '.join(m['files'])})\n\n"
                   f"Autonomously landed by the project brain after the "
                   f"acceptance gate passed green + self-consistent at HEAD.")
            res = pc.do_commit(norm, conv, msg, files=m['files'], author=None)
            if res.get('ok') and res.get('committed'):
                _delete_marker(norm, m['id'])
                out['landed'].append(conv)
                audit_log('ready_marker_landed', project_path=norm,
                          marker_id=m['id'], conv_id=conv,
                          commit=res.get('commitSha', ''))
                logger.info('[Ready] auto-landed slice conv=%s files=%d sha=%s',
                            (conv or '-')[:8], len(m['files']),
                            res.get('commitSha', ''))
            else:
                # do_commit refused (all contaminated by a sibling race) →
                # leave the marker so it re-gates next sweep. NOT an error loop:
                # this is a git op, not a billed LLM turn.
                out['errors'].append(conv)
                logger.info('[Ready] auto-land held conv=%s marker=%s: %s '
                            '(re-gates next sweep)', (conv or '-')[:8], m['id'],
                            res.get('error', 'no clean files'))
    except Exception as e:
        logger.warning('[Ready] auto_land_ready failed proj=%.40r: %s', norm, e)
    return out


def execute_ready_land_tool(fn_args: dict, *, current_conv_id: str = '',
                            project_path: str = '') -> str:
    """Agent-tool entry point (``project_ready_land``) → human-readable string.

    This is the PRODUCER that feeds the autonomous landing loop: an agent calls
    it when it has finished a slice and declares the slice's ``files`` +
    ``test_paths``. It runs the acceptance gate and, iff green + self-consistent,
    posts a ready-to-land MARKER — so the 30s heartbeat can auto-land the slice
    without a human. Mirrors ``project_commit``'s declare-your-files discipline:
    the agent knows exactly what it wrote and how to test it; there is no
    file/test auto-discovery.
    """
    try:
        if not project_path:
            return ('Error: project_ready_land is only available in project mode '
                    '(open a project first).')
        files = fn_args.get('files') or None
        if files is not None and not isinstance(files, list):
            files = [str(files)]
        test_paths = fn_args.get('test_paths') or None
        if test_paths is not None and not isinstance(test_paths, list):
            test_paths = [str(test_paths)]
        if not files:
            return ('No files declared — project_ready_land requires files=[...] '
                    '(the paths YOUR slice edited). It does not auto-discover '
                    'your work.')
        if not test_paths:
            return ('No test_paths declared — project_ready_land requires '
                    'test_paths=[...] (the tests that must pass at HEAD to prove '
                    'the slice green). The acceptance gate runs exactly these.')
        at_ref = (fn_args.get('at_ref') or 'HEAD').strip() or 'HEAD'
        res = gate_and_post(project_path, current_conv_id, files=files,
                            test_paths=test_paths, at_ref=at_ref)
        gate = res.get('gate') or {}
        if res.get('posted'):
            return ('Slice is ready to land — posted a ready marker '
                    f'({res.get("markerId", "")}). The acceptance gate passed '
                    'green + self-consistent at %s. The autonomous heartbeat '
                    'will land it (if its file-set is disjoint from every other '
                    'pending marker) or hold it for human authorization (if it '
                    'overlaps a sibling slice). No further action needed.'
                    % at_ref)
        why = []
        if not gate.get('green', True):
            why.append('tests did not pass')
        if not gate.get('selfConsistent', True):
            orphans = gate.get('orphans') or []
            syms = ', '.join(o.get('symbol', '?') for o in orphans[:5])
            why.append(f'HEAD would be split-brained (orphaned callers: {syms})')
        if not why:
            why.append(gate.get('error') or 'gate not ok')
        summary = gate.get('testSummary') or ''
        return ('Slice NOT posted — the acceptance gate did not pass: '
                + '; '.join(why)
                + (f'\nTest tail:\n{summary}' if summary else '')
                + '\nFix the failure and re-run project_ready_land.')
    except Exception as e:
        logger.warning('[Ready] execute_ready_land_tool failed: %s', e,
                       exc_info=True)
        return f'Error executing project_ready_land: {e}'


__all__ = ['gate_and_post', 'post_ready_marker', 'read_ready_markers',
           'landable_markers', 'held_markers', 'auto_land_ready',
           'execute_ready_land_tool']
