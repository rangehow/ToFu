"""A Worker submits its own checkpoint under every grant that checkpoints (owner decision 2026-11-18).

``integration_submit`` only saves the final checkpoint of the Worker's own
isolated task workspace, the workspace ``integration_checkpoint`` captures
(docs/PROJECT_COORDINATION.md, ``workerDeniedTools``). A request's Worker
policy is frozen at its first Coordinator Turn and every continuation of that
request, a restart resume included, issues new Workers from it, so the grants of
requests that began before the decision list ``integration_checkpoint`` without
``integration_submit``. ``scope.granted_tools`` derives the submit from the
checkpoint, so those Workers, new ones and follow-ups alike, can submit; a grant
without the checkpoint gains nothing.
"""
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import ToolAdmissionRefused
from lib.project_dispatch import admission
from lib.project_dispatch.delegation import capture_worker_policy
from tests.mcp_protocol.test_mcp_search_only import (
    OWNER, _mcfg, _names, _task, bridge, coordination, workspace,
)
from tests.project_mod.test_coordinator_continuation import base_runtime, context, runtime
from tests.project_mod.test_coordinator_workers import _worker_task

__all__ = ('base_runtime', 'bridge', 'context', 'coordination', 'runtime', 'workspace')

pytestmark = pytest.mark.unit

# The Coordinator catalogs a grant is captured from: the current one, one frozen
# before 2026-11-18 (the denied submit is absent), and one without project integration.
GRANTS = {
    'current': (('read_files', 'run_command', 'integration_checkpoint', 'integration_submit'), True),
    'frozen before the decision': (('read_files', 'run_command', 'integration_checkpoint'), True),
    'no project integration': (('read_files', 'run_command'), False),
}


@pytest.mark.parametrize('grant', GRANTS)
def test_delegated_worker_submits_its_own_checkpoint_iff_its_grant_checkpoints(
        runtime, monkeypatch, tmp_path, grant):
    from lib.project_dispatch.scope import admit_tool
    import lib.tasks_pkg.handlers  # noqa: F401  registers the handlers
    from lib.tasks_pkg.executor import tool_registry
    catalog, submits = GRANTS[grant]
    capture_worker_policy(runtime.task, runtime.task['config'],
                          [{'function': {'name': name}} for name in catalog], 'model-a',
                          worker_model='model-a')
    worker, row = _worker_task(runtime)
    assert admission.profile_for(row)['tools'] == sorted(
        {*catalog, 'search_tools', 'execute_tools', 'execute_program'})
    checkout = (tmp_path / 'worker-checkout').resolve()
    checkout.mkdir()
    worker['config']['projectPath'] = str(checkout)
    monkeypatch.setattr(admission, 'owned_workspace',
                        lambda task, binding=None: {'workspace_path': str(checkout)})
    monkeypatch.setattr('lib.integration_state_repository.find_workspace',
                        lambda root, task_id, user_id: {
                            'user_id': user_id, 'task_id': task_id, 'project_root': root,
                            'state': 'active', 'workspace_path': str(checkout),
                            'origin': {'executionMode': 'isolated-task'}})
    monkeypatch.setattr('lib.conversations.project_brain.ensure_work_item',
                        lambda task, path, trigger: 'work-1')
    submitted = []
    monkeypatch.setattr('lib.integration_control.execute_integration_tool',
                        lambda name, args, **kwargs: submitted.append((name, args)) or 'Checkpoint saved.')

    def submit():
        # The executor's one admission gate, then the handler it dispatches to.
        admit_tool(worker, 'integration_submit', {'summary': 'Done; verified.'}, project_path=str(checkout))
        return tool_registry.lookup('integration_submit')(
            worker, {}, 'integration_submit', 'tc-submit', {'summary': 'Done; verified.'},
            0, {'toolName': 'integration_submit', 'toolCallId': 'tc-submit', 'query': ''}, {},
            str(checkout), True)

    if submits:
        assert 'Checkpoint saved.' in str(submit())
        [(name, args)] = submitted
        assert (name, args['summary']) == ('integration_submit', 'Done; verified.')
    else:
        with pytest.raises(ToolAdmissionRefused, match='capability ceiling'):
            submit()
        assert submitted == []
    # Nothing else widens: deferred wakes and role tools stay outside every Worker grant.
    for name in ('timer_create', 'delegate_project_work', 'review_project_work'):
        with pytest.raises(ToolAdmissionRefused):
            admit_tool(worker, name, {}, project_path=str(checkout))


def test_coordinator_issues_workers_the_submit_with_the_checkpoint(bridge, coordination):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    path = coordination['coordinator']
    cfg = {'projectPath': path, '_turnOwnerUserId': OWNER, 'userId': OWNER,
           'memoryEnabled': False, 'plugins': []}
    task = _task('coordinator-task', cfg, conv_id='coordinator', content='继续')
    assemble_round_tools(cfg, task, {**_mcfg(path), 'model': 'model-a', 'code_exec_enabled': True})
    granted = task['_coordinatorWorkerPolicy'].tools
    assert {'integration_checkpoint', 'integration_submit'} <= set(granted)


def test_worker_with_a_frozen_grant_finds_the_submit_by_search(bridge, workspace, monkeypatch):
    """The derived submit is executable and searchable; the frozen grant's direct wire stays."""
    from lib.project_dispatch.scope import granted_tools
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    from lib.tools.gateway import search_executable_catalog
    profile = {'mode': 'project', 'tools': sorted(
        {'read_files', 'run_command', 'integration_checkpoint', 'search_tools', 'execute_tools'})}
    monkeypatch.setattr(admission, 'is_dispatch_task', lambda task: task.get('id') == 'granted-worker')
    monkeypatch.setattr(admission, 'granted_coordinator_scope', lambda task: (True, None))
    monkeypatch.setattr(admission, 'validate_task', lambda task, **_: {'assignmentId': 'grant'})
    monkeypatch.setattr(admission, 'repository_for',
                        lambda binding: SimpleNamespace(read=lambda assignment: {}))
    monkeypatch.setattr(admission, 'profile_for', lambda assignment: profile)
    cfg = {'userId': OWNER, 'projectPath': str(workspace), 'plugins': [],
           'memoryEnabled': False, 'schedulerEnabled': False}
    task = _task('granted-worker', cfg, conv_id='granted-worker-conv', content='Finish and submit.')

    tools, available = assemble_round_tools(cfg, task, _mcfg(str(workspace)))

    assert available and 'integration_submit' in granted_tools(profile)
    catalog = task['_executable_tool_catalog']
    assert 'integration_submit' in _names(catalog)
    assert 'integration_submit' not in _names(tools)
    found = search_executable_catalog(
        catalog, 'integration_submit',
        namespace_by_name=task.get('_executableToolNamespaceByName') or {},
        search_text_by_name=task.get('_executableToolSearchTextByName') or {},
        contract_documents_by_name=task.get('_toolContractDocumentsByName') or {},
        visible_names=set(_names(tools)))
    assert [item['name'] for item in found['items']][:1] == ['integration_submit']
