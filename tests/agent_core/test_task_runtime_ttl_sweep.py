"""Every TaskRuntime expires its terminal records by its own declared TTL.

Measured 2026-11-15 on the execution host: translation tasks of the
``translate`` runtime stayed resident for up to 9 h past a 30-minute TTL,
because only the chat runtime had a periodic sweep.
"""

import time

import pytest

from lib.agent_core.task_runtime import TaskRuntime, cleanup_stale_task_runtimes

pytestmark = pytest.mark.unit


def test_shared_sweep_expires_every_runtime_by_its_own_ttl():
    short = TaskRuntime('ttl-sweep-short', ttl=60)
    long = TaskRuntime('ttl-sweep-long', ttl=86_400)
    expired = short.create(user_id=1, task_id='ttl-expired')
    expired.update(status='done', finished_at=time.time() - 120)
    fresh = short.create(user_id=1, task_id='ttl-fresh')
    fresh.update(status='done', finished_at=time.time())
    running = short.create(user_id=1, task_id='ttl-running')
    running['status'] = 'running'
    kept = long.create(user_id=1, task_id='ttl-long-kept')
    kept.update(status='error', finished_at=time.time() - 120)

    assert cleanup_stale_task_runtimes() >= 1

    assert short.get('ttl-expired') is None
    assert short.get('ttl-fresh') is fresh
    assert short.get('ttl-running') is running
    assert long.get('ttl-long-kept') is kept
