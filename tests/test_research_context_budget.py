"""Research action input admission is separate from cumulative usage."""
import pytest

pytestmark = pytest.mark.unit


def test_projection_preserves_exact_sources_but_removes_receipt_prose():
    from lib.research.context import project_workspace
    workspace = {'source_files': [{'path': 'main.tex', 'content': 'exact source'}],
        'runs': [{'tool_receipts': [{'result_excerpt': 'large prose', 'artifact_ref': 'a'}]}]}
    projected = project_workspace(workspace, 'compile')
    assert projected['source_files'] == workspace['source_files']
    assert 'result_excerpt' not in projected['runs'][0]['tool_receipts'][0]
    assert workspace['runs'][0]['tool_receipts'][0]['result_excerpt'] == 'large prose'
    assert 'source_files' not in project_workspace(workspace, 'experiment')


def test_large_context_fails_before_dispatch_and_counts_tools(monkeypatch):
    import lib.research.context as context
    monkeypatch.setattr(context, 'action_window', lambda _: 8192)
    assert context.admit_messages([{'role': 'user', 'content': 'brief'}], model='test') < 100
    with pytest.raises(ValueError, match='exact sources were not truncated'):
        context.admit_messages([{'role': 'user', 'content': 'brief'}], model='test',
            tools=[{'description': 'unique content ' * 30000}])


def test_actual_persisted_evidence_keys_reach_actions(monkeypatch):
    from lib.research.action import _research_context
    import lib.research.persistence as persistence
    monkeypatch.setattr(persistence, 'load_research_artifacts', lambda *a, **k: {
        'survey_md': 'Actual survey', 'open_gaps': {'open_gaps': [{'id': 'g'}]},
        'research_cycle': {'status': 'unresolved', 'issues': [{'change': 'measure'}]},
    })
    context = _research_context('topic', 'en', 1)
    assert context['survey_markdown'] == 'Actual survey'
    assert context['gap_map'] == [{'id': 'g'}]
    assert context['research_cycle']['status'] == 'unresolved'
