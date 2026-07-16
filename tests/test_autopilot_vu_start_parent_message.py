"""Backend regression: `autopilot_vu_start` carries the parent's SETTLED
finish metadata as `parentMessage`.

WHY
---
When autopilot kicks in, the frontend EARLY-FINALIZES the parent worker bubble
at `autopilot_vu_start` so the VU can own the single `#streaming-msg` substrate.
The parent worker's SETTLED finish metadata (finishReason / usage / cost),
however, is committed to `task['_committedMsg']` by the pre-emit
`_sync_result_to_conversation` and only shipped later on the parent `done`
event — which the backend deliberately WITHHOLDS until the whole VU stream
completes (so the follow-up baton can ride on it). Result: the parent bubble's
finish bar showed ONLY the model tag for the entire VU turn (12–52s), reported
as "the finish tag bar for the previous agent's result is always incomplete".

THE FIX (this suite locks it in): `maybe_run_autopilot` attaches
`task['_committedMsg']` — the EXACT committed dict — onto the
`autopilot_vu_start` event as `parentMessage`, so the frontend can complete the
parent's finish bar AT HANDOFF. The authoritative copy still rides `done`
verbatim (harmless no-op repaint); the skip path (no `_committedMsg`) omits the
field and the frontend falls back to the `done` re-render — both preserved.

NC (bites): drop `task['_committedMsg']` (the backend skip path) and assert the
vu_start event carries NO `parentMessage` — proving the field is sourced from
the committed dict, not fabricated.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import autopilot as ap


def _wire(monkeypatch):
    """Stub the eligibility guards + the VU run so maybe_run_autopilot reaches
    the vu_start emission deterministically and then bails cleanly (no follow-up
    spawn, no DB writes)."""
    import lib.tasks_pkg.manager as mgr

    captured: list = []
    monkeypatch.setattr(mgr, 'append_event',
                        lambda task, event: captured.append(event))

    # Eligibility gauntlet → all pass.
    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid: False)
    monkeypatch.setattr(ap, '_successor_already_running', lambda task, cid: False)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda cid: 'run-1')

    # VU produces no reply → bail right after vu_start (emits vu_cancel, returns
    # None). We only care that vu_start was emitted with the right payload.
    monkeypatch.setattr(ap, 'run_virtual_user', lambda task, vu_msg_id: None)

    return captured


def _make_task(committed=None):
    task = {
        'id': 'parent-abc123',
        'convId': 'conv1',
        'messages': [{'role': 'user', 'content': 'hi'},
                     {'role': 'assistant', 'content': 'done'}],
        'config': {},
        'events': [],
        'events_lock': threading.Lock(),
        'aborted': False,
    }
    if committed is not None:
        task['_committedMsg'] = committed
    return task


_COMMITTED = {
    'role': 'assistant',
    '_msgId': 'worker-1',
    'content': 'Here is the worker reply.',
    'model': 'aws.claude-opus-4.8',
    'finishReason': 'end_turn',
    'usage': {'input_tokens': 4200, 'output_tokens': 380},
    'cost': {'costCny': 0.0123},
}


def _vu_start(captured):
    for e in captured:
        if e.get('type') == 'autopilot_vu_start':
            return e
    return None


def test_vu_start_carries_committed_parent_message(monkeypatch):
    captured = _wire(monkeypatch)
    ap.maybe_run_autopilot(_make_task(committed=_COMMITTED))

    ev = _vu_start(captured)
    assert ev is not None, f'no autopilot_vu_start emitted; got {captured}'
    assert 'parentMessage' in ev, \
        f'vu_start must carry parentMessage; got {ev}'
    pm = ev['parentMessage']
    # It is the EXACT committed dict — the finish-bar-bearing fields are there.
    assert pm['finishReason'] == 'end_turn'
    assert pm['usage']['output_tokens'] == 380
    assert pm['cost']['costCny'] == 0.0123
    assert pm is _COMMITTED, 'parentMessage should be the committed dict itself'


def test_nc_skip_path_omits_parent_message(monkeypatch):
    """NC: on the backend skip path (no `_committedMsg` — freshness/CAS-miss/
    inline), vu_start carries NO `parentMessage`. Proves the field is sourced
    from the committed dict, and the frontend correctly falls back to the
    `done` re-render in that window."""
    captured = _wire(monkeypatch)
    ap.maybe_run_autopilot(_make_task(committed=None))

    ev = _vu_start(captured)
    assert ev is not None, f'no autopilot_vu_start emitted; got {captured}'
    assert 'parentMessage' not in ev, \
        f'skip path must omit parentMessage; got {ev}'
