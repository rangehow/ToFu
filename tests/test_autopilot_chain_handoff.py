"""tests/test_autopilot_chain_handoff.py — TARGET contract for epic
pt_8dc030176bad450b (Autopilot VU as an independent stream).

STATUS: SKIPPED (the whole module) until the cutover commit. This suite is the
migration-test plan the owner named as the precondition for touching the
exactly-once baton contract ("先写迁移测试再动"). It codifies the TARGET behavior so
that when the cutover happens, these tests are the acceptance gate — and it lives
in-tree NOW (skipped) so the design intent is reviewable and the collection gate
stays green (6k+ collection).

See docs/EPIC_AUTOPILOT_INDEPENDENT_STREAM_DESIGN.md for the full rationale.

WHY THE CURRENT MECHANISM MUST CHANGE
-------------------------------------
Today the VU runs SYNCHRONOUSLY inside the parent's finalize
(`maybe_run_autopilot` → `_run_single_turn`, autopilot.py:949), its events ride
the parent SSE via `_VUEventForwarder`, and the parent `done` is WITHHELD (gated
by `task['_autopilot_deciding']`) until the VU finishes — because `done` must
carry the baton (`autopilotNextTaskId`/`autopilotVuMessage`), which can only be
computed after the VU decides. That withhold is the root of the "parent bar
incomplete for the whole VU turn" symptom (already independently root-fixed via
`parentMessage` projection, commits 589cfaa/b221921/9ce7d93 — this epic is the
deeper structural cleanup, NOT that bug's fix).

TARGET
------
An autopilot run becomes a plain sequence of INDEPENDENT agent bubbles
(parent → VU → follow-up), each on its own task/stream. The baton is replaced by
a transport-agnostic `task['_autopilot_chain']` descriptor carrying STABLE ids
(charter front/back contract: backend computes the lifecycle fact, frontend is a
pure reducer):

    {vuTaskId, vuMsgId, nextTaskId?, state}
    state ∈ {'vu_running', 'spawned', 'done_no_followup'}

- Parent `done` fires IMMEDIATELY at parent-turn end, carrying
  `autopilotChain{vuTaskId, vuMsgId, state:'vu_running'}` (no withhold, no
  `_autopilot_deciding`).
- The VU is a normal child task; the frontend attaches to its own stream.
- The VU's own `done` carries `autopilotChain{nextTaskId, state:'spawned'}`
  (or `state:'done_no_followup'` when the VU emitted TASK_DONE).
- Poll fallback + cold-replay both read `task['_autopilot_chain']` from the task
  dict, so both transports + a fresh mid-chain connection resolve the SAME state.

The four exactly-once guards from test_autopilot_poll_handoff.py are re-expressed
below against the chain descriptor. Keep test_autopilot_poll_handoff.py green
until the cutover commit, which replaces it with THIS suite in the same commit
(the two describe mutually-exclusive worlds — the withhold either exists or it
doesn't, so no strangler-fig dual-run is possible).
"""

import pytest

# The whole module is inert until the cutover. Removing this skip is part of the
# HUMAN-GATED cutover commit (step 4 in the design doc's build order).
pytestmark = pytest.mark.skip(
    reason='epic pt_8dc030176bad450b (autopilot independent stream) not yet cut '
           'over — target-contract suite, activated in the cutover commit'
)


