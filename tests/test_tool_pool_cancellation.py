"""Pool Stop observes silent workers and drains resource owners before settlement."""
import threading
from concurrent.futures import Future

import pytest

from tests.test_tool_settle_immediacy import _mk_task, _mk_tc, _run_pipeline
from tests.test_tool_settle_immediacy import rec as recorder_fixture
from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
from lib.tasks_pkg.tool_dispatch import _pipeline

pytestmark = pytest.mark.unit

# Register the imported fixture explicitly without shadowing its Python name.
rec = recorder_fixture


@pytest.mark.parametrize('finish_first', [False, True])
def test_stop_cancels_silent_parallel_resources_before_terminal(monkeypatch, rec, finish_first):
    task = _mk_task()
    items = [_mk_tc('one', 'get_conversation', 1), _mk_tc('two', 'grep_search', 2)]
    started = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    exited = [threading.Event(), threading.Event()]
    errors = []

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        i = rn - 1
        context = context_for_task(task, round_num=rn, tool_call_id=call_id,
                                   tool_name=name, round_entry=row)
        context.register_cancel_callback(releases[i].set)
        started[i].set()
        try:
            assert releases[i].wait(5), 'test cleanup failed to release worker'
            # A real handler may still write state after its resource closes.
            # This must happen BEFORE the pipeline publishes terminal receipts.
            row['lastWorkerWrite'] = 'released'
            from lib.tasks_pkg.executor._finalize import _finalize_tool_round
            _finalize_tool_round(task, rn, row, [])
            context.settle_once('cancelled' if task['aborted'] else 'done')
            return call_id, 'worker result', False
        finally:
            unregister_context(context)
            exited[i].set()

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)
    monkeypatch.setattr(_pipeline, '_parallel_worker_limit', lambda *a, **k: 2)

    def run():
        try:
            _run_pipeline(task, items)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert all(event.wait(2) for event in started)
        if finish_first:
            releases[0].set()
            assert exited[0].wait(2)
        task['aborted'] = True
        thread.join(2)
        assert not thread.is_alive(), 'silent pool did not observe Stop/release resources'
        assert not errors
        assert all(event.is_set() for event in exited)
        assert not task.get('_activeToolContexts')
        for item in items:
            event = rec.find(item[2], 'tool_complete')
            assert event is not None
            assert event.get('status', item[5]['status']) in (
                {'done', 'aborted'} if finish_first else {'aborted'})
            assert 'Unknown tool' not in str(event)
        assert items[1][5]['status'] == 'aborted'
    finally:
        for release in releases:
            release.set()
        thread.join(5)
        assert not thread.is_alive()


def test_abort_before_harvesting_done_future_preserves_receipt():
    future = Future()
    future.set_result(('one', 'late result', False))
    assert list(_pipeline._completed_tools_until_cancel(
        {future}, {'aborted': True}, 0)) == [future, None]


def test_done_but_uncollected_stop_keeps_real_pipeline_receipt(monkeypatch, rec):
    task = _mk_task()
    item = _mk_tc('done', 'get_conversation', 1)
    original = _pipeline._completed_tools_until_cancel

    def stop_before_collection(futures, task, deadline):
        for future in futures:
            future.result(timeout=2)
        task['aborted'] = True
        yield from original(futures, task, deadline)

    monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', stop_before_collection)
    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(task, rn, row, [])
        return call_id, 'real completed receipt', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)
    messages = _run_pipeline(task, [item])
    assert item[5]['status'] == 'done'
    assert item[5]['toolContent'] == 'real completed receipt'
    assert messages[-1]['content'] == 'real completed receipt'
    assert rec.find('done', 'tool_result')['status'] == 'done'
    assert rec.find('done', 'tool_complete')['toolContent'] == 'real completed receipt'
    assert rec.count('done', 'tool_complete') == 1


@pytest.mark.parametrize('blocking_callback', [False, True])
def test_timeout_returns_without_draining_noncooperative_owner(monkeypatch, rec, blocking_callback):
    task = _mk_task()
    item = _mk_tc('timeout', 'get_conversation', 1)
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    callback_called = threading.Event()
    errors = []

    def callback():
        callback_called.set()
        release.wait(5)

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        context = context_for_task(task, round_num=rn, tool_call_id=call_id,
                                   tool_name=name, round_entry=row)
        if blocking_callback:
            context.register_cancel_callback(callback)
        started.set()
        try:
            release.wait(5)
            # Honest legacy boundary: a timed-out thread is still able to write.
            row['lateWrite'] = True
            return call_id, 'too late', False
        finally:
            unregister_context(context)
            exited.set()

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '1')

    def run():
        try:
            _run_pipeline(task, [item])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(2)
        thread.join(2)
        assert not thread.is_alive(), 'timeout must not drain arbitrary threads/callbacks'
        assert not errors
        assert not exited.is_set()
        assert not callback_called.is_set()
        assert task.get('_activeToolContexts'), 'timeout does not kill the resource owner'
        assert item[5]['status'] == 'error'
        assert 'timed out' in item[5]['toolContent']
        assert rec.count('timeout', 'tool_complete') == 1
    finally:
        release.set()
        thread.join(5)
        assert exited.wait(2)
    assert item[5]['lateWrite'] is True


def test_noncooperative_worker_retains_ownership_until_cleanup(monkeypatch, rec):
    task = _mk_task()
    item = _mk_tc('owned', 'get_conversation', 1)
    started = threading.Event()
    release = threading.Event()
    cancellation_observed = threading.Event()
    errors = []

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        context = context_for_task(task, round_num=rn, tool_call_id=call_id,
                                   tool_name=name, round_entry=row)
        context.register_cancel_callback(cancellation_observed.set)
        started.set()
        try:
            release.wait(5)
            row['lastWorkerWrite'] = 'before terminal'
            return call_id, 'late result', False
        finally:
            context.settle_once('cancelled')
            unregister_context(context)

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)

    def run():
        try:
            _run_pipeline(task, [item])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(2)
        task['aborted'] = True
        assert cancellation_observed.wait(2)
        assert thread.is_alive()
        assert rec.find('owned', 'tool_complete') is None
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert item[5]['lastWorkerWrite'] == 'before terminal'
    assert item[5]['status'] == 'aborted'
    assert rec.count('owned', 'tool_complete') == 1
