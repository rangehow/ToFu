"""tests/test_conv_state_staleness_selfheal.py — pt_cadaa70ffa6b468d.

THE SECOND HALF OF THE REPORTED SYMPTOM
---------------------------------------
The owner's complaint had two parts. The first — state converging wrongly — was
a clock-domain defect, fixed separately (charter #27). This is the second part,
which is not a latency problem at all:

  "before the backend state is fully synchronized, the frontend doesn't even
   know its state is lagging, which might lead to displaying incorrect states"

Two independent gaps produced that, and each is a case of a component that
already KNOWS something and does nothing with it:

  A. THE SERVER DETECTED STALLS AND ONLY LOGGED THEM.
     ``drift_tracker.observe_divergence`` already separates a client that is
     merely sampling late (its value is MOVING) from one that is FROZEN while
     the server advances — the "a notify frame was dropped and this tab will
     never converge" case — and marks the latter ``sustained``. But the probe
     endpoint used that verdict only to pick a log level. The server knew
     exactly which socket was stuck and left the user to discover it by
     pressing F5.

  B. THE CLIENT HAD NO CONCEPT OF ITS OWN STALENESS.
     push.js runs an 8s ping watchdog and force-closes a half-open socket, so
     "frames are not arriving" is already computed. Nothing consumed it for
     state rendering: during an outage the sidebar kept drawing a busy/idle
     verdict as settled fact.

WHAT THIS SUITE PINS

  Repair policy (pure, no hub):
    1. a sustained stall warrants a repair;
    2. a non-sustained divergence does NOT (a single missed beat is not worth a
       frame; the next notify almost always closes it);
    3. cooldown suppresses a second repair to the same socket;
    4. an empty socket id can never trigger one — an untargetable repair would
       have to be broadcast, which hits healthy tabs and MASKS the condition;
    5. a failed delivery must NOT start the cooldown, or one unlucky probe
       silences repair for the whole window.

  Transport:
    6. the hub can deliver to ONE socket by req_id, and does not touch others.

  Client confidence:
    7. it is a SEPARATE dimension — ``computeConvBusy`` must be byte-identical
       in behaviour whatever the channel health is. If a dead socket could flip
       busy to false, a running conversation would offer Send and hide Stop:
       a lost abort handle, strictly worse than the staleness being reported.
    8. a live local stream outranks channel health (bytes are arriving on that
       conv, so it cannot be stale).
    9. a timed-out / offline channel degrades an authoritative-only busy verdict
       to 'unconfirmed'.
   10. 'poor' latency stays confirmed — a slow link still delivers, and
       treating slow as stale would light the degraded UI on every mobile
       connection.

  Rendering (the part that makes it visible rather than internal plumbing):
   11. the unconfirmed flag reaches the DOM (dot + tag class);
   12. it is inside the sidebar's status hash, so a health flip actually
       repaints instead of being swallowed by the fast-path early return.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
CONV_LIST = os.path.join(JS_DIR, 'ui', 'conversation_list.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


@pytest.fixture(autouse=True)
def _clean_repair_state():
    from lib.conversations import drift_repair
    drift_repair.reset()
    yield
    drift_repair.reset()


# ═══════════════════════════════════════════════════════════════════
# A. Repair policy — pure, unit-testable without a push hub
# ═══════════════════════════════════════════════════════════════════

def _verdict(sustained: bool):
    """A tracker-shaped verdict. Mirrors observe_divergence's return keys."""
    return {'sustained': sustained, 'stalled': sustained, 'age': 200.0,
            'observations': 4, 'direction': 'client_behind',
            'severity': 'warning' if sustained else 'debug'}


def test_sustained_stall_warrants_a_repair():
    """Face 1 — the whole point: a detected stall must produce an action."""
    from lib.conversations.drift_repair import should_repair
    assert should_repair('sock-1', [_verdict(True)]) is True


def test_non_sustained_divergence_does_not_repair():
    """Face 2 — ordinary sampling lag must not spend a frame.

    The overwhelming majority of divergences are a healthy client whose 60s
    digest is simply older than a live server read. Repairing those would emit
    a frame per report per conv and drown the real signal — the same mistake
    that made the P5 WARNING log unusable before the tracker existed.
    """
    from lib.conversations.drift_repair import should_repair
    assert should_repair('sock-1', [_verdict(False)]) is False
    assert should_repair('sock-1', []) is False
    assert should_repair('sock-1', None) is False


