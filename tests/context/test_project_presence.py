"""Tests for the project_presence context block: the Coordinator's live same-project conversations."""
import time
from unittest.mock import patch

import pytest

from lib.tasks_pkg.context_composer._models import ComposeRequest
from lib.tasks_pkg.context_composer._providers import _live_project_peers, _project_presence_block

pytestmark = pytest.mark.unit


def _request(task=None, conv_id='', user_id=1, project_path='/test/project'):
    return ComposeRequest(
        project_path=project_path,
        project_enabled=True,
        conv_id=conv_id,
        user_id=user_id,
        task=task or {},
    )


def _mock_binding(*, available=True, conversation_id='coord-1'):
    return {
        'projectPath': '/test/project',
        'availability': 'available' if available else 'unavailable',
        'conversationId': conversation_id,
    }


def _mock_task(conv_id='worker-1', status='running', updated_at=None, events=None, title='Test task',
               project_path='/test/project', workspace=None):
    task = {
        'convId': conv_id,
        'status': status,
        'updated_at': updated_at if updated_at is not None else time.time(),
        'events': events or [],
        'config': {'projectPath': project_path},
        'meta': {'title': title},
    }
    if workspace:
        task['_workspaceDelivery'] = {'repositories': [
            {'workspacePath': workspace, 'canonicalRoot': '/test/project'}]}
    return task


def _presence(req, peers, binding=None):
    with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
         patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
        repo_cls.return_value.read.return_value = binding or _mock_binding()
        runtime.snapshot_owned.return_value = peers
        live = _live_project_peers(req)
    return live, _project_presence_block(live)


class TestProjectPresenceBlock:

    def test_empty_when_not_coordinator(self):
        live, block = _presence(_request(conv_id='not-coordinator'), [_mock_task()])
        assert live is None and block == ''

    def test_empty_when_binding_unavailable(self):
        live, block = _presence(_request(conv_id='coord-1'), [_mock_task()], _mock_binding(available=False))
        assert live is None and block == ''

    def test_empty_when_no_peers(self):
        assert _presence(_request(conv_id='coord-1'), []) == ({}, '')

    def test_renders_running_worker_with_stable_bytes(self):
        req = _request(conv_id='coord-1')
        peer = _mock_task(conv_id='worker-a1b2', updated_at=time.time() - 60,
                          events=[{'type': 'tool_call', 'name': 'read_files'}], title='Refactor auth module')
        _, result = _presence(req, [peer])
        assert result == '[Project Presence]\n● "Refactor auth module" (worker-a1b2) — running'
        # A later event or minute does not change the block, so the composer reuses it.
        peer['updated_at'] = time.time() - 30
        peer['events'].append({'type': 'text', 'text': 'more output'})
        assert _presence(req, [peer])[1] == result

    def test_renders_stalled_worker(self):
        peer = _mock_task(conv_id='worker-c3d4', updated_at=time.time() - 300, title='Fix pagination bug')
        _, result = _presence(_request(conv_id='coord-1'), [peer])
        assert '◐' in result and 'stalled' in result

    @pytest.mark.parametrize('status', ['done', 'error', 'aborted', 'unknown_state'])
    def test_finished_tasks_are_not_presence(self, status):
        _, result = _presence(_request(conv_id='coord-1'), [_mock_task(status=status)])
        assert result == ''

    def test_the_coordinator_itself_is_not_presence(self):
        _, result = _presence(_request(conv_id='coord-1'), [_mock_task(conv_id='coord-1')])
        assert result == ''

    def test_one_row_per_conversation_newest_task_wins(self):
        older = _mock_task(conv_id='worker-1', updated_at=time.time() - 30, title='First turn')
        newer = _mock_task(conv_id='worker-1', updated_at=time.time() - 5, title='Follow-up')
        live, result = _presence(_request(conv_id='coord-1'), [older, newer])
        assert list(live) == ['worker-1'] and result.count('worker-1') == 1 and 'Follow-up' in result

    def test_a_worker_in_its_isolated_checkout_is_this_projects_presence(self):
        peer = _mock_task(conv_id='worker-iso', project_path='/tmp/worktrees/worker-iso',
                          workspace='/tmp/worktrees/worker-iso')
        _, result = _presence(_request(conv_id='coord-1'), [peer])
        assert 'worker-iso' in result

    def test_filters_by_project_path(self):
        peer_same = _mock_task(conv_id='worker-1', title='Same project')
        peer_other = _mock_task(conv_id='worker-2', title='Other project', project_path='/other/project')
        _, result = _presence(_request(conv_id='coord-1'), [peer_same, peer_other])
        assert 'worker-1' in result
        assert 'worker-2' not in result
        assert 'Other project' not in result


def test_storage_free_runtime_skips_presence_without_reading_a_binding():
    from lib.tasks_pkg.context_composer._providers import _project_blocks

    req = _request(task={'config': {'_storageFreeRuntime': True}}, conv_id='coord-1')
    with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls:
        blocks = {block.id: block for block in _project_blocks(req, '')}
    repo_cls.assert_not_called()
    assert blocks['project_presence'].content == ''
    assert blocks['project_presence'].suppressed_reason == 'project_disabled'
