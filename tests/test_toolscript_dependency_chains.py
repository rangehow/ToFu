"""Dependent ToolScript chains through the real local gateway and pipeline.

Only the leaf tool implementation is substituted: catalog search, argument
validation, admission, receipts, private result delivery and program execution
are production code. No provider or synthetic model turn drives these chains.
"""

from __future__ import annotations

import json
import threading

import pytest

from lib.tasks_pkg.handlers import tool_gateway as handler
from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline
from lib.tools.gateway import EXECUTE_TOOLS_NAME


pytestmark = pytest.mark.unit


def _tool(name, properties=None, required=None, description=''):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description or name,
            'parameters': {
                'type': 'object', 'properties': properties or {},
                'required': required or [], 'additionalProperties': False,
            },
        },
    }


@pytest.fixture
def local_gateway(monkeypatch):
    """Keep the actual gateway/dispatch stack; isolate only tool I/O."""
    dispatched = []
    implementations = {}
    task = {
        'id': 'dependency-chain', '_userId': 1, 'model': 'local-test',
        'status': 'running', 'aborted': False,
        'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
        '_attended': False, '_tool_stream_active': True,
        'config': {'tools': {'resultEnvelope': 'v2'}},
        '_executable_tool_catalog': [],
        '_ptc_local': {'tier': 'program', 'eligible': []},
    }
    invocations = []

    def execute_leaf(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        dispatched.append((fn_name, dict(fn_args)))
        status, value = implementations[fn_name](fn_args)
        round_entry['status'] = status
        # Real handlers return content strings. The gateway wraps these in
        # output/error, NOT content; programs must parse receipt.output.
        return tc_id, json.dumps(value), False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', execute_leaf)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', execute_leaf)

    def invoke(source, catalog, tools):
        assert not invocations, 'a dependent chain must use one gateway call'
        invocations.append(source)
        task['_executable_tool_catalog'] = catalog
        implementations.update(tools)
        tc = {
            'id': 'chain-gateway', 'type': 'function',
            'function': {'name': EXECUTE_TOOLS_NAME,
                         'arguments': json.dumps({'program': source})},
        }
        call_id, content, aborted = handler.handle_execute_tools(
            task, tc, EXECUTE_TOOLS_NAME, tc['id'], {'program': source},
            1, {'calls': [], 'llmRound': 1}, {'autoApply': True}, None, False)
        assert call_id == tc['id']
        assert aborted is False
        payload = json.loads(content)
        assert payload['status'] == 'ok', payload
        assert payload['program']['status'] == 'ok', payload
        assert len(task['programRuns']) == 1
        run = task['programRuns'][0]
        assert run['source'] == 'execute_program'
        assert run['status'] == 'completed'
        assert run['result'] == payload['program']['result']
        assert [child['name'] for child in run['childCalls']] == [
            name for name, _ in dispatched]
        assert len(invocations) == 1
        return payload['program']

    return task, dispatched, invoke


def test_upstream_output_supplies_downstream_arguments(local_gateway):
    _, dispatched, invoke = local_gateway
    catalog = [
        _tool('lookup_document'),
        _tool('read_document', {'document_id': {'type': 'string'}},
              ['document_id']),
    ]
    program = invoke('''
        const lookup = tools.call('lookup_document', {});
        const document = JSON.parse(lookup.output);
        const read = tools.call('read_document', {document_id: document.id});
        return {lookup: lookup, read: read, title: JSON.parse(read.output).title};
    ''', catalog, {
        'lookup_document': lambda args: ('done', {'id': 'doc-from-lookup'}),
        'read_document': lambda args: ('done', {'title': 'Resolved title'}),
    })

    assert dispatched == [
        ('lookup_document', {}),
        ('read_document', {'document_id': 'doc-from-lookup'}),
    ]
    result = program['result']
    assert result['title'] == 'Resolved title'
    for key, expected in [('lookup', {'id': 'doc-from-lookup'}),
                          ('read', {'title': 'Resolved title'})]:
        receipt = result[key]
        assert receipt['status'] == 'done'
        assert receipt['source'] == 'execute_program'
        assert 'content' not in receipt
        assert json.loads(receipt['output']) == expected
        assert receipt['output_truncated'] is False
    assert program['stats']['tool_calls'] == 2