def test_cooldown_suppresses_a_second_repair():
    """Face 3 — a repair that is not working must degrade to rare, not flood.

    A stalled client keeps reporting every 60s. Without a cooldown an unhealed
    stall emits a frame every minute for as long as it lasts — turning the
    repair into a slow flood exactly when the transport is already unhealthy.
    """
    from lib.conversations.drift_repair import (note_repair_attempt,
                                               repair_cooldown_sec,
                                               should_repair)
    v = [_verdict(True)]
    assert should_repair('sock-1', v, now=1000.0) is True
    note_repair_attempt('sock-1', now=1000.0)
    assert should_repair('sock-1', v, now=1000.0 + 10) is False, (
        'a second repair inside the cooldown must be suppressed')
    later = 1000.0 + repair_cooldown_sec() + 1
    assert should_repair('sock-1', v, now=later) is True, (
        'after the cooldown elapses a still-stalled socket may be repaired again')
    # A DIFFERENT socket is independent — one tab's cooldown must not shield
    # another tab that is also stalled.
    assert should_repair('sock-2', v, now=1000.0 + 10) is True


def test_untargetable_repair_is_never_attempted():
    """Face 4 — no socket id ⇒ no repair, never a broadcast fallback.

    Broadcasting a correction "somewhere in the fleet" would deliver it to
    healthy tabs, where applying it is indistinguishable from a normal
    reconnect — so the stalled tab might still be broken while the logs claim a
    repair went out. An honest negative is strictly more useful.
    """
    from lib.conversations.drift_repair import should_repair
    assert should_repair('', [_verdict(True)]) is False
    assert should_repair(None, [_verdict(True)]) is False


def test_failed_delivery_does_not_burn_the_cooldown():
    """Face 5 — the cooldown starts on DELIVERY, not on the decision.

    A socket living on another replica (or already gone) yields no delivery. If
    deciding-to-try started the clock, that one unlucky probe would suppress
    repair for the entire window even though nothing was ever sent.
    """
    from lib.conversations.drift_repair import should_repair, tracked_sockets
    v = [_verdict(True)]
    assert should_repair('sock-x', v, now=500.0) is True
    # Caller did NOT call note_repair_attempt (delivery failed).
    assert tracked_sockets() == 0
    assert should_repair('sock-x', v, now=500.0 + 1) is True, (
        'a repair that was never delivered must be retried on the next probe')


# ═══════════════════════════════════════════════════════════════════
# B. Transport — targeted delivery
# ═══════════════════════════════════════════════════════════════════

def test_hub_delivers_to_one_socket_only():
    """Face 6 — deliver_to_socket hits the named socket and nothing else."""
    from lib.agent_core.push import PushClient, PushHub

    hub = PushHub()
    a = PushClient(user_id='u1', req_id='rid-a')
    b = PushClient(user_id='u1', req_id='rid-b')
    hub.register(a)
    hub.register(b)

    frame = {'channel': 'notify', 'type': 'conv_state_snapshot', 'convs': {}}
    assert hub.deliver_to_socket('rid-a', frame) is True

    assert a._queue.qsize() == 1, 'the named socket must receive the frame'
    assert b._queue.qsize() == 0, (
        'a sibling socket must NOT receive a repair aimed at another tab — '
        'fanning out would mask the condition being repaired')


def test_hub_reports_unknown_socket_honestly():
    """An absent socket returns False so the caller can log a real negative."""
    from lib.agent_core.push import PushClient, PushHub
    hub = PushHub()
    hub.register(PushClient(user_id='u1', req_id='rid-a'))
    assert hub.deliver_to_socket('rid-zzz', {'x': 1}) is False
    assert hub.deliver_to_socket('', {'x': 1}) is False


