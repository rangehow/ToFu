"""A settled task releases its terminal subscribers on every append outcome.

``on_terminal`` callbacks live in a process-global registry until the terminal
signal fires. Measured 2026-11-15 on the execution host: 64 settled attempts
whose terminal frame could not be published (revoked lane, lost worker lease)
kept their callbacks, and through each callback's closure the whole task graph,
for hours.
"""

import gc
import weakref

import pytest

from lib.agent_core import admission
from lib.tasks_pkg.manager._events import append_event

pytestmark = pytest.mark.unit


class _Graph:
    pass


def _subscribe(task):
    calls = []
    graph = _Graph()
    task['graph'] = graph

    def cleanup(task_id):
        calls.append(task_id)
        task.pop('graph', None)

    admission.on_terminal(task['id'], cleanup)
    return calls, weakref.ref(graph)


@pytest.mark.parametrize('status', ['done', 'aborted', 'error'])
def test_unpublishable_terminal_frame_still_releases_terminal_subscribers(status):
    task = {'id': f'terminal-subscriber-{status}', 'status': status,
            '_invocationPublicationClosed': True}
    calls, graph_alive = _subscribe(task)
    with pytest.raises(RuntimeError):
        append_event(task, {'type': 'done'})
    assert calls == [task['id']]
    assert task['id'] not in admission._term_callbacks
    gc.collect()
    assert graph_alive() is None


def test_running_task_keeps_subscribers_when_a_progress_frame_fails():
    task = {'id': 'terminal-subscriber-running', 'status': 'running',
            '_invocationPublicationClosed': True}
    calls, _graph_alive = _subscribe(task)
    try:
        with pytest.raises(RuntimeError):
            append_event(task, {'type': 'delta', 'content': 'x'})
        assert calls == []
        assert task['id'] in admission._term_callbacks
    finally:
        admission.fire_terminal_callbacks(task['id'])