def test_failed_upstream_conditionally_skips_side_effect(local_gateway):
    _, dispatched, invoke = local_gateway
    writes = []
    program = invoke('''
        const upstream = tools.call('lookup_document', {});
        if (upstream.status === 'done') {
            return tools.call('publish_document', {
                document_id: JSON.parse(upstream.output).id
            });
        }
        return {skipped: true, failure: upstream};
    ''', [
        _tool('lookup_document'),
        _tool('publish_document', {'document_id': {'type': 'string'}},
              ['document_id']),
    ], {
        'lookup_document': lambda args: ('error', {'reason': 'not found'}),
        'publish_document': lambda args: (writes.append(args) or
                                           ('done', {'published': True})),
    })

    assert dispatched == [('lookup_document', {})]
    assert writes == []
    assert program['result']['skipped'] is True
    failure = program['result']['failure']
    assert failure['status'] == 'error'
    assert 'output' not in failure
    assert json.loads(failure['error']) == {'reason': 'not found'}
    assert program['stats']['tool_calls'] == 1


def test_bounded_result_derived_fanout_then_aggregation(local_gateway):
    _, dispatched, invoke = local_gateway
    program = invoke('''
        const upstream = tools.call('list_documents', {});
        const ids = JSON.parse(upstream.output).ids.slice(0, 2);
        const reads = ids.map(id => tools.call('score_document', {id: id}));
        const total = reads.reduce((sum, row) =>
            sum + JSON.parse(row.output).score, 0);
        return tools.call('save_summary', {ids: ids, total: total});
    ''', [
        _tool('list_documents'),
        _tool('score_document', {'id': {'type': 'string'}}, ['id']),
        _tool('save_summary', {
            'ids': {'type': 'array', 'items': {'type': 'string'}},
            'total': {'type': 'integer'},
        }, ['ids', 'total']),
    ], {
        'list_documents': lambda args: ('done', {'ids': ['a', 'b', 'excluded']}),
        'score_document': lambda args: (
            'done', {'score': {'a': 7, 'b': 11}[args['id']]}),
        'save_summary': lambda args: ('done', {'saved': args}),
    })

    # Serial bounded fan-out intentionally pins dependency order, not thread
    # scheduling. The third upstream candidate never reaches a leaf handler.
    assert dispatched == [
        ('list_documents', {}), ('score_document', {'id': 'a'}),
        ('score_document', {'id': 'b'}),
        ('save_summary', {'ids': ['a', 'b'], 'total': 18}),
    ]
    assert json.loads(program['result']['output']) == {
        'saved': {'ids': ['a', 'b'], 'total': 18}}
    assert program['stats']['tool_calls'] == 4


@pytest.mark.parametrize(('target', 'arguments', 'error_code'), [
    ('publish_document', '{wrong: data.id}', 'missing_required_arguments'),
    ('disabled_remote_action', '{document_id: data.id}', 'tool_not_enabled'),
])
def test_derived_call_rejection_has_no_side_effect(
        local_gateway, target, arguments, error_code):
    task, dispatched, invoke = local_gateway
    side_effects = []
    # Even a wire-visible name does not grant authority outside the immutable
    # executable catalog. The invalid enabled call still needs schema checks.
    task['_tool_schema'] = [_tool('disabled_remote_action')]
    program = invoke('''
        const upstream = tools.call('lookup_document', {});
        const data = JSON.parse(upstream.output);
        return tools.call('%s', %s);
    ''' % (target, arguments), [
        _tool('lookup_document'),
        _tool('publish_document', {'document_id': {'type': 'string'}},
              ['document_id']),
    ], {
        'lookup_document': lambda args: ('done', {'id': 'doc-guarded'}),
        target: lambda args: (side_effects.append(args) or ('done', {})),
    })

    assert dispatched == [('lookup_document', {})]
    assert side_effects == []
    assert program['result']['status'] == 'error'
    assert program['result']['error']['code'] == error_code
    assert program['stats']['tool_calls'] == 2


