"""An expired settled task is freed by reference counting, not a cyclic collection.

Measured 2026-11-15 on the execution host: after the four checkpoint fixes,
21 settled tasks remained. 11 were translate registry entries (covered by the
runtime TTL sweep); 10 chat tasks had already left the chat registry and were
held only through their own reference cycles: the execution account's
``_active`` map, observer closures, a progress sink's ``_task``, a refused
progress frame's copied context (``DispatchAdmission.root``) and stored
exceptions whose traceback frames hold the task. CPython reclaims such cycles
only in a full collection, which a large heap makes rare, so each kept its
replay window for hours. These specifications run with the cyclic collector
disabled: only reference counting may free the graph.
"""

import gc
import time
import weakref

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget
from lib.agent_core.task_runtime import TaskRuntime

pytestmark = pytest.mark.unit


class _Probe:
    """Lives exactly as long as the task graph that holds it."""


@pytest.fixture
def no_cyclic_collection():
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


def _settled_cyclic_task(runtime, task_id):
    task = runtime.create(user_id=1, task_id=task_id)
    task['events'].append({'type': 'messages_snapshot', 'messages': ['x' * 4096]})
    task['_probe'] = _Probe()
    _ensure_execution_budget(task)
    task['_executionModelAttemptObserver'] = lambda *_args, task=task: task.get('id')
    try:
        raise RuntimeError('publication refused')
    except RuntimeError as exc:
        task['_storedFailure'] = exc
    task.update(status='done', finished_at=time.time() - 3600)
    return weakref.ref(task['_probe'])


def test_ttl_expiry_frees_a_cyclic_settled_task_without_a_cyclic_collection(no_cyclic_collection):
    runtime = TaskRuntime('evicted-release-ttl', ttl=60)
    probe = _settled_cyclic_task(runtime, 'evicted-ttl')

    assert runtime.cleanup_stale() == 1

    assert probe() is None


def test_capacity_eviction_frees_the_victim_without_a_cyclic_collection(no_cyclic_collection):
    runtime = TaskRuntime('evicted-release-capacity', ttl=86_400)
    runtime.max_tasks = 1
    probe = _settled_cyclic_task(runtime, 'evicted-capacity')

    runtime.create(user_id=1, task_id='evicted-capacity-successor')

    assert runtime.get('evicted-capacity') is None
    assert probe() is None


def test_released_carrier_keeps_its_scalar_identity():
    runtime = TaskRuntime('evicted-release-identity', ttl=60)
    task = runtime.create(user_id=1, task_id='evicted-identity')
    task.update(status='done', finished_at=time.time() - 3600, convId='conv-1')

    runtime.cleanup_stale()

    assert task['id'] == 'evicted-identity'
    assert task['status'] == 'done'
    assert task['convId'] == 'conv-1'
    assert 'events' not in task and 'abort_event' not in task


def test_a_carrier_owing_delivery_or_registered_elsewhere_is_left_intact():
    runtime = TaskRuntime('evicted-release-owing', ttl=60)
    other = TaskRuntime('evicted-release-other', ttl=86_400)
    owing = runtime.create(user_id=1, task_id='evicted-owing')
    owing.update(status='done', finished_at=time.time() - 3600, _eventDeliveryPending=True)
    shared = runtime.create(user_id=1, task_id='evicted-shared')
    shared['kind'] = None
    shared['status'] = 'running'
    assert other.adopt(shared)
    shared.update(status='done', finished_at=time.time() - 3600)

    runtime.cleanup_stale()

    assert runtime.get('evicted-owing') is None
    assert isinstance(owing['events'], list)
    assert other.get('evicted-shared') is shared
    assert isinstance(shared['events'], list)
