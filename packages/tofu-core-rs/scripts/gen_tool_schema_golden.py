"""Regenerate packages/tofu-core-rs/tests/fixtures/tool_schema_golden.json from Python.

``PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/gen_tool_schema_golden.py [--check]``

The corpus is every built-in tool schema Tofu assembles (the static schema
owners plus a fully enabled registry build, as
``tests/tools/test_tool_schema_provider_compatibility.py`` collects them) and
hand-written schemas that exercise the provider projection: root and nested
``allOf``/``oneOf``/``anyOf``, ``type`` beside ``anyOf``, type arrays,
``const``, ``$ref``/``$defs``/legacy ``definitions`` (including recursion and
collisions), Python equality (``1 == 1.0 == True``), and shapes that fail
validation (the tool is dropped) or raise.

Two oracles are recorded:

* ``preflight``: ``lib.tools.gateway.sanitize_wire_tools([tool], model=...)``
  for each tool and model: the projected tools plus the isolation reports
  (tool name and reason code; the free-text detail depends on frozenset
  iteration order), or the exception class and message.
* ``requests``: whole catalogs through ``build_body`` + ``prepare_request`` on
  the FRIDAY Anthropic and OpenAI faces (sha256 and length of the sealed
  payload, plus URL and headers).

``--check`` exits 1 when the committed fixture differs from a regeneration.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_wire_golden import _install_profiles  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'tool_schema_golden.json'
PROJECT_PATH = '/golden/project'
MODELS = ['kimi-k3', 'kimi-k2.7-code', 'kimi-k2.6', 'claude-opus-4.8', 'gpt-5.5', 'glm-5.3']
FRIDAY_ANTHROPIC = 'https://api.openai.com/v1/anthropic'
FRIDAY_CHAT = 'https://api.openai.com/v1'
MESSAGES = [
    {'role': 'system', 'content': 'Schema golden.'},
    {'role': 'user', 'content': 'Use a tool.'},
]


def _tool(name, parameters, description='Hand-written schema case.'):
    function = {'name': name, 'description': description}
    if parameters is not _MISSING:
        function['parameters'] = parameters
    return {'type': 'function', 'function': function}


_MISSING = object()
S = {'type': 'string'}
I = {'type': 'integer'}  # noqa: E741

# name -> parameters. Every name is a valid provider tool name.
HAND = {
    # ── root object projection (every model) ──
    'root_plain': {'type': 'object', 'properties': {'a': S}, 'required': ['a']},
    'root_untyped': {'properties': {'a': S}},
    'root_type_array': {'type': ['object', 'null'], 'properties': {'a': S}},
    'root_type_string': {'type': 'string'},
    'root_type_empty_array': {'type': [], 'properties': {}},
    'root_type_unknown': {'type': ['object', 'thing']},
    'root_oneof_required': {'type': 'object', 'additionalProperties': False,
                            'properties': {'a': S, 'b': I},
                            'oneOf': [{'required': ['a']}, {'required': ['b']}]},
    'root_anyof_branch_props': {'type': 'object', 'description': 'union',
                                'anyOf': [{'properties': {'a': S, 'c': S}, 'required': ['a', 'c']},
                                          {'properties': {'a': I, 'c': S}, 'required': ['c']},
                                          {'type': 'string'}]},
    'root_anyof_partial_presence': {'anyOf': [{'properties': {'x': S}}, {'properties': {'y': S}}]},
    'root_anyof_bool_branches': {'anyOf': [False, True, {'properties': {'k': S}, 'required': ['k']}]},
    'root_anyof_no_object_branch': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
    'root_anyof_all_false': {'anyOf': [False]},
    'root_anyof_empty': {'anyOf': []},
    'root_anyof_not_list': {'anyOf': {'type': 'object'}},
    'root_anyof_non_object_branch': {'anyOf': [{'type': 'object'}, 3]},
    'root_anyof_closed_branches': {'anyOf': [{'properties': {'a': S}, 'additionalProperties': False},
                                             {'properties': {'b': S}, 'additionalProperties': False}]},
    'root_oneof_branch_props_bad': {'oneOf': [{'properties': ['a']}]},
    'root_props_not_object': {'properties': [1], 'anyOf': [{'required': []}]},
    'root_allof_merge': {'type': 'object', 'properties': {'a': S},
                         'allOf': [{'properties': {'b': I}, 'required': ['b']},
                                   {'properties': {'b': S, 'c': S}, 'required': ['c', 'b']}],
                         'required': ['a']},
    'root_allof_false': {'type': 'object', 'properties': {'a': S}, 'allOf': [{'required': ['a']}, False]},
    'root_allof_true_only': {'allOf': [True]},
    'root_allof_string_branch': {'allOf': [{'type': 'string'}]},
    'root_multiple_combinators': {'type': ['object'], 'properties': {'a': S, 'b': S},
                                  'anyOf': [{'required': ['a']}], 'allOf': [{'required': ['b']}],
                                  'oneOf': [{'required': ['a']}], 'title': 'multi'},
    'root_py_equality': {'anyOf': [{'properties': {'n': {'default': 1}}}, {'properties': {'n': {'default': 1.0}}},
                                   {'properties': {'n': {'default': True}}}],
                         'required': [], 'description': 'py =='},
    'root_required_undeclared': {'type': 'object', 'properties': {'a': S}, 'required': ['a', 'ghost']},
    'root_required_not_strings': {'type': 'object', 'properties': {'a': S}, 'required': [1]},
    'nested_required_undeclared': {'type': 'object', 'properties': {
        'o': {'type': 'object', 'properties': {}, 'required': ['z']}}},
    'branch_inherits_parent_properties': {'type': 'object', 'properties': {'p': {
        'type': 'object', 'properties': {'a': S, 'b': S},
        'anyOf': [{'required': ['a']}, {'required': ['b']}]}}},
    # ── nested applicators (Kimi MFJS dialect) ──
    'nested_allof': {'type': 'object', 'properties': {
        'n': {'allOf': [{'type': ['integer', 'string'], 'minimum': 1}, {'type': 'number', 'minimum': 1.0},
                        {'description': 'merged'}]},
        'm': {'type': 'object', 'allOf': [{'properties': {'a': S}, 'required': ['a'],
                                           'additionalProperties': True},
                                          {'properties': {'a': I, 'b': I}, 'required': ['b', 'a'],
                                           'additionalProperties': False}]},
        'f': {'type': 'string', 'allOf': [{'minLength': 1}, False, {'maxLength': 3}]},
        't': {'allOf': [True, {'type': 'boolean'}]},
    }},
    'nested_allof_incompatible': {'type': 'object', 'properties': {
        'x': {'allOf': [{'type': 'string'}, {'type': 'integer'}]}}},
    'nested_allof_bad_branch': {'type': 'object', 'properties': {'x': {'allOf': [3]}}},
    'nested_allof_empty': {'type': 'object', 'properties': {'x': {'allOf': []}}},
    'nested_allof_with_anyof': {'type': 'object', 'properties': {
        'x': {'allOf': [{'type': 'string'}], 'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'y': {'allOf': [{'type': 'string'}], 'oneOf': [{'type': 'string'}, {'type': 'integer'}]}}},
    'nested_allof_bad_type': {'type': 'object', 'properties': {
        'x': {'type': 'integer', 'allOf': [{'type': 'nope'}]}}},
    'nested_oneof': {'type': 'object', 'properties': {
        'o': {'oneOf': [{'type': 'string'}, {'type': 'integer'}], 'description': 'one'},
        'both': {'oneOf': [{'type': 'string'}], 'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
        'arr': {'type': 'array', 'items': {'oneOf': [S, {'type': 'object', 'properties': {'k': S}}]}}}},
    'nested_oneof_empty': {'type': 'object', 'properties': {'o': {'oneOf': []}}},
    'nested_const': {'type': 'object', 'properties': {
        's': {'const': 'fixed'}, 'i': {'const': 3, 'type': 'integer'}, 'f': {'const': 2.5},
        'b': {'const': True}, 'z': {'const': None}, 'o': {'const': {'k': 1}}, 'a': {'const': [1]},
        'e': {'const': 'x', 'enum': ['y']}, 'tb': {'const': False, 'type': 'boolean'}}},
    'nested_type_arrays': {'type': 'object', 'properties': {
        'sn': {'type': ['string', 'null'], 'description': 'nullable'},
        'dup': {'type': ['integer', 'integer']},
        'one': {'type': ['boolean']},
        'with_anyof': {'type': ['string', 'integer'], 'anyOf': [{'minLength': 1}, {'type': 'number'}]}}},
    'nested_type_array_unknown': {'type': 'object', 'properties': {'x': {'type': ['string', 'date']}}},
    'nested_type_array_empty': {'type': 'object', 'properties': {'x': {'type': []}}},
    'type_anyof_distribution': {'type': 'object', 'properties': {
        'x': {'type': 'object', 'anyOf': [{'properties': {'a': S}}, True, False, {'type': 'string'},
                                          {'type': ['object', 'null'], 'required': []}]},
        'num': {'type': 'number', 'anyOf': [{'type': 'integer'}, {'type': 'number'}, {'type': ['integer', 'string']}]},
        'impossible': {'type': 'string', 'anyOf': [{'type': 'integer'}], 'items': {'type': ['x']}},
    }},
    'type_anyof_bad_branch': {'type': 'object', 'properties': {'x': {'type': 'string', 'anyOf': [7]}}},
    'type_anyof_empty': {'type': 'object', 'properties': {'x': {'type': 'string', 'anyOf': []}}},
    'type_anyof_bad_parent': {'type': 'object', 'properties': {'x': {'type': 'strin', 'anyOf': [S]}}},
    'anyof_fold_parent_object': {'type': 'object', 'properties': {
        'x': {'properties': {'shared': S, 'over': S}, 'required': ['shared'], 'additionalProperties': False,
              'anyOf': [{'type': 'object', 'properties': {'over': I, 'own': S}, 'required': ['own']},
                        {'type': 'string'}, {'required': ['over']}]},
        'y': {'additionalProperties': {'type': 'string'},
              'anyOf': [{'properties': {'a': S}}, {'additionalProperties': True}]},
        'z': {'properties': {}, 'required': [], 'anyOf': [{'type': 'null'}, True]}}},
    'nested_definitions_and_refs': {
        'type': 'object',
        'properties': {'node': {'$ref': '#/$defs/node'}, 'legacy': {'$ref': '#/definitions/leaf'},
                       'self': {'$ref': '#'}, 'missing': {'$ref': '#/$defs/absent'},
                       'external': {'$ref': 'https://example.com/s.json'},
                       'deep': {'$ref': '#/$defs/node/properties/children'},
                       'clash': {'$ref': '#/definitions/node'}},
        '$defs': {'node': {'type': 'object', 'properties': {
            'children': {'type': 'array', 'items': {'$ref': '#/$defs/node'}},
            'kind': {'oneOf': [{'const': 'a'}, {'const': 'b'}]}}}},
        'definitions': {'leaf': {'type': ['string', 'null']}, 'node': {'type': 'integer'}},
    },
    'definitions_only': {'definitions': {'a/b~c': {'type': 'string', 'format': 'x'}},
                         'type': 'object', 'properties': {'v': {'$ref': '#/definitions/a~1b~0c'}}},
    'definitions_first_then_defs': {'type': 'object', 'definitions': {'x': S}, 'properties': {'p': {'$ref': '#/$defs/x'}},
                                    '$defs': {'x': S, 'y': {'$ref': '#/$defs/x'}}},
    'definitions_collision_chain': {'type': 'object', '$defs': {'n': S, 'legacy_n': I},
                                    'definitions': {'n': {'type': 'boolean'}},
                                    'properties': {'p': {'$ref': '#/definitions/n'}}},
    'definitions_same_value': {'type': 'object', '$defs': {'n': S}, 'definitions': {'n': {'type': 'string'}}},
    'defs_not_object': {'type': 'object', '$defs': [1], 'properties': {'p': {'$ref': '#/$defs/x'}}},
    'definitions_not_object': {'type': 'object', 'definitions': 5, 'properties': {}},
    'defs_bad_child': {'type': 'object', '$defs': {'bad': 4}},
    'nested_defs_dropped': {'type': 'object', 'properties': {'p': {'type': 'object', '$defs': {'q': S},
                                                                   'definitions': {'r': S}}}},
    'ref_non_string': {'type': 'object', 'properties': {'p': {'$ref': 5, 'type': 'string'}}},
    'recursive_ref_with_applicators': {'type': 'object', '$defs': {'tree': {
        'type': ['object', 'null'], 'properties': {'kids': {'type': 'array', 'items': {'$ref': '#/$defs/tree'}},
                                                   'tag': {'allOf': [{'const': 't'}]}}}},
        'properties': {'root': {'$ref': '#/$defs/tree'}}, 'required': ['root']},
    'mfjs_keyword_strip': {'type': 'object', 'title': 'T', 'required': ['mode', 'extra'], 'properties': {
        'mode': {'type': 'string', 'enum': ['a', 'b'], 'examples': ['a'], 'minLength': 1},
        'mixed': {'enum': ['x', 1]}, 'bools': {'enum': [True, False]}, 'empty_enum': {'enum': []},
        'floats': {'enum': [1.5, 2.5]}, 'ints': {'enum': [1, 2]}, 'mixed_num': {'enum': [1, 2.5]},
        'pair': {'type': 'array', 'prefixItems': [S], 'items': I},
        'tuple': {'type': 'array', 'items': [S, I]},
        'items_true': {'type': 'array', 'items': True},
        'map': {'type': 'object', 'patternProperties': {'^x': S}, 'additionalProperties': False},
        'map_open': {'type': 'object', 'patternProperties': {'^x': S}, 'additionalProperties': True},
        'addl_num': {'type': 'object', 'additionalProperties': 3},
        'flag': True, 'never': False,
        'desc_num': {'type': 'string', 'description': 7},
        'choice': {'anyOf': [S, {'type': 'integer', 'format': 'int32'}]},
        'choice_open': {'anyOf': [S, {}]}, 'choice_true': {'anyOf': [True, S]},
        'choice_false': {'anyOf': [False, S]}, 'choice_only_false': {'anyOf': [False]},
        'default_obj': {'type': 'object', 'default': {'k': [1, 2.0]}},
        'not_cond': {'not': {'type': 'string'}, 'if': {'const': 1}, 'then': {'const': 2}, 'else': {'type': ['null']}},
        'pp_dependent': {'type': 'object', 'dependentSchemas': {'a': {'oneOf': [S]}}, 'propertyNames': {'maxLength': 3}},
    }},
    'mfjs_nested_bad_not': {'type': 'object', 'properties': {'p': {'not': {'type': []}}}},
    'mfjs_nested_bad_contains': {'type': 'object', 'properties': {'p': {'contains': {'allOf': [2]}}}},
    'mfjs_property_not_schema': {'type': 'object', 'properties': {'p': 3}},
    'mfjs_properties_not_object': {'type': 'object', 'properties': {'p': {'type': 'object', 'properties': [1]}}},
    'mfjs_anyof_branch_not_schema': {'type': 'object', 'properties': {'p': {'anyOf': [S, 'x']}}},
    'mfjs_anyof_not_list': {'type': 'object', 'properties': {'p': {'anyOf': {'type': 'string'}}}},
    'mfjs_bad_type_name': {'type': 'object', 'properties': {'p': {'type': 'str'}}},
    'mfjs_type_number_value': {'type': 'object', 'properties': {'p': {'type': 5}}},
    'mfjs_type_null_value': {'type': 'object', 'properties': {'p': {'type': None, 'description': None}}},
    'mfjs_required_not_list': {'type': 'object', 'properties': {'p': {'type': 'object', 'required': 'p'}}},
    'mfjs_bad_additional': {'type': 'object', 'additionalProperties': {'type': 'whatever'}},
    'mfjs_bad_items': {'type': 'object', 'properties': {'p': {'type': 'array', 'items': {'type': 'x'}}}},
    'mfjs_anyof_conflict_true_branch': {'type': 'object', 'properties': {'p': {
        'properties': {'a': S}, 'anyOf': [True, {'properties': {'b': S}}]}}},
    'mfjs_anyof_conflict_bool_branch': {'type': 'object', 'properties': {'p': {
        'properties': {'a': S}, 'anyOf': [False, {'properties': {'b': S}}]}}},
    'kimi_required_string_root': {'type': 'object', 'properties': {'a': S, 'b': S}, 'required': 'ab',
                                  'oneOf': [{'required': ['a']}]},
    'kimi_required_object_branch': {'properties': {'a': S}, 'anyOf': [{'required': {'a': 1}}]},
    'kimi_required_bool_branch': {'properties': {'a': S}, 'anyOf': [{'required': True}]},
    'kimi_required_unhashable': {'properties': {'a': S}, 'anyOf': [{'required': [['a']]}]},
    'kimi_required_unhashable_parent': {'properties': {'a': S}, 'required': [{'a': 1}], 'anyOf': [{'required': []}]},
    'kimi_required_numbers': {'type': 'object', 'properties': {'a': S}, 'anyOf': [{'required': [1, True, 1.0]}]},
    'kimi_allof_required_unhashable': {'type': 'object', 'properties': {
        'x': {'allOf': [{'required': ['a']}, {'required': [['b']]}]}}},
    'kimi_fold_required_string': {'type': 'object', 'properties': {'x': {
        'required': ['a'], 'properties': {'a': S, 'b': S}, 'anyOf': [{'required': 'b'}]}}},
}
LEGACY_PARAMETERS = {
    'parameters_missing': _MISSING,
    'parameters_null': None,
    'parameters_true': True,
    'parameters_false': False,
    'parameters_string': 'object',
    'parameters_list': [],
}
# Tool envelopes beside the parameter schema.
ENVELOPES = {
    'untyped_function': {'function': {'name': 'untyped_function', 'parameters': {'type': 'object'}}},
    'wrong_type_function': {'type': 'tool', 'function': {'name': 'wrong_type_function',
                                                         'parameters': {'type': 'object'}}},
    'nameless_function': {'type': 'function', 'name': 'nameless_function', 'function': {'parameters': {}}},
    'empty_name': {'type': 'function', 'function': {'name': '', 'parameters': {}}},
    'decoded_result': {'type': 'function', 'decoded_result_schema': {'type': 'object', 'description': 'r\u00e9'},
                       'function': {'name': 'decoded_result', 'description': 'd',
                                    'parameters': {'oneOf': [{'properties': {'a': S}}]}}},
}
# Python exceptions abort the whole request, so they stay out of the request catalogs.
RAISING = {'kimi_required_bool_branch', 'kimi_required_unhashable', 'kimi_required_unhashable_parent',
           'kimi_allof_required_unhashable'}


def builtin_tools():
    """Every built-in schema: static owners plus a fully enabled registry build."""
    import lib.tasks_pkg.handlers  # noqa: F401 — handler-complete registry
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import project_dispatch_file_tools
    from lib.tool_input_repair._schema import _build_schema_index
    from lib.tools.registry import ToolContext, all_specs

    tools = {name: {'type': 'function', 'function': {'name': name, 'parameters': parameters}}
             for name, parameters in _build_schema_index().items()}
    ctx = ToolContext(
        cfg={}, task_id='schema-golden', project_path=PROJECT_PATH, project_enabled=True,
        search_mode='multi', search_enabled=True, fetch_enabled=True, code_exec_enabled=True,
        browser_enabled=True, desktop_enabled=True, image_gen_enabled=True,
        human_guidance_enabled=True, scheduler_enabled=True, messages=[], enabled_plugins=None)
    for phase in ('base', 'capability'):
        for spec in all_specs():
            if spec.phase != phase or (spec.source != 'builtin' and not spec.host_owned):
                continue
            for tool in spec.build(ctx) or []:
                tools[tool['function']['name']] = tool
                ctx.current_count += 1
        ctx.has_base_tools = ctx.current_count > 0
    dispatch = {'mode': 'files', 'tools': ['read_files', 'write_file', 'edit_file'],
                'readPaths': ['src'], 'writePaths': ['src']}
    out = [copy.deepcopy(tools[name]) for name in sorted(tools)]
    out.extend(copy.deepcopy(project_dispatch_file_tools(dispatch)))
    return json.loads(json.dumps(out))


def hand_tools():
    tools = [_tool(name, parameters) for name, parameters in HAND.items()]
    tools.extend(_tool(name, parameters) for name, parameters in LEGACY_PARAMETERS.items())
    tools.extend(ENVELOPES.values())
    return json.loads(json.dumps(tools))


def _preflight(tool, model):
    from lib.tools.gateway import sanitize_wire_tools
    isolated = []
    try:
        out = sanitize_wire_tools([copy.deepcopy(tool)], model=model, on_tool_isolated=isolated.append)
    except Exception as exc:  # noqa: BLE001 — the oracle records any exception
        return {'error': {'class': type(exc).__name__, 'message': str(exc)}}
    return {'tools': out, 'isolated': [[row['toolName'], row['reasonCode']] for row in isolated]}


def _request(name, model, base_url, protocol, tools):
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    body = build_body(model, copy.deepcopy(MESSAGES), max_tokens=4096, tools=copy.deepcopy(tools),
                      provider_id='golden', precomputed_input_tokens=100, context_window=200000)
    case = {'name': name, 'protocol': protocol, 'model': model, 'base_url': base_url}
    try:
        plan = prepare_request(body, api_key='sk-golden', base_url=base_url, api_protocol=protocol)
    except Exception as exc:  # noqa: BLE001
        case['error'] = {'class': type(exc).__name__, 'message': str(exc)}
        return case
    payload = plan.body.payload
    case.update({
        'url': plan.url, 'headers': [[k, v] for k, v in plan.hdrs.items() if k != 'M-TraceId'],
        'payload_sha256': hashlib.sha256(payload).hexdigest(), 'payload_len': len(payload),
    })
    return case


def build():
    for name in ('TOFU_CACHE_MID_MODE', 'TOFU_CACHE_MARKERS_NONCLAUDE'):
        os.environ.pop(name, None)
    profiles = _install_profiles()
    os.chdir('/')
    import lib
    lib.CACHE_EXTENDED_TTL = False
    corpora = {'builtin': builtin_tools(), 'hand': hand_tools()}
    preflight = []
    for corpus, tools in corpora.items():
        for index, tool in enumerate(tools):
            results = []
            by_model = {}
            for model in MODELS:
                result = _preflight(tool, model)
                if result.get('tools') == [tool] and not result.get('isolated'):
                    result = {'unchanged': True}
                if result not in results:
                    results.append(result)
                by_model[model] = results.index(result)
            preflight.append({'corpus': corpus, 'index': index, 'models': by_model, 'results': results})
    hand_names = [tool.get('function', {}).get('name') for tool in corpora['hand']]
    request_sets = {
        'builtin': ['builtin', list(range(len(corpora['builtin'])))],
        'hand': ['hand', [index for index, name in enumerate(hand_names) if name not in RAISING]],
        'raising': ['hand', [hand_names.index('root_plain'), hand_names.index('kimi_required_unhashable')]],
    }
    routes = [('kimi-k3', FRIDAY_ANTHROPIC, 'anthropic'), ('kimi-k2.7-code', FRIDAY_CHAT, 'openai'),
              ('claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic'), ('glm-5.3', FRIDAY_CHAT, 'openai')]
    requests = []
    for tool_set, (corpus, indexes) in request_sets.items():
        tools = [corpora[corpus][index] for index in indexes]
        for model, base_url, protocol in routes:
            case = _request(f'{tool_set}-{model}-{protocol}', model, base_url, protocol, tools)
            case['tools'] = tool_set
            requests.append(case)
    return json.dumps({
        'generator': 'packages/tofu-core-rs/scripts/gen_tool_schema_golden.py',
        'models': MODELS, 'max_tokens': 4096, 'temperature': 1.0, 'api_key': 'sk-golden', 'working_dir': '/',
        'gateway_profiles': profiles, 'messages': MESSAGES, 'corpora': corpora, 'preflight': preflight,
        'request_sets': request_sets,
        'requests': requests,
    }, indent=1, ensure_ascii=False) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding='utf-8') != text:
            print(f'{OUT} is stale; rerun {Path(__file__).name}', file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
