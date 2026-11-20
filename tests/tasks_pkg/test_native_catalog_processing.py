"""Native catalog documentation is validated once without changing wire policy."""
import copy
from unittest.mock import patch

import pytest

from lib.tasks_pkg.native_gateway import compile_native_tool_catalog
from lib.tools import contracts
from lib.tools.gateway import sanitize_wire_tools

pytestmark = pytest.mark.unit


def definition(declaration):
    return {'type': 'function', 'function': {
        'name': 'read_files', 'description': 'Read fixture JSON',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}},
    }, 'decoded_result_schema': declaration}


@pytest.mark.parametrize('declaration', [
    {}, {'type': 'object'}, {'title': '中文 😀'},
    {'properties': {'value': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}},
    {'enum': [None, True, 1, 2.5, 'x']}, {'items': ({'type': 'string'},)},
    {2: 'two', 10: 'ten'}, {'nested': {2: 'two', 10: 'ten'}},
    {'$defs': {'value': {'type': 'string'}}, '$ref': '#/$defs/value'},
    {'description': 'quotes " slash \\ newline\n'},
    {'allOf': [{'type': 'object'}, {'required': ['path']}]},
    {'properties': {str(i): {'type': 'string'} for i in range(32)}},
])
def test_native_catalog_serializes_each_declaration_once(declaration):
    tools = [definition(declaration)]
    original = copy.deepcopy(tools)
    expected = sanitize_wire_tools(tools)[0]['function']
    with patch.object(contracts, 'serialize_decoded_result_schema',
                      wraps=contracts.serialize_decoded_result_schema) as serialize:
        result = compile_native_tool_catalog({}, tools, permissions={'read_files': 'read'})
    assert result == {'read_files': {
        'name': 'read_files', 'description': expected['description'],
        'contractVersion': 'tofu.tool-contract/v2', 'permission': 'read',
        'arguments_schema': expected['parameters'],
    }}
    assert serialize.call_count == 1
    assert tools == original
    result['read_files']['arguments_schema']['properties'].clear()
    assert tools == original


@pytest.mark.parametrize('declaration', [None, [], {'value': float('nan')},
                                            {'value': 'x' * 32768}])
def test_native_catalog_still_refuses_invalid_metadata_even_when_not_permitted(declaration):
    with pytest.raises(ValueError):
        compile_native_tool_catalog({}, [definition(declaration)], permissions={})


def test_native_catalog_still_uses_wire_schema_validation():
    tools = [definition({'type': 'object'})]
    tools[0]['function']['parameters'] = {'type': 'string'}
    with pytest.raises(ValueError, match='projection was refused'):
        compile_native_tool_catalog({}, tools, permissions={'read_files': 'read'})


@pytest.mark.parametrize('defect', ['duplicate', 'function_control', 'parameters', 'permission'])
def test_metadata_refusal_precedes_later_catalog_errors(defect):
    tools = [definition(None)]
    permissions = {'read_files': 'read'}
    if defect == 'duplicate':
        tools.insert(0, definition({}))
    elif defect == 'function_control':
        tools[0]['function']['strict'] = True
    elif defect == 'parameters':
        tools[0]['function']['parameters'] = None
    else:
        permissions['read_files'] = 'foreign'
    with pytest.raises(ValueError, match='^decoded_result_schema must be an object$'):
        compile_native_tool_catalog({}, tools, permissions=permissions)
