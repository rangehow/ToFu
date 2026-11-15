"""A partial result names only tools its caller can call.

2026-11-11 17:52: a Coordinator's get_conversation result came back partial with
"continue with read_tool_artifact", but read_tool_artifact was not in that
round's executable catalog: the follow-up call was rejected as an unknown tool
and the requested data never reached the model. The result budget now sees the
tool set the caller has. Without the reader, a result that is safe to request
again names its own tool and arguments; any other result keeps its bounded
excerpt and says its remainder cannot be read here.
"""
from __future__ import annotations

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import split_tool_result_delivery

pytestmark = pytest.mark.unit

_READERS = ('read_tool_artifact', 'search_tool_artifact')
_REF = 'tool-result:' + 'a' * 64


def _names_a_reader(text):
    return any(reader in text for reader in _READERS)


@pytest.fixture
def small_budget(monkeypatch):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 1000)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 1000)
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', 1500)
    monkeypatch.setattr(_budget, '_store_tool_result_artifact',
                        lambda content, *, user_id, observed_at_ms: _REF)


_LARGE = 'evidence line of a long worker transcript\n' * 400


@pytest.mark.parametrize('artifact_reader', [True, False])
def test_single_result_names_the_reader_only_when_callable(small_budget, artifact_reader):
    view = _budget.budget_tool_result_v2(
        'run_command', _LARGE, user_id=0, recovery_policy='artifact',
        artifact_reader=artifact_reader)
    text = split_tool_result_delivery(view).model_text
    assert '"status":"partial"' in text
    assert _names_a_reader(text) is artifact_reader
    if not artifact_reader:
        assert 'cannot read it' in text
        assert 'evidence line' in text


@pytest.mark.parametrize('artifact_reader', [True, False])
def test_round_budget_names_the_reader_only_when_callable(small_budget, artifact_reader):
    results = {f'call-{index}': (_LARGE, 'run_command', f'call-{index}')
               for index in range(2)}
    declared = {key: {'policy': 'artifact', 'arguments': {},
                      'artifactReader': artifact_reader} for key in results}
    reduced = _budget.enforce_round_aggregate_budget_v2(
        dict(results), user_id=0, recovery_by_call_id=declared)
    for content, _name, _call in reduced.values():
        text = split_tool_result_delivery(content).model_text
        assert _names_a_reader(text) is artifact_reader


def _schema(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': {}}}}


def _task(catalog, *, sent=None, effect='read_only'):
    task = {'id': 'reader-task', 'convId': 'cv-reader', '_userId': 1,
            'model': 'test-model', 'config': {},
            '_executable_tool_catalog': [_schema(name) for name in catalog],
            '_toolContractDocumentsByName': {
                name: {'contractVersion': 'tofu.tool-contract/v2', 'name': name,
                       'resultRecovery': 'artifact', 'effectRecovery': effect}
                for name in catalog}}
    if sent is not None:
        task['events'] = [{'type': 'tool_wire_projection', 'toolNames': list(sent)}]
    return task


@pytest.mark.parametrize(('catalog', 'sent', 'callable_'), [
    (['get_conversation', 'read_tool_artifact'], None, True),
    (['get_conversation', 'read_tool_artifact'], ['get_conversation', 'read_tool_artifact'], True),
    # Executable but off the wire: callable through execute_tools only.
    (['get_conversation', 'read_tool_artifact', 'execute_tools'],
     ['get_conversation', 'execute_tools'], True),
    (['get_conversation', 'read_tool_artifact'], ['get_conversation'], False),
    # The 17:52 case: not even in the executable catalog.
    (['get_conversation'], None, False),
    (['get_conversation', 'execute_tools'], ['get_conversation', 'execute_tools'], False),
])
def test_reader_is_callable_only_from_the_callers_tool_set(catalog, sent, callable_):
    from lib.tasks_pkg.tool_dispatch._labels import artifact_reader_callable
    assert artifact_reader_callable(_task(catalog, sent=sent)) is callable_


@pytest.mark.parametrize('source_retry_safe', [True, False])
def test_without_reader_a_safe_source_is_named_and_the_artifact_kept(
        small_budget, source_retry_safe):
    view = _budget.budget_tool_result_v2(
        'get_conversation', _LARGE, user_id=0, recovery_policy='artifact',
        tool_arguments={'conversation_id': 'worker-a'}, artifact_reader=False,
        source_retry_safe=source_retry_safe)
    delivery = split_tool_result_delivery(view)
    assert not _names_a_reader(delivery.model_text)
    assert delivery.evidence['artifactRef'] == _REF
    assert ('"recovery":{"tool":"get_conversation"' in delivery.model_text) \
        is source_retry_safe
    assert ('cannot read it' in delivery.model_text) is not source_retry_safe


@pytest.mark.parametrize(('effect', 'safe'), [
    ('read_only', True), ('idempotent', True), ('reconcile', False)])
def test_only_effect_free_tools_are_named_for_another_request(effect, safe):
    from lib.tasks_pkg.tool_dispatch._pipeline import _source_retry_safe
    assert _source_retry_safe(
        _task(['get_conversation'], effect=effect), 'get_conversation') is safe


def test_coordinator_partial_result_without_reader_names_its_own_tool(
        small_budget, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    import lib.tool_results.tool_result_artifacts as artifacts
    monkeypatch.setattr(pipeline, 'append_event', lambda *a, **kw: None)
    monkeypatch.setattr(artifacts, 'register_artifact_provenance', lambda *a, **kw: None)
    monkeypatch.setattr(artifacts, 'mint_artifact_alias', lambda *a, **kw: None)
    task = _task(['get_conversation', 'get_project_assignment'])
    row = {'toolCallId': 'call-1', 'toolName': 'get_conversation', 'roundNum': 1,
           'llmRound': 1, 'status': 'done', 'toolArgs': '{}'}
    task['toolRounds'] = [row]
    arguments = {'conversation_id': 'worker-a', 'limit': 1}
    content = pipeline._settle_tool_result(
        task, 'get_conversation', 'call-1', arguments, 1, row, _LARGE,
        idempotent_tools=frozenset(), cache={}, tid='reader-task', round_num=1,
        settled_results={})
    assert '"status":"partial"' in content
    assert not _names_a_reader(content)
    assert '"recovery":{"tool":"get_conversation"' in content
    assert 'evidence line' in content
    assert row['toolResultEvidence']['artifactRef'] == _REF


@pytest.mark.parametrize('catalog', [['get_conversation', 'read_tool_artifact'],
                                     ['get_conversation']])
def test_compacted_cold_result_names_the_reader_only_when_callable(catalog):
    from types import SimpleNamespace
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import (
        _canonical_recovery_note,
    )
    task = _task(catalog)
    task['toolRounds'] = [{
        'toolCallId': 'call-1', 'toolContent': 'old partial view',
        'toolResultEvidence': {'resultContractVersion': 'tofu.tool-result/v2',
                               'artifactRef': 'tool-result:' + 'b' * 64}}]
    note = _canonical_recovery_note(
        SimpleNamespace(scratch={}, task=task),
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'old partial view'})
    if 'read_tool_artifact' in catalog:
        assert 'read_tool_artifact' in note
    else:
        assert note is None