def test_probe_endpoint_wires_detection_to_repair():
    """The endpoint must actually CALL the repair seam — and REACHABLY.

    Faces 1-6 verify the pieces; this verifies they are connected. Structural
    (AST over the view's source) because forcing a real sustained stall through
    HTTP needs 180s of frozen reports. Comments are stripped first (charter
    #24) so prose describing the repair cannot satisfy this.

    ★ NAME-PRESENCE IS NOT ENOUGH — measured. A first version of this guard
    only asserted the symbols appeared somewhere in the function. Neutering the
    repair to ``if False and should_repair(...)`` — i.e. restoring the exact
    bug this epic exists to fix — left the whole suite GREEN, because every
    name was still lexically present. That is the same vacuous shape as a test
    named for a lane it never touches: the guard has to check the CONDITION the
    call hangs off, not just that the call was typed into the file.
    """
    import ast
    import inspect
    import textwrap

    from routes.api_v1 import conversations as conv_mod
    from tests._source_scan import strip_comments

    src = strip_comments(inspect.getsource(conv_mod.sync_digest), lang='python')
    tree = ast.parse(textwrap.dedent(src))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert 'should_repair' in names, (
        'sync_digest must consult drift_repair.should_repair — otherwise the '
        'server detects a stall and still does nothing about it')
    assert 'build_conv_state_snapshot' in names, (
        'the repair must reuse the ORDINARY connect snapshot, so the client '
        'needs no new frame handling and a repair is indistinguishable from a '
        'reconnect')
    assert 'deliver_to_socket' in attrs, (
        'the repair must be delivered to the reporting socket specifically')
    assert 'note_repair_attempt' in names, (
        'a delivered repair must arm the cooldown')

    # ── Reachability: the repair must be gated on should_repair ALONE ──
    # Find the `if` whose test contains the should_repair call, then assert the
    # test is not short-circuited by a constant (False/0/None) — the shape a
    # kill-switch or a debug edit takes.
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        called = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == 'should_repair'
                     for c in ast.walk(node.test))
        if called:
            guards.append(node)
    assert guards, (
        'should_repair must gate an `if` — a call whose result is discarded '
        'detects the stall and still sends nothing')

    for g in guards:
        consts = [n for n in ast.walk(g.test)
                  if isinstance(n, ast.Constant)
                  and (n.value is False or n.value is None or n.value == 0)]
        assert not consts, (
            'the repair branch is short-circuited by a constant '
            '(e.g. `if False and should_repair(...)`), so a detected stall '
            'produces no correction. Guard expression: %s'
            % ast.unparse(g.test))
        # The delivery + cooldown must live INSIDE that branch, not merely
        # somewhere in the function.
        body_src = '\n'.join(ast.unparse(s) for s in g.body)
        assert 'deliver_to_socket' in body_src, (
            'the delivery must happen inside the should_repair branch; '
            'branch body:\n' + body_src)
        assert 'note_repair_attempt' in body_src, (
            'the cooldown must be armed inside the should_repair branch')


# ═══════════════════════════════════════════════════════════════════
# C. Client confidence + rendering
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

const JS_DIR = process.argv[1];
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'core/conv_state_reducer.js'), 'utf8'));

const M = new Map();
const WALL = Date.now() * 1e6;

// ── Face 7: confidence is a SEPARATE dimension; busy is untouched by health ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [WALL, 'r1'] });
  resetAuthoritativeChannelHealthForTests();
  const busyHealthy = computeConvBusy(c, M);
  markAuthoritativeChannelHealth('timeout', true);
  const busyUnhealthy = computeConvBusy(c, M);
  markAuthoritativeChannelHealth('good', false);
  const busyOffline = computeConvBusy(c, M);
  check('F7_busy_is_invariant_to_channel_health',
        busyHealthy === true && busyUnhealthy === true && busyOffline === true,
        'busy must NOT change with channel health — a dead socket flipping busy '
        + 'to false would hide Stop on a running conv. got '
        + [busyHealthy, busyUnhealthy, busyOffline].join(','));
}

// ── Face 8: a live local stream outranks channel health ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  const streams = new Map([['c1', {}]]);
  markAuthoritativeChannelHealth('timeout', true);
  check('F8_live_local_stream_is_confirmed',
        computeConvStateConfidence(c, streams) === 'confirmed',
        'bytes are arriving on THIS conv, so it cannot be stale; got '
        + computeConvStateConfidence(c, streams));
}

// ── Face 9: timeout / offline degrade an authoritative-only verdict ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [WALL, 'r1'] });
  resetAuthoritativeChannelHealthForTests();
  check('F9a_healthy_channel_is_confirmed',
        computeConvStateConfidence(c, M) === 'confirmed');
  markAuthoritativeChannelHealth('timeout', true);
  check('F9b_ping_timeout_is_unconfirmed',
        computeConvStateConfidence(c, M) === 'unconfirmed',
        'got ' + computeConvStateConfidence(c, M));
  markAuthoritativeChannelHealth('good', false);
  check('F9c_disconnected_is_unconfirmed',
        computeConvStateConfidence(c, M) === 'unconfirmed',
        'got ' + computeConvStateConfidence(c, M));
  markAuthoritativeChannelHealth('good', true);
  check('F9d_recovery_returns_to_confirmed',
        computeConvStateConfidence(c, M) === 'confirmed',
        'got ' + computeConvStateConfidence(c, M));
}

