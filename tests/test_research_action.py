"""Least-authority and evidence gates for agentic research actions."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _schema(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': {}},
    }}


def _epoch(names):
    from lib.paper.tools import PaperToolEpochV2

    schemas = tuple(_schema(name) for name in names)
    return PaperToolEpochV2(
        wire_schemas=schemas, executable_schemas=schemas,
        contract_documents_by_name={name: {'schema_hash': name} for name in names},
        discovery_policy_by_name={name: 'eager' for name in names},
        namespace_by_name={name: 'test' for name in names},
        search_text_by_name={name: name for name in names},
        script_safe_by_name={name: False for name in names},
        schema_tokens=1, gateway_schema_tokens=0, schema_budget_tokens=100,
        result_envelope='v2', epoch_hash='full',
    )


def test_action_epoch_exposes_only_native_allowlist_and_exact_binding(monkeypatch):
    import lib.research.action as action

    bound = 'mcp__any-provider__launch'
    unbound = 'mcp__any-provider__delete_all'
    monkeypatch.setattr(action, 'build_paper_full_tool_epoch', lambda **_kwargs: _epoch([
        'web_search', 'fetch_url', 'run_command', 'create_memory', bound, unbound,
    ]))
    workspace = {'capability_bindings': [{
        'capability': 'experiment.execute', 'provider': 'mcp',
        'tool': bound, 'schema_hash': 'one', 'enabled': True,
        'argument_defaults': {}, 'notes': '',
    }]}
    catalog = {'tools': [
        {'name': bound, 'read_only': False, 'schema_hash': 'one'},
        {'name': unbound, 'read_only': False, 'schema_hash': 'two'},
    ]}
    epoch, bindings, problems = action.build_action_tool_epoch(
        action='experiment', user_id=1, workspace=workspace,
        confirm_external_writes=True, catalog=catalog)
    names = {row['function']['name'] for row in epoch.executable_schemas}
    assert names == {'web_search', 'fetch_url', 'run_command', bound}
    assert bindings[0]['tool'] == bound and not problems


def test_write_actions_require_explicit_confirmation():
    from lib.research.action import build_action_tool_epoch

    with pytest.raises(ValueError, match='confirm_external_writes'):
        build_action_tool_epoch(
            action='experiment', user_id=1, workspace={},
            confirm_external_writes=False, catalog={'tools': []})


def test_research_context_preserves_abort_signal(monkeypatch):
    import lib.research.action as action
    import lib.research.persistence as persistence
    from lib.llm_errors import AbortedError

    def abort(*_args, **_kwargs):
        raise AbortedError('owner stopped the task')

    monkeypatch.setattr(persistence, 'load_research_artifacts', abort)
    with pytest.raises(AbortedError, match='owner stopped'):
        action._research_context('Causal compression', 'en', 1)


def test_bound_argument_defaults_are_applied_but_call_arguments_win():
    from lib.research.action import _merge_bound_arguments

    got = _merge_bound_arguments(
        'mcp__provider__run', {'seed': 7}, [{
            'tool': 'mcp__provider__run',
            'argument_defaults': {'queue': 'research', 'seed': 3},
        }])
    assert got == {'queue': 'research', 'seed': 7}


def test_experiment_cannot_pass_without_a_successful_tool_receipt():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Causal compression')
    updated = apply_action_result(
        action='experiment', workspace=workspace,
        payload={'run': {'id': 'r1', 'status': 'passed', 'metric': '99'}},
        receipts=[], task_id='task-1', bindings=[])
    assert updated['runs'][0]['status'] == 'planned'
    assert updated['runs'][0]['task_id'] == 'task-1'


def test_experiment_ignores_search_receipts_and_invented_artifact_refs():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Evidence contracts'),
        payload={'run': {
            'id': 'r1', 'status': 'passed',
            'artifact_refs': ['artifact://invented'],
        }},
        receipts=[{
            'tool': 'web_search', 'status': 'done',
            'artifact_ref': 'evidence:search', 'result_excerpt': 'papers only',
        }],
        task_id='task-1', bindings=[])
    assert updated['runs'][0]['status'] == 'planned'
    assert updated['runs'][0]['artifact_refs'] == []


def test_analysis_downgrades_unobserved_supported_claim_and_visual():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    updated = apply_action_result(
        action='analyze', workspace=empty_workspace('Evidence contracts'),
        payload={
            'claims': [{
                'id': 'c1', 'text': 'Large gain', 'status': 'supported',
                'evidence_refs': ['artifact://invented'],
            }],
            'figures': [{
                'id': 'f1', 'status': 'verified',
                'data_ref': 'data://invented', 'script_ref': 'code://invented',
                'output_ref': 'figure://invented',
            }],
        },
        receipts=[], task_id='task-1', bindings=[])
    assert updated['claims'][0]['status'] == 'draft'
    assert updated['claims'][0]['evidence_refs'] == []
    assert updated['figures'][0]['status'] == 'planned'
    assert updated['figures'][0]['output_ref'] == ''


def test_publish_cannot_claim_success_from_model_text_alone():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Evidence contracts')
    workspace['source_files'] = [{'path': 'main.tex', 'content': 'paper'}]
    binding = {'capability': 'publication.push', 'tool': 'mcp__vendor__push'}
    rejected = apply_action_result(
        action='publish', workspace=workspace,
        payload={'status': 'published', 'project_url': 'https://example.test/p'},
        receipts=[], task_id='task-1', bindings=[binding])
    assert rejected['publication']['status'] == 'failed'

    accepted = apply_action_result(
        action='publish', workspace=workspace,
        payload={'status': 'published', 'project_url': 'https://example.test/p'},
        receipts=[{'tool': binding['tool'], 'status': 'done'}],
        task_id='task-2', bindings=[binding])
    assert accepted['publication']['status'] == 'published'
    assert accepted['publication']['source_digest']
    assert accepted['publication']['provider'] == 'vendor'
    assert accepted['publication']['project_url'] == ''


@pytest.mark.parametrize('status,expected', [
    (None, 'pending'), ('', 'pending'), ('pending', 'pending'),
    ('searching', 'pending'), ('running', 'pending'),
    ('error', 'error'), ('failed', 'error'), ('rejected', 'rejected'),
    ('done', 'done'),
])
def test_receipt_requires_explicit_terminal_success(status, expected):
    from lib.research.action import _tool_receipts, apply_action_result
    from lib.research.workspace import empty_workspace

    row = {'toolName': 'run_command',
           'toolContent': '{"scientific_outcome":"supported"}'}
    if status is not None:
        row['status'] = status
    receipts = _tool_receipts([row])
    assert receipts[0]['status'] == expected
    assert bool(receipts[0]['artifact_ref']) == (expected == 'done')
    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Receipt authority'),
        payload={'run': {'id': 'r1', 'status': 'passed'}},
        receipts=receipts, task_id='t1', bindings=[])
    run = updated['runs'][0]
    assert run['scientific_outcome'] == (
        'supported' if expected == 'done' else 'inconclusive')
    if expected != 'done':
        assert run['execution_status'] != 'completed'


@pytest.mark.parametrize('capability', ['compute.submit', 'compute.status'])
@pytest.mark.parametrize('remote_status,expected', [
    (None, 'planned'), ('success', 'planned'), ('queued', 'queued'),
    ('pending', 'queued'), ('running', 'running'), ('failed', 'failed'),
    ('completed', 'completed'),
])
def test_compute_api_success_is_not_experiment_completion(capability, remote_status, expected):
    import json
    from lib.research.action import _tool_receipts, apply_action_result
    from lib.research.workspace import empty_workspace

    observed = {'ok': True, 'scientific_outcome': 'supported', 'job_id': 'j1'}
    if remote_status is not None:
        observed['job_status'] = remote_status
    tool = 'mcp__compute__job'
    receipts = _tool_receipts([{
        'toolName': tool, 'status': 'done', 'toolContent': json.dumps(observed)}])
    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Remote jobs'),
        payload={'run': {'id': 'r1', 'status': 'passed', 'remote_job_id': 'j1'},
                 'claims': [{'id': 'c1', 'text': 'Works', 'status': 'supported',
                             'evidence_refs': ['r1']}]},
        receipts=receipts, task_id='t1',
        bindings=[{'capability': capability, 'tool': tool}])
    run = updated['runs'][0]
    assert run['execution_status'] == expected
    assert run['remote_job_id'] == 'j1'
    assert run['scientific_outcome'] == (
        'supported' if expected == 'completed' else 'inconclusive')
    assert bool(run['finished_at']) == (expected in {'failed', 'completed'})
    if expected != 'completed':
        assert not run['artifact_refs']
        assert updated['claims'][0]['status'] != 'supported'


def test_compute_status_poll_can_complete_queued_submission():
    from lib.research.action import _tool_receipts, apply_action_result
    from lib.research.workspace import empty_workspace

    receipts = _tool_receipts([
        {'toolName': 'submit', 'status': 'done',
         'toolContent': '{"job":{"status":"queued"}}'},
        {'toolName': 'poll', 'status': 'done',
         'toolContent': '{"job":{"status":"completed"},"scientific_outcome":"refuted"}'},
    ])
    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Remote jobs'),
        payload={'run': {'id': 'r1'}}, receipts=receipts, task_id='t1',
        bindings=[{'capability': 'compute.submit', 'tool': 'submit'},
                  {'capability': 'compute.status', 'tool': 'poll'}])
    run = updated['runs'][0]
    assert run['execution_status'] == 'completed'
    assert run['scientific_outcome'] == 'refuted'
    assert run['artifact_refs'] == [receipts[1]['artifact_ref']]


@pytest.mark.parametrize('abort_during', ['agent', 'apply'])
def test_abort_after_agent_never_saves_workspace(monkeypatch, abort_during):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    import lib.research.workspace as workspace

    abort = threading.Event()
    task = {'task_id': 'abort-test', 'action': 'experiment', 'user_id': 1,
            'direction': 'Stop safely', 'lang': 'en', 'expected_revision': 0,
            'confirm_external_writes': True, 'abort_event': abort}
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    monkeypatch.setattr(workspace, 'load_workspace', lambda *a, **k: {'revision': 0})
    save = Mock()
    monkeypatch.setattr(workspace, 'save_workspace', save)
    monkeypatch.setattr(action, 'build_action_tool_epoch',
                        lambda **k: (_epoch([]), [], []))

    def agent(*args):
        if abort_during == 'agent':
            abort.set()
        return {}, []

    def apply(**kwargs):
        abort.set()
        return {}

    apply_mock = Mock(side_effect=apply)
    monkeypatch.setattr(action, '_run_agent', agent)
    monkeypatch.setattr(action, 'apply_action_result', apply_mock)
    action.run_research_action(task)
    save.assert_not_called()
    owner.abort.assert_called_once_with('abort-test')
    assert owner.finish.call_args.kwargs['terminal_event_fields']['type'] == 'aborted'
    assert apply_mock.call_count == (0 if abort_during == 'agent' else 1)
