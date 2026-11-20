"""Regenerate packages/tofu-core-rs/tests/fixtures/wire_golden.json from Python.

``PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/gen_wire_golden.py [--check]``

Every case runs one history through ``build_body`` + ``prepare_request`` for
one route Tofu uses: the three generic wire faces, the FRIDAY gateway's
Anthropic face (Claude with cache markers, the tool-name budget, extended
TTL and Mt-Session-Id; Kimi without markers), its OpenAI face (GLM, DeepSeek
and Qwen with markers; Doubao and MiniMax without; Kimi's MFJS schema
dialect; Claude with a tool result that cannot carry a marker), its Responses
faces (Kunlun replay, temperature omission) and the CatPaw gateway (declared
system identity and working-directory header). The sealed payloads, URLs and
headers are the cargo test's expected bytes; the gateway profiles the oracle
loaded are recorded so the Rust port reads the same declared traits.
The ``catalog-*`` cases resolve a registered catalog profile with the face's
features and pass build_body's reasoning arguments (effort, depth, off,
explicit budget), including Gemini's thought signatures and Mistral's
tool-call IDs; a case whose oracle raises records the exception instead.
``--check`` exits 1 when the committed fixture differs from a regeneration.
"""
import argparse
import base64
import copy
import json
import os
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'wire_golden.json'
WORKING_DIR = '/'
TOOLS = [
    {'type': 'function', 'function': {'name': 'run_command', 'description': 'Run a shell command \u2014 bounded.',
     'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}}},
    {'type': 'function', 'function': {'name': 'send', 'description': 'Send to one target.',
     'parameters': {'additionalProperties': False, 'oneOf': [{'required': ['a']}, {'required': ['b']}],
                    'properties': {'a': {'type': 'string'}, 'b': {'type': 'integer'}}, 'type': 'object'}}},
    {'type': 'function', 'decoded_result_schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                                                   'description': 'r\u00e9sultat'},
     'function': {'name': 'read_files', 'description': 'Read files.',
                  'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}},
]
MESSAGES = [
    {'role': 'system', 'content': 'You are a test harness.'},
    {'role': 'system', 'content': 'Managed section.', '_isMeta': True},
    {'role': 'user', 'content': [{'type': 'text', 'text': 'Run it '}, {'type': 'text', 'text': '   '},
                                 {'type': 'text', 'text': 'please \u2603'}]},
    {'role': 'user', 'content': '<system-reminder>ambient</system-reminder>', '_isMeta': True},
    {'role': 'assistant', 'content': 'Running.', 'tool_calls': [
        {'id': 'call_a', 'type': 'function', 'function': {'name': 'run_command', 'arguments': '{"command": "true"}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_a', 'content': '$ true\n\n[exit code: 0]'},
    {'role': 'tool', 'tool_call_id': 'call_zzz', 'content': 'orphan'},
    {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_b', 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{"path": "f0.txt"}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_b', 'content': 'File: f0.txt (1 lines)\n   1\tnoop 0\n'},
    {'role': 'user', 'content': 'A private progress event.', '_isMeta': True},
]


def _call(call_id, name, arguments):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


# A tool loop that ends on tool results with no prose on the last turns: the
# OpenAI-wire tail marker must walk back past unmarkable tool messages, and
# the Anthropic wire hoists the marker onto the tool_result block.
LOOP_MESSAGES = [
    {'role': 'system', 'content': [{'type': 'text', 'text': 'Static prompt.'},
                                   {'type': 'text', 'text': 'Memory block.'}]},
    {'role': 'user', 'content': 'Inspect the repo.'},
    {'role': 'assistant', 'content': 'Looking.', 'tool_calls': [_call('toolu_1', 'run_command', '{"command": "ls"}')]},
    {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'a\nb'},
    {'role': 'assistant', 'content': None, 'tool_calls': [
        _call('toolu_2', 'read_files', '{"path": "a"}'), _call('toolu_3', 'read_files', '{"path": "b"}')]},
    {'role': 'tool', 'tool_call_id': 'toolu_2', 'content': 'A'},
    {'role': 'tool', 'tool_call_id': 'toolu_3', 'content': [{'type': 'text', 'text': 'B'}]},
    {'role': 'assistant', 'content': '', 'tool_calls': [_call('toolu_4', 'run_command', '{"command": "pwd"}')]},
    {'role': 'tool', 'tool_call_id': 'toolu_4', 'content': '/w'},
]

# A trailing assistant turn Claude cannot accept as a prefill: converted to a
# user turn whose sentinel keeps the tail marker off it.
PREFILL_MESSAGES = [
    {'role': 'system', 'content': 'Be brief.'},
    {'role': 'user', 'content': 'Start.'},
    {'role': 'assistant', 'content': 'Partial answer so far'},
]


def _named_tool(name):
    return {'type': 'function', 'function': {'name': name, 'description': f'{name} tool.',
                                             'parameters': {'type': 'object', 'properties': {}}}}


# Joined names well above the FRIDAY Claude budget (448 bytes counted as
# sum(len(name) + 1)); search_tools/execute_tools and poll stay on the wire.
_LONG_NAMES = [f'{stem}_{"x" * 40}' for stem in (
    'browser_navigate', 'browser_screenshot', 'browser_read_page', 'desktop_click',
    'desktop_type', 'knowledge_search', 'memory_lookup', 'paper_outline', 'slides_render', 'video_frames')]
BUDGET_TOOLS = [_named_tool('search_tools'), _named_tool('execute_tools'), _named_tool('poll'),
                *[_named_tool(name) for name in _LONG_NAMES], *copy.deepcopy(TOOLS)]
# Same names without the discovery pair: nothing may be hidden.
UNDISCOVERABLE_TOOLS = [tool for tool in BUDGET_TOOLS
                        if tool['function']['name'] not in ('search_tools', 'execute_tools')]
# Schemas the MFJS projection rewrites (annotations, tuple items, a
# required-but-undeclared property, a heterogeneous enum) and one it keeps.
KIMI_TOOLS = [
    copy.deepcopy(TOOLS[0]),
    {'type': 'function', 'function': {'name': 'shape', 'description': 'MFJS shapes.', 'parameters': {
        'type': 'object', 'title': 'Shape', 'required': ['mode', 'extra'],
        'properties': {
            'mode': {'type': 'string', 'enum': ['a', 'b'], 'examples': ['a']},
            'mixed': {'enum': ['x', 1]},
            'pair': {'type': 'array', 'prefixItems': [{'type': 'string'}], 'items': {'type': 'integer'}},
            'map': {'type': 'object', 'patternProperties': {'^x': {'type': 'string'}},
                    'additionalProperties': False},
            'list': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'default': []},
            'choice': {'anyOf': [{'type': 'string'}, {'type': 'integer', 'format': 'int32'}]},
            'flag': True,
        }}}},
]

FRIDAY_ANTHROPIC = 'https://api.openai.com/v1/anthropic'
FRIDAY_CHAT = 'https://api.openai.com/v1'
FRIDAY_RESPONSES = 'https://api.openai.com/v1'
CATPAW = 'https://mcli.internal.example.com'
LOCAL = 'http://127.0.0.1:9/v1'
MT_AFFINITY = {'features': {'cache_affinity_mt_session_id': 'supported'}}
KUNLUN = {'sampling': {'temperature': 'omit'},
          'features': {'responses_reasoning_replay': 'supported', 'cache_affinity_mt_session_id': 'supported'}}
MESSAGE_SETS = {'base': MESSAGES, 'loop': LOOP_MESSAGES, 'prefill': PREFILL_MESSAGES}
TOOL_SETS = {'base': TOOLS, 'budget': BUDGET_TOOLS, 'undiscoverable': UNDISCOVERABLE_TOOLS, 'kimi': KIMI_TOOLS,
             'none': None}

# name, model, base_url, protocol, messages, tools, inference_profile, extended_ttl, lineage
CASES = [
    ('generic-openai', 'golden-model', LOCAL, 'openai', 'base', 'base', None, False, ''),
    ('generic-anthropic', 'golden-model', LOCAL, 'anthropic', 'base', 'base', None, False, ''),
    ('generic-responses', 'golden-model', LOCAL, 'responses', 'base', 'base', None, False, ''),
    ('friday-claude-anthropic', 'aws.claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic', 'base', 'base',
     MT_AFFINITY, False, 'conv-1'),
    ('friday-claude-anthropic-loop', 'aws.claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic', 'loop', 'base',
     MT_AFFINITY, False, 'conv-1'),
    ('friday-claude-anthropic-extended-ttl', 'aws.claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic', 'loop',
     'base', MT_AFFINITY, True, 'conv-2'),
    ('friday-claude-anthropic-prefill', 'aws.claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic', 'prefill',
     'none', MT_AFFINITY, False, ''),
    ('friday-claude-tool-name-budget', 'aws.claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic', 'loop', 'budget',
     MT_AFFINITY, False, 'conv-3'),
    ('friday-claude-tool-name-budget-undiscoverable', 'aws.claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic',
     'loop', 'undiscoverable', MT_AFFINITY, False, 'conv-3'),
    ('friday-claude-openai-face', 'aws.claude-opus-4.8', FRIDAY_CHAT, 'openai', 'loop', 'base', MT_AFFINITY,
     False, 'conv-4'),
    ('friday-kimi-k3-anthropic', 'kimi-k3', FRIDAY_ANTHROPIC, 'anthropic', 'loop', 'budget', MT_AFFINITY,
     False, 'conv-5'),
    ('friday-kimi-k3-anthropic-mfjs', 'kimi-k3', FRIDAY_ANTHROPIC, 'anthropic', 'base', 'kimi', MT_AFFINITY,
     False, ''),
    ('friday-kimi-k27-code-chat', 'kimi-k2.7-code', FRIDAY_CHAT, 'openai', 'loop', 'kimi', MT_AFFINITY, False,
     'conv-6'),
    ('friday-glm-chat', 'glm-5.3', FRIDAY_CHAT, 'openai', 'base', 'base', MT_AFFINITY, False, 'conv-7'),
    ('friday-glm-chat-loop', 'glm-5.1', FRIDAY_CHAT, 'openai', 'loop', 'base', MT_AFFINITY, False, 'conv-7'),
    ('friday-glm-chat-markers-declared-off', 'glm-5.3', FRIDAY_CHAT, 'openai', 'loop', 'base',
     {'features': {'chat_cache_breakpoints': 'unsupported'}}, False, ''),
    ('friday-deepseek-chat', 'deepseek-v4-flash', FRIDAY_CHAT, 'openai', 'loop', 'base', MT_AFFINITY, False,
     'conv-8'),
    ('friday-qwen-chat', 'qwen3.5-plus', FRIDAY_CHAT, 'openai', 'base', 'base', MT_AFFINITY, False, ''),
    ('friday-doubao-chat-no-markers', 'Doubao-Seed-2.0-pro', FRIDAY_CHAT, 'openai', 'loop', 'base', MT_AFFINITY,
     False, 'conv-9'),
    ('friday-doubao-chat-base', 'Doubao-Seed-2.0-pro', FRIDAY_CHAT, 'openai', 'base', 'base', None, False, ''),
    ('friday-minimax-chat', 'MiniMax-M2.5', FRIDAY_CHAT, 'openai', 'loop', 'base', MT_AFFINITY, False, ''),
    ('friday-kunlun-responses', 'kunlunbedrock.gpt-6.1-sol-m17', FRIDAY_RESPONSES, 'responses', 'loop', 'base',
     KUNLUN, False, 'conv-10'),
    ('friday-tokrouter-responses', 'tokrouter.gpt-5.5.distill-m17', FRIDAY_RESPONSES, 'responses', 'base', 'base',
     KUNLUN, False, ''),
    ('catpaw-kimi-k3', 'kimi-k3', CATPAW, 'anthropic', 'base', 'base', None, False, ''),
    ('catpaw-glm-loop', 'glm-5.3', CATPAW, 'anthropic', 'loop', 'base', None, False, ''),
    # Registered catalog profiles (resolved with the face's features) under
    # non-default reasoning settings; the trailing element is build_body's
    # reasoning arguments. scripts/gen_catalog_golden.py sweeps every model.
    ('catalog-claude-opus-5-effort-max', 'claude-opus-5', FRIDAY_ANTHROPIC, 'anthropic', 'loop', 'base',
     'catalog', False, 'conv-11', {'effort': 'max'}),
    ('catalog-claude-opus-4.8-off', 'claude-opus-4.8', FRIDAY_ANTHROPIC, 'anthropic', 'base', 'base',
     'catalog', False, '', {'thinking_enabled': False}),
    ('catalog-claude-sonnet-4.5-budget', 'claude-sonnet-4.5', FRIDAY_ANTHROPIC, 'anthropic', 'loop', 'base',
     'catalog', False, '', {'extra': {'thinking': {'type': 'enabled', 'budget_tokens': 4096}}}),
    ('catalog-claude-opus-4.5-depth-xhigh', 'claude-opus-4.5', FRIDAY_ANTHROPIC, 'anthropic', 'base', 'base',
     'catalog', False, '', {'thinking_depth': 'xhigh'}),
    ('catalog-claude-opus-5.5-off-floors', 'claude-opus-5.5', FRIDAY_ANTHROPIC, 'anthropic', 'base', 'none',
     'catalog', False, '', {'thinking_depth': 'off'}),
    ('catalog-claude-opus-5.5-exact-ultra-raises', 'claude-opus-5.5', FRIDAY_ANTHROPIC, 'anthropic', 'base',
     'none', 'catalog', False, '', {'effort': 'ultra'}),
    ('catalog-kimi-k3-effort-low', 'kimi-k3', FRIDAY_ANTHROPIC, 'anthropic', 'loop', 'base', 'catalog', False,
     'conv-12', {'effort': 'low'}),
    ('catalog-kimi-k27-code-off', 'kimi-k2.7-code', FRIDAY_CHAT, 'openai', 'loop', 'base', 'catalog', False,
     '', {'thinking_enabled': False}),
    ('catalog-kimi-k26-off', 'kimi-k2.6', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False, '',
     {'thinking_depth': 'off'}),
    ('catalog-glm-5.3-depth-high', 'glm-5.3', FRIDAY_CHAT, 'openai', 'loop', 'base', 'catalog', False,
     'conv-13', {'thinking_depth': 'high'}),
    ('catalog-glm-5.2-depth-minimal', 'glm-5.2', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False, '',
     {'thinking_depth': 'minimal'}),
    ('catalog-deepseek-v4-flash-off', 'deepseek-v4-flash', FRIDAY_CHAT, 'openai', 'loop', 'base', 'catalog',
     False, '', {'thinking_depth': 'off'}),
    ('catalog-deepseek-v4-pro-ultra', 'deepseek-v4-pro', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog',
     False, '', {'thinking_depth': 'ultra'}),
    ('catalog-qwen3.8-max-enabled', 'qwen3.8-max', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False, '',
     {'thinking_enabled': True}),
    ('catalog-qwen-max-off-unsupported', 'qwen-max', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False,
     '', {'thinking_depth': 'off'}),
    ('catalog-minimax-m2.5-split', 'MiniMax-M2.5', FRIDAY_CHAT, 'openai', 'loop', 'base', 'catalog', False, '',
     {'thinking_depth': 'medium'}),
    ('catalog-doubao-seed-off', 'Doubao-seed-2.1-pro', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False,
     '', {'thinking_enabled': False}),
    ('catalog-hy4-off-disabled-effort', 'hy4', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False, '',
     {'thinking_depth': 'off'}),
    ('catalog-grok-4.6-depth-max', 'grok-4.6', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog', False, '',
     {'thinking_depth': 'max'}),
    ('catalog-gemini-3.6-flash-loop', 'gemini-3.6-flash', FRIDAY_CHAT, 'openai', 'loop', 'base', 'catalog',
     False, 'conv-14', {'thinking_depth': 'low'}),
    ('catalog-gemini-4-hydrogen-default', 'gemini-4-hydrogen', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog',
     False, '', None),
    ('catalog-mistral-large-loop', 'mistral-large-4-0', 'https://api.mistral.ai/v1', 'openai', 'loop', 'base',
     'catalog', False, '', {'thinking_depth': 'xhigh'}),
    ('catalog-mistral-large-anthropic-ids', 'mistral-large-4-0', LOCAL, 'anthropic', 'base', 'base', None, False,
     '', None),
    ('catalog-gpt-5.5-kunlun-high', 'gpt-5.5', FRIDAY_RESPONSES, 'responses', 'loop', 'base', 'catalog', False,
     'conv-15', {'thinking_depth': 'high'}),
    ('catalog-gpt-5.4-kunlun-off', 'gpt-5.4', FRIDAY_RESPONSES, 'responses', 'base', 'base', 'catalog', False,
     '', {'thinking_enabled': False}),
    ('catalog-gpt-6-astra-legacy-xhigh-raises', 'gpt-6-astra', FRIDAY_RESPONSES, 'responses', 'base', 'base',
     'catalog', False, '', {'thinking_depth': 'xhigh'}),
    ('catalog-gpt-6-astra-legacy-on-chat', 'gpt-6-astra', FRIDAY_CHAT, 'openai', 'base', 'base', 'catalog',
     False, '', {'thinking_depth': 'xhigh'}),
    ('generic-openai-legacy-depth', 'golden-model', LOCAL, 'openai', 'base', 'base', None, False, '',
     {'thinking_depth': 'high'}),
]

_FACE_PROFILES = {FRIDAY_ANTHROPIC: MT_AFFINITY, FRIDAY_CHAT: MT_AFFINITY, FRIDAY_RESPONSES: KUNLUN}


def _catalog_profile(model, base_url, protocol):
    """The registered profile resolved with the face's declared features."""
    from lib.model_routing.inference import resolve_inference_profile
    from lib.model_routing.model_defaults import current_model_defaults
    (profile, _previous), = [entry for ref, entry in current_model_defaults().exact.items()
                             if ref.model_id == model]
    face = _FACE_PROFILES.get(base_url)
    return resolve_inference_profile(model={'inference_profile': profile},
                                     offering={'inference_profile': face} if face else None,
                                     connection={'protocol': protocol})


PROFILE_DIR = Path(__file__).resolve().parents[3] / 'bundles' / 'yourprovider' / 'gateway_profiles'


def _install_profiles():
    """Pin the oracle to the tracked FRIDAY/CatPaw profiles, whatever is installed."""
    from lib.provider import gateway_profiles as registry
    profiles = tuple(registry.parse_gateway_profile(json.loads(path.read_text(encoding='utf-8')), path.name)
                     for path in sorted(PROFILE_DIR.glob('*.json')))
    with registry._cache_lock:
        registry._cache = profiles
    return [{'id': p['id'], 'hosts': list(p['hosts']), 'provider_ids': list(p['provider_ids']),
             'traits': dict(p['traits'])} for p in profiles]


def build():
    for name in ('TOFU_CACHE_MID_MODE', 'TOFU_CACHE_MARKERS_NONCLAUDE'):
        os.environ.pop(name, None)
    profiles = _install_profiles()
    os.chdir(WORKING_DIR)
    import lib
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    cases = []
    for name, model, base_url, protocol, messages, tools, profile, extended_ttl, lineage, *rest in CASES:
        reasoning = rest[0] if rest else None
        if profile == 'catalog':
            profile = _catalog_profile(model, base_url, protocol)
        lib.CACHE_EXTENDED_TTL = extended_ttl
        case = {
            'name': name, 'protocol': protocol, 'model': model, 'base_url': base_url,
            'messages': messages, 'tools': tools, 'inference_profile': profile,
            'extended_cache_ttl': extended_ttl, 'lineage': lineage,
        }
        if reasoning is not None:
            case['reasoning'] = reasoning
        try:
            body = build_body(model, copy.deepcopy(MESSAGE_SETS[messages]), max_tokens=4096,
                              tools=copy.deepcopy(TOOL_SETS[tools]), provider_id='golden',
                              precomputed_input_tokens=100, context_window=200000,
                              inference_profile=copy.deepcopy(profile), **copy.deepcopy(reasoning or {}))
            if lineage:
                body['_conv_id'] = lineage
            plan = prepare_request(body, api_key='sk-golden', base_url=base_url, api_protocol=protocol)
        except Exception as exc:  # the oracle's exception is the expected outcome
            case['error'] = {'class': type(exc).__name__, 'message': str(exc)}
        else:
            case.update({
                'url': plan.url, 'headers': [[k, v] for k, v in plan.hdrs.items() if k != 'M-TraceId'],
                'payload_b64': base64.b64encode(plan.body.payload).decode(),
            })
        cases.append(case)
    lib.CACHE_EXTENDED_TTL = False
    return json.dumps({
        'generator': 'packages/tofu-core-rs/scripts/gen_wire_golden.py',
        'max_tokens': 4096, 'temperature': 1.0, 'api_key': 'sk-golden', 'working_dir': WORKING_DIR,
        'gateway_profiles': profiles, 'message_sets': MESSAGE_SETS, 'tool_sets': TOOL_SETS, 'cases': cases,
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
