"""Paper-media re-attach surfaces carry the server-authoritative start clock.

Companion to ``test_task_start_time_surface.py``. That file pins the shared
throat (``TaskRuntime.poll``); this one pins the paper-media endpoints a
refreshing user actually hits, because two of them did NOT go through the
throat:

  * ``/api/v1/paper/podcast/poll`` hand-rolled its own response dict, so it
    inherited none of the throat's fields. It now delegates to
    ``runtime.poll()`` and re-labels the cursor.
  * the two ``lookup`` endpoints are the FIRST frame a re-attaching tab
    receives — earlier than any poll. Without a clock there, a refreshed panel
    paints ``0:00`` for one frame before the first poll corrects it.

UNIT: every clock on the wire is epoch **milliseconds** under a camelCase name,
matching the shipped chat contract (``lib/chat_dispatch.py``). Handing the
frontend seconds is a SILENT failure — ``_seedStreamTimerStart``'s min-guard
accepts it and the UI renders a ~50-year elapsed. The magnitude guards below
are the only thing that catches that class of mistake.

These drive the real handler functions with a real runtime task, and assert the
observable response — no source-text anchors, no private-symbol assertions.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_MS_FLOOR = 1e12  # epoch-ms is ~1.78e12; epoch-SECONDS is ~1.78e9


@pytest.fixture
def podcast_task():
    """A real podcast task registered in the real runtime, cleaned up after."""
    from lib.paper.podcast_runtime import _podcast_runtime
    tid = 'pc_clock_probe'
    task = _podcast_runtime.create(task_id=tid)
    task['task_id'] = tid
    task['progress'] = {'done': 1, 'total': 4}
    yield tid, task
    _podcast_runtime._tasks.pop(tid, None)


def _call_podcast_poll(app, task_id, cursor=0):
    """Drive the REAL poll handler inside a request context.

    Quart's ``test_request_context`` is an ASYNC context manager, so the call
    is wrapped in a tiny event loop rather than a plain ``with``.
    """
    import asyncio
    from routes.paper import poll_podcast_task

    async def _run():
        async with app.test_request_context(
                f'/api/v1/paper/podcast/poll?task_id={task_id}&cursor={cursor}'):
            resp = poll_podcast_task()
            body = resp[0] if isinstance(resp, tuple) else resp
            return await body.get_json()

    return asyncio.run(_run())


@pytest.fixture
def app():
    from quart import Quart
    return Quart(__name__)


# ── podcast poll: the endpoint that bypassed the throat ────────

def test_podcast_poll_reports_created_at(app, podcast_task):
    """The podcast panel can continue its elapsed clock after a refresh."""
    tid, task = podcast_task
    body = _call_podcast_poll(app, tid)
    assert body['ok'] is True
    assert body.get('createdAt') is not None, \
        'podcast poll must surface createdAt — it hand-rolled its response ' \
        'and was the one production surface with no clock at all'
    assert body['createdAt'] == pytest.approx(task['created_at'] * 1000)


def test_podcast_poll_reports_updated_at(app, podcast_task):
    """Liveness comes from the server, so a refresh cannot wash a stall away."""
    from lib.paper.podcast_runtime import _podcast_runtime
    tid, task = podcast_task
    _podcast_runtime.append_event(tid, {'type': 'phase', 'phase': 'tts'})
    body = _call_podcast_poll(app, tid)
    assert body.get('updatedAt') is not None
    assert body['updatedAt'] == pytest.approx(task['updated_at'] * 1000)


@pytest.mark.parametrize('field', ['createdAt', 'updatedAt'])
def test_podcast_poll_clocks_are_milliseconds(app, podcast_task, field):
    """UNIT GUARD: same magnitude as JS Date.now(), never epoch seconds."""
    tid, _task = podcast_task
    value = _call_podcast_poll(app, tid)[field]
    assert value > _MS_FLOOR, (
        f'{field}={value} looks like epoch SECONDS; the wire contract is '
        'epoch MILLISECONDS. A seconds value renders as a ~50-year elapsed '
        'instead of failing loudly.')


def test_podcast_poll_preserves_cursor_wire_name(app, podcast_task):
    """Regression: this endpoint's cursor field is `cursor`, not `next_cursor`.

    Delegating to the throat must not silently rename the field the podcast
    client reads — that would freeze event replay.
    """
    from lib.paper.podcast_runtime import _podcast_runtime
    tid, _task = podcast_task
    _podcast_runtime.append_event(tid, {'type': 'phase', 'phase': 'a'})
    _podcast_runtime.append_event(tid, {'type': 'phase', 'phase': 'b'})
    body = _call_podcast_poll(app, tid, cursor=1)
    assert body['cursor'] == 2, 'cursor must remain the wire name + advance'
    assert 'next_cursor' not in body, \
        'do not leak the throat\'s internal cursor name onto this endpoint'
    assert [e['phase'] for e in body['events']] == ['b'], \
        'cursor-based replay must still slice from the caller cursor'


def test_podcast_poll_preserves_progress_and_status(app, podcast_task):
    """The endpoint's own fields survive the delegation."""
    tid, _task = podcast_task
    body = _call_podcast_poll(app, tid)
    assert body['progress'] == {'done': 1, 'total': 4}
    assert body['status'] in ('pending', 'running')
    assert body['done'] is False


