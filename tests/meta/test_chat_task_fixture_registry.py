"""The chat task fixture registry never hands a test another test's record."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_registering_over_a_record_the_runtime_keeps_fails_loudly():
    """A settled executor's persistence receipt fences discard; re-registering
    its id must not silently keep that record (another test's owner)."""
    from lib.agent_core.task_runtime import TaskRuntime
    from tests.support.chat_tasks import ChatTaskTestRegistry
    registry = ChatTaskTestRegistry(TaskRuntime('chat', ttl=3600, push_channel=None))
    registry['executor'] = {'_userId': 41, 'status': 'done'}
    registry['executor']['_terminalPersistencePending'] = True
    with pytest.raises(ValueError, match='executor'):
        registry['executor'] = {'_userId': 11}
    assert registry['executor']['_userId'] == 41