_VU_MSG = {
    'role': 'user',
    'content': 'Yes, wire the breaker state into the API.',
    '_msgId': 'vu-msg-id-1',
    '_isVirtualUser': True,
}


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup.

    (Same fixture shape as test_autopilot_poll_handoff.py — kept independent so
    the two suites don't share a conftest coupling across the cutover.)
    """
    from lib.tasks_pkg import tasks, tasks_lock
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def _poll(client, task_id):
    resp = client.get(f'/api/v1/chat/poll/{task_id}')
    return resp.status_code, resp.get_json()


def _make_full_task(task_id, **overrides):
    from lib.tasks_pkg.manager import create_task
    task = create_task('cv-chain-' + task_id, [{'role': 'user', 'content': 'q'}], {})
    task['id'] = task_id
    task.update(overrides)
    return task


def _sse_collect(client, task_id, max_chars=20000):
    resp = client.get(f'/api/chat/stream/{task_id}')
    return resp.get_data(as_text=True)[:max_chars]


# ── Guard 1 (was test_baton_surfaced_when_followup_spawned) ──
@pytest.mark.api
def test_chain_vu_running_surfaced_on_parent_done(flask_client, put_task):
    """Parent done with an armed chain surfaces autopilotChain{state:'vu_running'}
    carrying the VU task+msg ids, and NOT a nextTaskId yet (the VU hasn't decided).
    The successor is never stranded: the frontend attaches to vuTaskId's stream."""
    put_task({
        'id': 'chain-parent-1', 'status': 'done',
        'content': 'Sure, I will add a public method.', 'thinking': '',
        'finishReason': 'stop',
        '_autopilot_chain': {
            'vuTaskId': 'vu-task-aaaa', 'vuMsgId': 'vu-msg-id-1',
            'state': 'vu_running',
        },
    })
    status, body = _poll(flask_client, 'chain-parent-1')
    assert status == 200, body
    assert body['status'] == 'done'
    ch = body['autopilotChain']
    assert ch['vuTaskId'] == 'vu-task-aaaa'
    assert ch['vuMsgId'] == 'vu-msg-id-1'
    assert ch['state'] == 'vu_running'
    assert ch.get('nextTaskId') is None


# ── Guard 2 (was test_status_gated_while_deciding + the SSE hold test) ──
@pytest.mark.api
def test_parent_done_fires_immediately_no_withhold(flask_client, put_task):
    """With the withhold removed, a parent SSE with autopilot armed emits its
    done PROMPTLY (state snapshot → done, no multi-second hold), and the done
    carries state:'vu_running' so the conv stays live by attaching to the VU
    stream — NOT idle. The `_autopilot_deciding` latch is gone."""
    task = _make_full_task('chain-parent-sse-1', status='done',
                           content='partial')
    task['_autopilot_chain'] = {
        'vuTaskId': 'vu-task-bbbb', 'vuMsgId': 'vu-msg-id-1',
        'state': 'vu_running',
    }
    # No _autopilot_deciding key exists in the target world.
    assert '_autopilot_deciding' not in task
    put_task(task)
    body = _sse_collect(flask_client, 'chain-parent-sse-1')
    assert '"type": "done"' in body or '"type":"done"' in body, body[:600]
    assert 'vu-task-bbbb' in body, \
        f'parent done must carry the vu_running chain: {body[:800]}'


# ── Guard for the VU's own terminal event ──
@pytest.mark.api
def test_vu_task_own_stream_carries_chain_spawned(flask_client, put_task):
    """The VU task's OWN done carries autopilotChain{nextTaskId, state:'spawned'}
    so the frontend attaches to the follow-up over the same _attachAutopilotFollowup
    code path (now keyed on the chain, not the legacy baton fields)."""
    put_task({
        'id': 'vu-task-cccc', 'status': 'done',
        'content': 'VU reply.', 'thinking': '', 'finishReason': 'stop',
        '_isVirtualUserTask': True,
        '_autopilot_chain': {
            'vuTaskId': 'vu-task-cccc', 'vuMsgId': 'vu-msg-id-1',
            'nextTaskId': 'next-task-cccc', 'state': 'spawned',
        },
    })
    status, body = _poll(flask_client, 'vu-task-cccc')
    assert status == 200, body
    ch = body['autopilotChain']
    assert ch['state'] == 'spawned'
    assert ch['nextTaskId'] == 'next-task-cccc'


@pytest.mark.api
def test_vu_task_done_no_followup_state(flask_client, put_task):
    """VU emitted TASK_DONE → VU done carries state:'done_no_followup', no
    nextTaskId — the chain terminates cleanly, no spurious successor attach."""
    put_task({
        'id': 'vu-task-dddd', 'status': 'done',
        'content': '[VU: TASK_DONE]', 'thinking': '', 'finishReason': 'stop',
        '_isVirtualUserTask': True,
        '_autopilot_chain': {
            'vuTaskId': 'vu-task-dddd', 'vuMsgId': 'vu-msg-id-1',
            'state': 'done_no_followup',
        },
    })
    status, body = _poll(flask_client, 'vu-task-dddd')
    assert status == 200, body
    ch = body['autopilotChain']
    assert ch['state'] == 'done_no_followup'
    assert ch.get('nextTaskId') is None


# ── Guard 3 (was test_normal_done_task_unaffected + closes_promptly) ──
@pytest.mark.api
def test_plain_done_no_chain(flask_client, put_task):
    """A plain non-autopilot done: no chain keys, closes promptly. The chain
    machinery must not touch normal turns."""
    task = _make_full_task('chain-plain-1', status='done',
                           content='Done.', finishReason='stop')
    put_task(task)
    status, body = _poll(flask_client, 'chain-plain-1')
    assert status == 200, body
    assert body['status'] == 'done'
    assert 'autopilotChain' not in body
    sse = _sse_collect(flask_client, 'chain-plain-1')
    assert '"type": "done"' in sse or '"type":"done"' in sse, sse[:600]
    assert 'autopilotChain' not in sse


# ── Guard 4 (was test_sse_synthetic_done_carries_baton) ──
@pytest.mark.api
def test_cold_replay_synthetic_done_carries_chain(flask_client, put_task):
    """Cold-replay: a fresh connection lands with no buffered done event; the
    snapshot branch synthesizes a done from extract_task_meta(). That synthetic
    done MUST still stamp autopilotChain from task['_autopilot_chain'] — else a
    mid-chain reload strands the VU/follow-up and the conv goes idle."""
    task = _make_full_task('chain-synth-1', status='done',
                           content='partial', finishReason='stop')
    task['_autopilot_chain'] = {
        'vuTaskId': 'vu-task-eeee', 'vuMsgId': 'vu-msg-id-1',
        'state': 'vu_running',
    }
    put_task(task)
    body = _sse_collect(flask_client, 'chain-synth-1')
    assert '"type": "done"' in body or '"type":"done"' in body, body[:600]
    assert 'vu-task-eeee' in body, \
        f'synthetic done dropped the autopilot chain: {body[:800]}'


# ── Transport-parity guard ──
@pytest.mark.api
def test_poll_and_sse_surface_same_chain(flask_client, put_task):
    """The poll route and the SSE snapshot must surface the SAME chain from the
    task dict — a client on either transport resolves identical state."""
    task = _make_full_task('chain-parity-1', status='done',
                           content='x', finishReason='stop')
    task['_autopilot_chain'] = {
        'vuTaskId': 'vu-task-ffff', 'vuMsgId': 'vu-msg-id-1',
        'state': 'vu_running',
    }
    put_task(task)
    _, poll_body = _poll(flask_client, 'chain-parity-1')
    sse_body = _sse_collect(flask_client, 'chain-parity-1')
    assert poll_body['autopilotChain']['vuTaskId'] == 'vu-task-ffff'
    assert 'vu-task-ffff' in sse_body
