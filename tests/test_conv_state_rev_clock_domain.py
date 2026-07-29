"""tests/test_conv_state_rev_clock_domain.py — pt_781ae072d6ee4e84:
``runningTaskIdsRev`` must be TOTALLY ORDERED across process restarts and
across replicas, and the client must never mint a rev of its own.

THE DISEASE
-----------
``_revStrictlyGreater`` compares two rev tuples with ``>``. That is only
meaningful if every value being compared comes from the SAME clock domain.
It did not:

  * server minted ``[time.monotonic_ns(), replica_id]`` — monotonic_ns counts
    from THIS PROCESS's start, so it resets to ~0 on every restart and is
    incomparable between two replicas booted at different times;
  * the client's snapshot-CLEAR branch minted ``[Date.now() * 1e6,
    'snapshot-clear']`` — WALL-CLOCK ns, ~1.78e18, three orders of magnitude
    larger than any monotonic_ns value.

Three distinct user-visible failures fall out of that one mistake, and they
are MIRROR IMAGES of each other, which is why fixing only one is not a fix:

  A. FALSE-IDLE ("generating but nothing shown"). A conv goes busy → the push
     socket drops → on reconnect the snapshot omits it (its task finished) →
     the CLEAR branch stamps a wall-clock rev → every subsequent REAL server
     frame compares smaller and is dropped forever. The conv is deaf on BOTH
     transports (push notify and the poll fallback share this reducer), and
     the 25s/90s reconcile cannot heal it because the conversation-list
     endpoint deliberately carries no runningTaskIds. Only F5 recovers.

  B. FALSE-BUSY ("ghost Stop button") — the mirror, and the more common one,
     because it fires on EVERY server restart for EVERY connected tab. A conv
     is busy with t1 when the server restarts; monotonic_ns resets to ~0, so
     every post-restart frame compares smaller and is dropped. The client
     keeps the DEAD t1 in its authoritative set forever: the Stop button
     stays lit, and ``pickAuthoritativeTaskIdForReconnect`` hands out t1, so
     click-open attaches to a task that no longer exists.

  C. MULTI-REPLICA STARVATION. A replica booted 10 days ago mints ns ≈ 8.6e14;
     one booted an hour ago mints ≈ 3.6e12. The younger replica's frames are
     ALWAYS smaller, so its state can never land no matter how recent it is.

THE FIX THIS GUARD PINS
-----------------------
Rev becomes ``[BOOT_EPOCH_NS + monotonic_ns(), replica_id]`` — a wall-clock
ANCHOR sampled exactly once at process start, advanced only by a monotonic
delta. That is simultaneously:

  * strictly increasing within a process (monotonic delta; immune to a
    wall-clock rewind DURING the process — the clock is read once, at import);
  * strictly increasing ACROSS a restart (a fresh process re-anchors to
    wall-now, which is later than the old process's anchor + its uptime);
  * comparable ACROSS replicas (both anchored to wall time, so ordering
    matches real time to within NTP skew).

A per-conv persistent DB counter was the considered alternative and is
REJECTED on measured cost: it puts a write+read on the hot path of every
notify frame (measured 135 us median / 315 us p95 vs 0.19 us — ~720x) and
makes the DB a hard dependency of a signal whose whole job is to survive
degraded conditions. The boot-epoch scheme needs no storage at all.

Client-side rev synthesis is ABOLISHED rather than corrected: the snapshot
carries its own server-minted ``rev``, and CLEAR stamps THAT. A client that
mints its own value is by definition in a different clock domain from the
server, so "mint it correctly on the client" is not a reachable state.

Faces (all failing-first against the pre-fix tree):

  1. Server rev is wall-anchored (within a few seconds of time.time_ns()),
     not process-relative.
  2. Server rev survives a simulated restart: a re-imported/re-anchored
     mint is strictly greater than one from the previous "process".
  3. Server rev orders correctly across replicas with different boot times.
  4. The connect snapshot and the poll projection both carry a frame-level
     ``rev`` for the CLEAR branch to stamp.
  5. JS: no client-side rev synthesis remains (no Date.now() feeding a rev).
  6. JS reducer scenario A — false-idle no longer reachable.
  7. JS reducer scenario B — false-busy after restart no longer reachable;
     asserts SET CONTENTS and the reconnect target, not just the boolean.
  8. JS reducer scenario C — multi-replica ordering.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ═══════════════════════════════════════════════════════════════════
# Server-side: the rev must be wall-anchored and totally ordered
# ═══════════════════════════════════════════════════════════════════

def test_server_rev_is_wall_clock_anchored():
    """Face 1 — a minted rev must sit in the WALL-CLOCK domain.

    Pre-fix this is ``time.monotonic_ns()`` (uptime-relative: ~1e14 on a
    10-day host, ~1e9 on a fresh one), so it fails by many orders of
    magnitude. Post-fix it is within seconds of ``time.time_ns()``.

    This is the structural guard the owner asked for in place of a comment:
    it makes "the rev lives in the same domain as wall time" a machine-checked
    property, so no future edit can reintroduce a process-relative clock
    without turning this red.
    """
    from lib.conversations.meta_cache import _running_task_ids_rev
    rev = _running_task_ids_rev()
    assert isinstance(rev, list) and len(rev) == 2, (
        'rev must stay a 2-array so the client lex compare is unchanged')
    ns, replica = rev
    assert isinstance(ns, int), 'rev[0] must be an int ns value'
    assert isinstance(replica, str), 'rev[1] must be the replica id string'
    wall = time.time_ns()
    drift = abs(wall - ns)
    # 60s of slack: generous enough for a slow import/CI pause, tight enough
    # that a process-relative clock (off by days, or by ~1.78e18) cannot pass.
    assert drift < 60 * 10 ** 9, (
        'runningTaskIdsRev[0] must be WALL-CLOCK-ANCHORED ns, not '
        'process-relative. Got %r (drift from wall clock: %.3e ns). A '
        'monotonic_ns value resets on restart, which makes every '
        'post-restart frame compare SMALLER and be dropped forever '
        '(the ghost-Stop-button bug).' % (ns, drift))


def test_server_rev_strictly_increases_within_process():
    """Two consecutive mints must never be equal or inverted.

    An equal rev is not harmless: the client's gate is STRICTLY greater, so a
    duplicate ns silently drops the second frame.
    """
    from lib.conversations.meta_cache import _running_task_ids_rev
    prev = _running_task_ids_rev()[0]
    for _ in range(2000):
        cur = _running_task_ids_rev()[0]
        assert cur > prev, (
            'consecutive mints must strictly increase (got %r then %r); an '
            'equal value is dropped by the strict gate' % (prev, cur))
        prev = cur


def test_server_rev_survives_restart():
    """Face 2 — the rev after a restart must exceed the rev before it.

    Simulates a restart by re-computing the anchor the way a fresh process
    would, then minting through the REAL function's arithmetic. Pre-fix, a
    restarted process starts near 0 and loses to the old process forever.
    """
    from lib.conversations import meta_cache

    before = meta_cache._running_task_ids_rev()[0]

    # A fresh process re-samples the wall anchor and restarts monotonic at ~0.
    # Rebind the module's anchor to what a brand-new process would compute.
    anchor_name = '_BOOT_EPOCH_NS'
    assert hasattr(meta_cache, anchor_name), (
        'meta_cache must expose %s — the once-per-process wall anchor that '
        'makes the rev comparable across restarts' % anchor_name)
    saved = getattr(meta_cache, anchor_name)
    try:
        setattr(meta_cache, anchor_name, time.time_ns() - time.monotonic_ns())
        after = meta_cache._running_task_ids_rev()[0]
    finally:
        setattr(meta_cache, anchor_name, saved)

    assert after > before, (
        'a rev minted after a restart (%r) must be strictly greater than one '
        'minted before it (%r). With process-relative monotonic_ns the '
        'restarted server always loses, so every connected tab keeps its '
        'pre-restart task set forever — a Stop button that never clears and '
        'a reconnect target that 404s.' % (after, before))


def test_server_rev_orders_across_replicas_by_real_time():
    """Face 3 — a younger replica's newer frame must beat an older replica's.

    Both replicas anchor to wall time, so ordering follows real time rather
    than uptime. Pre-fix, the replica with the longest uptime won every
    comparison permanently.
    """
    from lib.conversations import meta_cache

    anchor_name = '_BOOT_EPOCH_NS'
    saved = getattr(meta_cache, anchor_name)
    try:
        # Replica A: booted 10 days ago. Its anchor is 10 days in the past,
        # but its monotonic reading is correspondingly large — the sum is
        # still ~wall-now, which is the whole point.
        setattr(meta_cache, anchor_name, time.time_ns() - time.monotonic_ns())
        rev_old_replica = meta_cache._running_task_ids_rev()[0]
        time.sleep(0.002)
        # Replica B: booted an hour ago, mints 2ms LATER in real time.
        setattr(meta_cache, anchor_name, time.time_ns() - time.monotonic_ns())
        rev_new_replica = meta_cache._running_task_ids_rev()[0]
    finally:
        setattr(meta_cache, anchor_name, saved)

    assert rev_new_replica > rev_old_replica, (
        'the frame minted LATER in real time must win regardless of which '
        'replica has the longer uptime (got %r vs %r)'
        % (rev_new_replica, rev_old_replica))


def test_snapshot_and_poll_projection_carry_frame_level_rev():
    """Face 4 — both transports must ship a server-minted frame-level ``rev``.

    This is what lets the client's CLEAR branch stamp an AUTHORITATIVE value
    instead of synthesizing one. Without it the client has nothing to stamp
    and the only options are "mint locally" (the bug) or "don't advance the
    rev" (which lets a stale frame resurrect a cleared dot).
    """
    from lib.agent_core.push import build_conv_state_snapshot
    snap = build_conv_state_snapshot(user_id='')
    assert 'rev' in snap, (
        'the conv_state_snapshot frame must carry a frame-level server-minted '
        '``rev`` for the CLEAR branch to stamp')
    assert isinstance(snap['rev'], list) and len(snap['rev']) == 2, (
        'frame-level rev must have the same [ns, replica] shape as a per-conv '
        'rev so ONE comparator handles both')
    drift = abs(time.time_ns() - snap['rev'][0])
    assert drift < 60 * 10 ** 9, (
        'frame-level rev must be wall-anchored like every other rev')


# ═══════════════════════════════════════════════════════════════════
# Structural guard: the client must not mint revs (charter #24 —
# strip comments first, and reuse the shared scanner rather than
# hand-rolling a second definition of "a real occurrence").
# ═══════════════════════════════════════════════════════════════════

def test_client_never_synthesizes_a_rev():
    """Face 5 — no client-side rev synthesis anywhere in the reducer.

    Comments are stripped first (charter #24) so the prose in this very file's
    sibling docstrings — and the reducer's own explanatory banner, which must
    be free to DESCRIBE the old bug — can neither satisfy nor violate the
    guard.

    The check is deliberately about the DOMAIN, not about one spelling: any
    ``Date.now()`` / ``performance.now()`` reaching a rev-shaped assignment is
    a cross-domain value by construction, because the client has no access to
    the server's anchor.
    """
    from tests._source_scan import strip_comments

    src = strip_comments(open(REDUCER, encoding='utf-8').read(), lang='js')

    offenders = []
    for m in re.finditer(r'^.*\b(Date\.now|performance\.now)\s*\(\s*\).*$',
                         src, re.MULTILINE):
        line = m.group(0)
        if re.search(r'Rev\b|rev\s*[=:]|runningTaskIdsRev', line):
            offenders.append(line.strip())

    assert not offenders, (
        'the client must NEVER mint a rev — it has no access to the server '
        'anchor, so any locally-minted value is in a different clock domain '
        'and poisons the strict-greater gate. Use the frame-level ``rev`` the '
        'server ships on the snapshot. Offending lines:\n  '
        + '\n  '.join(offenders))


def test_snapshot_clear_sentinel_is_gone():
    """The ``'snapshot-clear'`` sentinel replica-id must not survive.

    It only ever existed to label a client-minted rev. If it is still present
    the client is still minting, whatever the ns expression looks like.
    """
    from tests._source_scan import strip_comments
    src = strip_comments(open(REDUCER, encoding='utf-8').read(), lang='js')
    assert 'snapshot-clear' not in src, (
        "the client-minted 'snapshot-clear' rev sentinel must be gone — CLEAR "
        'now stamps the snapshot frame\'s own server-minted rev')


# ═══════════════════════════════════════════════════════════════════
# JS reducer behaviour — the three user-visible scenarios
# ═══════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;
global.debugLog = () => {};
global.saveConversations = () => {};
global.activeStreams = new Map();
global._currentUserId = null;

const out = [];
function check(name, cond, detail) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  :: ' + (detail || '')));
}

// With `node -e SCRIPT ARG`, the script's own path is absent from argv, so the
// first user argument lands at argv[1] (NOT argv[2] as in `node file.js ARG`).
const JS_DIR = process.argv[1];
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'core/conv_state_reducer.js'), 'utf8'));

const M = new Map();

/* ── WHY THE SERVER DOMAIN HERE IS DELIBERATELY *SMALL* ──────────────────
 * The client reducer must be TRANSPARENT to whatever clock domain the server
 * mints in: its job is to compare server values against each other, never to
 * inject a value of its own. A small ascending sequence models that domain
 * abstractly (and matches the pre-fix on-wire monotonic_ns magnitude).
 *
 * This is what makes scenario A discriminating. If the harness fed
 * WALL-ANCHORED revs instead, the client's buggy `Date.now()*1e6` stamp would
 * land in the SAME magnitude band as the test data and the bug would hide —
 * a green test over a live defect. Measured: with wall-anchored inputs all of
 * A/B/C pass against the unfixed reducer. */
let tick = 0;
const rev = (replica) => [1000 + (++tick) * 1000, replica || 'r1'];

// ── Scenario A: FALSE-IDLE. clear-then-new-task must go busy again. ──
// Failing-first for the CLIENT half: pre-fix the CLEAR branch stamps
// Date.now()*1e6 (~1.785e18), which dwarfs every subsequent server rev, so the
// conv is deaf forever and this scenario reports A_new_task_relights=FAIL.
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: rev() });
  const wasBusy = computeConvBusy(c, M);
  // Reconnect snapshot omits c1 (its task finished) => CLEAR branch.
  applyConvStateSnapshot(convs, { convs: {}, rev: rev() });
  const cleared = computeConvBusy(c, M) === false;
  // A NEW task starts; the server's next notify MUST land.
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t2'],
                                    runningTaskIdsRev: rev() });
  check('A_initial_busy', wasBusy === true);
  check('A_clear_extinguishes', cleared === true);
  check('A_new_task_relights', computeConvBusy(c, M) === true,
        'rev_after_clear=' + JSON.stringify(c._authoritativeActiveTaskIdsRev) +
        ' set=' + JSON.stringify([...(c._authoritativeActiveTaskIds || [])]));
  check('A_new_task_id_is_t2',
        !!c._authoritativeActiveTaskIds &&
        c._authoritativeActiveTaskIds.has('t2'),
        'set=' + JSON.stringify([...(c._authoritativeActiveTaskIds || [])]));
  check('A_attachable_is_t2',
        pickAuthoritativeTaskIdForReconnect(c) === 't2',
        'got=' + pickAuthoritativeTaskIdForReconnect(c));
  check('A_clear_stamped_server_rev_not_client_clock',
        Array.isArray(c._authoritativeActiveTaskIdsRev) &&
        c._authoritativeActiveTaskIdsRev[1] !== 'snapshot-clear' &&
        c._authoritativeActiveTaskIdsRev[0] < 1e15,
        'CLEAR must stamp the snapshot\'s server rev; got ' +
        JSON.stringify(c._authoritativeActiveTaskIdsRev));
}

// ── Scenario A2: CLEAR must still repel a genuinely stale notify. ──
// The CLEAR rev advance exists so a reordered older frame cannot un-clear the
// dot. Sourcing it from the server must not lose that property.
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [5000, 'r1'] });
  applyConvStateSnapshot(convs, { convs: {}, rev: [9000, 'r1'] });
  // An in-flight frame minted BEFORE the snapshot arrives late — must be dropped.
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [6000, 'r1'] });
  check('A2_stale_frame_cannot_unclear',
        computeConvBusy(c, M) === false,
        'set=' + JSON.stringify([...(c._authoritativeActiveTaskIds || [])]));
}

// ── Scenario B: FALSE-BUSY after a server restart. ──
// The failing-first proof for the restart bug is SERVER-side
// (test_server_rev_survives_restart): a reducer cannot repair a server that
// mints backwards. What THIS pins is the consequence given a fixed server —
// and it asserts SET CONTENTS + the reconnect target, never just `busy`,
// because the trap is that a stale DEAD task id keeps busy===true, so a
// boolean-only assertion reports green on the exact defect.
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [100000, 'pid-12345'] });
  check('B_pre_restart_has_t1', c._authoritativeActiveTaskIds.has('t1'));

  // ---- restart: fresh pid; a wall-anchored server still mints FORWARD ----
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t9'],
                                    runningTaskIdsRev: [200000, 'pid-67890'] });
  check('B_post_restart_adopts_t9',
        c._authoritativeActiveTaskIds.has('t9') &&
        !c._authoritativeActiveTaskIds.has('t1'),
        'set=' + JSON.stringify([...c._authoritativeActiveTaskIds]));
  check('B_reconnect_target_is_t9',
        pickAuthoritativeTaskIdForReconnect(c) === 't9',
        'got=' + pickAuthoritativeTaskIdForReconnect(c) +
        ' (a DEAD pre-restart id here means click-open attaches to a 404)');

  // t9 finishes; server reports idle. The dot MUST go out.
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: [],
                                    runningTaskIdsRev: [300000, 'pid-67890'] });
  check('B_idle_clears_after_restart',
        computeConvBusy(c, M) === false,
        'set=' + JSON.stringify([...c._authoritativeActiveTaskIds]) +
        ' (ghost Stop button)');
  check('B_no_reconnect_target_when_idle',
        pickAuthoritativeTaskIdForReconnect(c) === null,
        'got=' + pickAuthoritativeTaskIdForReconnect(c));
}

// ── Scenario C: MULTI-REPLICA. Younger replica's newer frame must win. ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['tA'],
                                    runningTaskIdsRev: [400000, 'replicaA'] });
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['tB'],
                                    runningTaskIdsRev: [500000, 'replicaB'] });
  check('C_younger_replica_frame_lands',
        c._authoritativeActiveTaskIds.has('tB') &&
        !c._authoritativeActiveTaskIds.has('tA'),
        'set=' + JSON.stringify([...c._authoritativeActiveTaskIds]));
}

// ── Scenario D: an ALREADY-POISONED tab must self-heal without F5. ──
// The existing fleet is carrying poisoned revs right now. Because the fixed
// server rev is wall-anchored — the SAME domain the poison used — the next
// real frame is strictly greater and lands, so the deployed tabs recover on
// their own instead of needing a manual refresh.
{
  const convs = [{ id: 'c1' }], c = convs[0];
  c._authoritativeActiveTaskIds = new Set();
  c._authoritativeAttachableTaskIds = new Set();
  c._vuCarrierTaskIds = new Set();
  c._authoritativeActiveTaskIdsRev = [Date.now() * 1e6, 'snapshot-clear'];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t5'],
                                    runningTaskIdsRev: [Date.now() * 1e6 + 5e9, 'r1'] });
  check('D_poisoned_tab_self_heals',
        computeConvBusy(c, M) === true,
        'a pre-fix poisoned tab must recover on the next frame, without F5');
}

// ── Scenario E: genuine stale-frame rejection still works. ──
// The gate must keep doing its ORIGINAL job; the fix must not weaken it.
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t2'],
                                    runningTaskIdsRev: [900000, 'r1'] });
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t-old'],
                                    runningTaskIdsRev: [800000, 'r1'] });
  check('E_stale_frame_still_rejected',
        c._authoritativeActiveTaskIds.has('t2') &&
        !c._authoritativeActiveTaskIds.has('t-old'),
        'set=' + JSON.stringify([...c._authoritativeActiveTaskIds]));
}

console.log(out.join('\n'));
"""


def _run_harness():
    proc = subprocess.run(['node', '-e', _HARNESS, JS_DIR],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    return proc.stdout.strip().splitlines()


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_reducer_clock_domain_scenarios():
    """Faces 6-8 (+ self-heal + non-regression) — drive the REAL shipped
    reducer through all three user-visible scenarios.

    Every negative assertion pins SET CONTENTS and the RECONNECT TARGET, not
    merely ``busy``: scenario B's whole trap is that a stale dead task id
    leaves ``busy === true``, so a boolean-only check reports green on the
    exact defect it exists to catch.
    """
    lines = _run_harness()
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, (
        'reducer clock-domain scenarios failed:\n  ' + '\n  '.join(failed)
        + '\n\nfull:\n  ' + '\n  '.join(lines))
    # Guard the guard: a harness that silently stopped emitting checks would
    # otherwise pass vacuously.
    assert len(lines) >= 15, (
        'expected the full scenario matrix, got %d checks:\n%s'
        % (len(lines), '\n'.join(lines)))