def test_object_catalog_search_limit_one_then_exact_known_tool(local_gateway):
    task, dispatched, invoke = local_gateway
    visible = _tool('grep_search', {'pattern': {'type': 'string'}}, ['pattern'],
                    'Search repository text')
    hidden = [
        _tool('mcp__docs__search', {'query': {'type': 'string'}}, ['query'],
              'Search document text'),
        _tool('mcp__archive__search', {'query': {'type': 'string'}}, ['query'],
              'Search archived text'),
    ]
    task['_tool_schema'] = [visible]
    task['_executableToolNamespaceByName'] = {
        'grep_search': 'project',
        'mcp__docs__search': 'mcp', 'mcp__archive__search': 'mcp',
    }
    program = invoke('''
        const found = catalog.search({query: 'search', limit: 1});
        const selected = found.items[0];
        const searched = tools.call(selected.name, {query: 'release notes'});
        const local = tools.call('grep_search', {
            pattern: JSON.parse(searched.output).needle
        });
        return {found: found, selected: selected.name, local: local};
    ''', [visible, *hidden], {
        'mcp__docs__search': lambda args: ('done', {'needle': 'release-v2'}),
        'mcp__archive__search': lambda args: ('done', {'needle': 'release-v2'}),
        'grep_search': lambda args: ('done', {'matches': ['CHANGELOG.md:12']}),
    })

    result = program['result']
    found = result['found']
    assert found['status'] == 'ok'
    assert found['query'] == 'search'
    assert len(found['items']) == 1
    assert found['total'] == 2
    assert found['next_cursor']
    assert found['execute_with'] == EXECUTE_TOOLS_NAME
    assert result['selected'] in {tool['function']['name'] for tool in hidden}
    assert found['items'][0]['arguments_schema']['required'] == ['query']
    assert dispatched == [
        (result['selected'], {'query': 'release notes'}),
        ('grep_search', {'pattern': 'release-v2'}),
    ]
    assert json.loads(result['local']['output']) == {
        'matches': ['CHANGELOG.md:12']}
    assert program['stats']['tool_calls'] == 2


def test_exact_incident_query_recovers_visible_tool(local_gateway):
    task, dispatched, invoke = local_gateway
    visible = _tool('grep_search', {'pattern': {'type': 'string'}}, ['pattern'])
    task['_tool_schema'] = [visible]
    program = invoke('''
        const ts = catalog.search({query:"grep_search",limit:1});
        return ts;
    ''', [visible, _tool('mcp__docs__search')], {})
    assert program['result']['query'] == 'grep_search'
    assert program['result']['items'] == []
    assert program['result']['already_visible'] == 'grep_search'
    assert program['stats']['tool_calls'] == 0
    assert dispatched == []


def test_result_derived_parallel_branches_join_in_one_program(local_gateway):
    _, dispatched, invoke = local_gateway
    program = invoke('''
        const upstream = tools.call('list_documents', {});
        if (upstream.status != 'done' || upstream.output_truncated) return upstream;
        const ids = JSON.parse(upstream.output).ids.slice(0, 2);
        const rows = tools.parallel(ids.map(id => ({
            name: 'score_document', arguments: {id: id}
        })));
        if (rows.filter(row => row.status != 'done' || row.output_truncated).length)
            return rows;
        return rows.reduce((sum, row) => sum + JSON.parse(row.output).score, 0);
    ''', [
        _tool('list_documents'),
        _tool('score_document', {'id': {'type': 'string'}}, ['id']),
    ], {
        'list_documents': lambda args: ('done', {'ids': ['a', 'b', 'excluded']}),
        'score_document': lambda args: ('done', {'score': {'a': 7, 'b': 11}[args['id']]}),
    })
    assert dispatched[0] == ('list_documents', {})
    assert sorted(args['id'] for name, args in dispatched[1:]) == ['a', 'b']
    assert program['result'] == 18
    assert program['stats']['tool_calls'] == 3


@pytest.mark.parametrize('value', [0, 1.5, False, True, None, [], [1, 'two'], ''])
def test_json_result_types_settle_without_losing_value(local_gateway, value):
    _, dispatched, invoke = local_gateway
    program = invoke('return ' + json.dumps(value) + ';', [], {})
    assert program['result'] == value
    assert type(program['result']) is type(value)
    assert dispatched == []