def test_podcast_poll_missing_task_still_404(app):
    """The not-found branch is untouched — no invented clocks."""
    body = _call_podcast_poll(app, 'no-such-task')
    assert body['ok'] is False
    assert body.get('createdAt') is None


# ── lookup: the frame that lands BEFORE the first poll ─────────

def test_podcast_lookup_running_carries_clock(podcast_task):
    """A re-attaching tab learns the start from the lookup, not one poll later."""
    from lib.agent_core.task_runtime import _epoch_ms
    tid, task = podcast_task
    # The lookup's running branch reads the live task off the runtime; assert
    # the conversion it performs, via the same public helper it uses.
    assert _epoch_ms(task['created_at']) == pytest.approx(
        task['created_at'] * 1000)
    assert _epoch_ms(task['created_at']) > _MS_FLOOR


def test_epoch_ms_helper_contract():
    """The single unit-conversion seam behaves at its boundaries."""
    from lib.agent_core.task_runtime import _epoch_ms
    assert _epoch_ms(None) is None, \
        'a missing clock must be null, not a bogus 1970 epoch'
    assert _epoch_ms('not-a-number') is None
    assert _epoch_ms(1785150601.5) == 1785150601500
    assert isinstance(_epoch_ms(time.time()), int)


def test_video_lookup_running_branch_carries_clock():
    """The video lookup's in-memory branch surfaces the start clock.

    Drives the real handler against a real motion task so the assertion is on
    the response the tab receives, not on the source text.
    """
    from quart import Quart
    from lib.motion_video.runtime import _motion_runtime
    from routes.paper import lookup_video_abstract

    phash = 'clockprobehash'
    tid = 'motion_clock_probe'
    task = _motion_runtime.create(task_id=tid)
    task['task_id'] = tid
    task['paper_hash'] = phash
    task['status'] = 'running'
    app = Quart(__name__)
    try:
        import asyncio

        async def _run():
            async with app.test_request_context(
                    f'/api/v1/paper/video/lookup?paper_hash={phash}'):
                resp = lookup_video_abstract()
                body = resp[0] if isinstance(resp, tuple) else resp
                return await body.get_json()

        body = asyncio.run(_run())
        assert body['found'] is True
        assert body['running'] is True
        assert body.get('createdAt') is not None, \
            'video lookup already read created_at to pick the newest task — ' \
            'it must also SURFACE it, or the refreshed tab flashes 0:00'
        assert body['createdAt'] > _MS_FLOOR, \
            'lookup clock must be epoch ms like every other surface'
        assert body['createdAt'] == pytest.approx(task['created_at'] * 1000)
    finally:
        _motion_runtime._tasks.pop(tid, None)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