// ── Face 10: a slow-but-working link stays confirmed ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  for (const s of ['good', 'ok', 'poor', 'unknown']) {
    markAuthoritativeChannelHealth(s, true);
    check('F10_' + s + '_stays_confirmed',
          computeConvStateConfidence(c, M) === 'confirmed',
          s + ' must not be treated as stale (it still delivers)');
  }
}

// ── Default posture: before any reading exists, do NOT paint the page stale ──
{
  resetAuthoritativeChannelHealthForTests();
  check('F11_default_is_confirmed',
        authoritativeChannelHealthy() === true,
        'the degrade must be opt-IN on positive evidence of trouble');
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_client_confidence_dimension():
    proc = subprocess.run(['node', '-e', _HARNESS, JS_DIR],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = proc.stdout.strip().splitlines()
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('confidence faces failed:\n  ' + '\n  '.join(failed)
                        + '\n\nfull:\n  ' + '\n  '.join(lines))
    assert len(lines) >= 11, (
        'expected the full confidence matrix, got %d:\n%s'
        % (len(lines), '\n'.join(lines)))


def test_unconfirmed_reaches_the_dom():
    """Face 11 — the flag must render, not just exist in the reducer.

    A confidence dimension nobody draws is invisible plumbing: the user would
    still be shown a stale state as settled fact, which is the reported
    symptom. Comments stripped first (charter #24).
    """
    from tests._source_scan import strip_comments
    src = strip_comments(open(CONV_LIST, encoding='utf-8').read(), lang='js')

    assert 'computeConvStateConfidence' in src, (
        'the sidebar must consult the confidence predicate')
    assert 'unconfirmed' in src, 'the flag must exist in the status flags'
    assert 'conv-state-unconfirmed' in src, (
        'the degraded state must reach the DOM as a class the stylesheet can '
        'target')


def test_unconfirmed_is_in_the_status_hash():
    """Face 12 — otherwise the repaint never fires.

    renderConversationList early-returns when its status hash is unchanged. A
    confidence flip changes no other flag, so if ``unconfirmed`` is absent from
    that hash the socket can go down, the flag can flip, and the DOM keeps the
    confident dot until some unrelated change happens to bump the hash. The
    feature would appear to work in a unit test and do nothing in the browser.
    """
    from tests._source_scan import strip_comments
    src = strip_comments(open(CONV_LIST, encoding='utf-8').read(), lang='js')

    m = re.search(r'_statusHash\s*=(.*?)\.join\(', src, re.DOTALL)
    assert m, 'could not locate the sidebar status hash construction'
    block = m.group(1)
    assert 'f.streaming' in block, (
        'located the wrong block — expected the per-conv status flags; got:\n'
        + block)
    assert 'unconfirmed' in block, (
        'f.unconfirmed MUST be part of _statusHash, else a channel-health flip '
        'does not invalidate the cached hash and the sidebar never repaints:\n'
        + block)


def test_i18n_key_exists_for_the_degraded_state():
    """A rendered title must not ship as a literal key.

    Measured precedent in this project: a tooltip shipped as the literal string
    ``project.qrScan`` because the render guard never checked the dictionary.
    """
    i18n = os.path.join(JS_DIR, 'i18n.js')
    src = open(i18n, encoding='utf-8').read()
    assert "'sidebar.stateUnconfirmed'" in src, (
        'sidebar.stateUnconfirmed must exist in i18n.js or the tooltip renders '
        'as a raw key')
    m = re.search(r"'sidebar\.stateUnconfirmed':\s*\{([^}]*)\}", src)
    assert m, 'malformed i18n entry'
    entry = m.group(1)
    assert 'zh:' in entry and 'en:' in entry, (
        'both languages required — the Chinese UI is the primary one here')


def test_css_targets_the_degraded_state():
    """The class must have a rule, or the degrade is semantically present but
    visually identical to a confident dot."""
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    assert '.conv-state-unconfirmed' in css, (
        'styles.css must style the unconfirmed state, else the flag reaches the '
        'DOM and changes nothing the user can see')
