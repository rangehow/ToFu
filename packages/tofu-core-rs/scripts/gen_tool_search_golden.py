"""Regenerate packages/tofu-core-rs/tests/fixtures/tool_search_golden.json from Python.

``PYTHONPATH=$PWD .venv/bin/python3 packages/tofu-core-rs/scripts/gen_tool_search_golden.py [--check]``

Every case is one real Tofu task round: the root tool catalog comes from
``assemble_round_tools`` (the orchestrator's own assembly for a project chat
with web search/fetch and code execution), the request from ``build_body``,
and every private tool field from ``attach_tool_request_metadata`` (the
dispatch path's Tool Search / programmatic / multi-agent metadata) with the
case's task configuration (``tools.toolSearch``, ``tools.schemaBudgetTokens``,
``tools.programmaticCalling``, ``orchestration.multiAgent``), task latches
(fail-open, pins, required direct tools, an MCP authority tool) or a
``tool_choice``. ``prepare_request`` seals it on the FRIDAY Anthropic, OpenAI
and Kunlun Responses faces. The sealed payloads,
URLs and headers are the cargo test's expected values (SHA-256 and length of
the exact bytes, plus a skeleton with tools reduced to names); cases marked
``expect_unsupported`` must be refused by the Rust port. Catalog lists are
stored once under ``catalogs`` and referenced as ``{"$ref": key}``.
``--check`` exits 1 when the committed fixture differs from a regeneration.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_wire_golden import (  # noqa: E402
    FRIDAY_ANTHROPIC, FRIDAY_CHAT, FRIDAY_RESPONSES, KUNLUN, LOOP_MESSAGES, MT_AFFINITY, _install_profiles)

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'tool_search_golden.json'
WORKING_DIR = '/'
PROJECT_DIR = '/nonexistent/tofu-tool-search-golden'
OWNER = 7
CLAUDE = 'aws.claude-opus-4.8'
MCP_TOOL = {'type': 'function', 'function': {
    'name': 'mcp__docs__lookup', 'description': 'Look up a documentation page.',
    'parameters': {'type': 'object', 'properties': {'page': {'type': 'string'}}, 'required': ['page']}}}

# variant -> (task config overlay, task latches, body overrides)
VARIANTS = {
    'local': ({}, {}, {}),
    'off': ({'tools': {'toolSearch': 'off'}}, {}, {}),
    'fail-open': ({}, {'_tool_search_fail_open': True}, {}),
    'pins': ({}, {'_frontendSelectedToolNames': ['repo_map', 'todo_write']}, {}),
    'required-direct': ({}, {'_requiredDirectToolNames': ['timer_create', 'repo_map', 'browser_preview_page']}, {}),
    # Direct declarations push the local surface over the FRIDAY Claude
    # tool-name budget: optional names hide behind search_tools/execute_tools.
    'name-budget': ({}, {'_requiredDirectToolNames': [
        'timer_create', 'repo_map', 'browser_preview_page', 'integration_checkpoint', 'todo_write',
        'schedule_create', 'send_agent_message', 'get_agent_messages', 'publish_response_file']}, {}),
    'mcp': ({}, {'mcp': True}, {'tool_choice': {'type': 'function', 'function': {'name': MCP_TOOL['function']['name']}}}),
    'named-choice': ({}, {}, {'tool_choice': {'type': 'function', 'function': {'name': 'repo_map'}}}),
    'schema-budget': ({'tools': {'schemaBudgetTokens': 128000}}, {}, {}),
    'ptc-off': ({'tools': {'programmaticCalling': 'off'}}, {}, {}),
    'batch-tier': ({}, {}, {'_programmatic_tier': 'batch'}),
    'multi-read-only': ({'orchestration': {'multiAgent': 'read_only'}}, {}, {}),
    'small-surface': ({}, {'small': True}, {}),
    # Refused by the port: each needs Python machinery it does not carry.
    'schema-budget-cut': ({'tools': {'schemaBudgetTokens': 4000}}, {}, {}),
    'code-api': ({'tools': {'programmaticExposure': 'code_api'}}, {}, {}),
    'unknown-field': ({}, {}, {'_tool_search_hosted_backend': 'native'}),
}
UNSUPPORTED_VARIANTS = frozenset({'schema-budget-cut', 'code-api', 'unknown-field'})

# name, model, base_url, protocol, inference_profile, variant
CASES = [
    *[(f'claude-anthropic-{variant}', CLAUDE, FRIDAY_ANTHROPIC, 'anthropic', MT_AFFINITY, variant)
      for variant in ('local', 'off', 'fail-open', 'pins', 'required-direct', 'name-budget', 'mcp', 'schema-budget',
                      'ptc-off', 'batch-tier', 'multi-read-only', 'small-surface', 'schema-budget-cut',
                      'code-api', 'unknown-field')],
    *[(f'glm-chat-{variant}', 'glm-5.3', FRIDAY_CHAT, 'openai', MT_AFFINITY, variant)
      for variant in ('local', 'off', 'fail-open', 'mcp', 'named-choice', 'name-budget', 'schema-budget',
                      'ptc-off', 'small-surface')],
    *[(f'kimi-k3-anthropic-{variant}', 'kimi-k3', FRIDAY_ANTHROPIC, 'anthropic', MT_AFFINITY, variant)
      for variant in ('local', 'off', 'mcp')],
    *[(f'kimi-k3-chat-{variant}', 'kimi-k3', FRIDAY_CHAT, 'openai', MT_AFFINITY, variant)
      for variant in ('local',)],
    *[(f'gpt-responses-{variant}', 'kunlunbedrock.gpt-5.5', FRIDAY_RESPONSES, 'responses', KUNLUN, variant)
      for variant in ('local', 'off', 'fail-open', 'mcp', 'schema-budget', 'multi-read-only')],
]


def _isolate_storage():
    """A root chat that is not a project coordinator; no storage daemon."""
    from lib.conversations import project_coordination
    project_coordination.ProjectCoordinationRepository = (
        lambda owner: SimpleNamespace(read=lambda path: {'availability': 'unbound'}))
    from lib.channels import tools as channel_tools
    channel_tools.ChannelStore = lambda owner: SimpleNamespace(account=lambda channel: None)


def _task(model, config, latches):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    task = {'id': 'golden-task', 'convId': 'golden-conv', '_userId': OWNER,
            'config': {'projectPath': PROJECT_DIR, **copy.deepcopy(config)},
            'abort_event': threading.Event(),
            'messages': copy.deepcopy(LOOP_MESSAGES)}
    task['config'].update(_turnOwnerUserId=OWNER, memoryEnabled=True, mcpEnabled=False, plugins=[])
    tools, available = assemble_round_tools(task['config'], task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': PROJECT_DIR,
        'search_mode': 'multi', 'search_enabled': True, 'fetch_enabled': True,
        'code_exec_enabled': True, 'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'model': model})
    assert available and len(tools) >= 40, len(tools)
    if latches.get('mcp'):
        # MCP stays task authority (search-only); a declaration never shows it.
        task['_executable_tool_catalog'] = [*task['_executable_tool_catalog'], copy.deepcopy(MCP_TOOL)]
        task['_requiredDirectToolNames'] = [*task['_requiredDirectToolNames'], MCP_TOOL['function']['name']]
        task['_toolSearchCatalogSize'] = len(task['_executable_tool_catalog'])
    if latches.get('small'):
        # A narrowed delegated surface: few, all-eager tools.
        keep = ('read_files', 'search_files', 'run_command', 'edit_file', 'poll', 'kill')
        tools = [tool for tool in tools if tool['function']['name'] in keep]
        task['_executable_tool_catalog'] = copy.deepcopy(tools)
        task['_toolSearchCatalogSize'] = len(tools)
        task['_toolSearchableCount'] = 0
        task['_toolDiscoveryPolicyByName'] = {tool['function']['name']: 'eager' for tool in tools}
    for key, value in latches.items():
        if key.startswith('_'):
            task[key] = copy.deepcopy(value)
    return task, tools


def _skeleton(payload):
    """The payload with each tool reduced to its name: a readable diff aid."""
    tools = payload.get('tools')
    if isinstance(tools, list):
        payload['tools'] = [tool.get('name') or (tool.get('function') or {}).get('name') for tool in tools]
    return payload


class _Catalogs:
    def __init__(self):
        self.rows = {}

    def ref(self, rows):
        text = json.dumps(rows, ensure_ascii=False, sort_keys=False)
        key = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
        self.rows.setdefault(key, rows)
        return {'$ref': key}


def build():
    for name in ('TOFU_CACHE_MID_MODE', 'TOFU_CACHE_MARKERS_NONCLAUDE', 'TOFU_PTC_TIER'):
        os.environ.pop(name, None)
    profiles = _install_profiles()
    os.chdir(WORKING_DIR)
    _isolate_storage()
    import lib
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    lib.CACHE_EXTENDED_TTL = False
    catalogs = _Catalogs()
    cases = []
    for name, model, base_url, protocol, profile, variant in CASES:
        config, latches, overrides = VARIANTS[variant]
        task, tools = _task(model, config, latches)
        messages = copy.deepcopy(LOOP_MESSAGES)
        body = build_body(model, copy.deepcopy(messages), max_tokens=4096, tools=copy.deepcopy(tools),
                          provider_id='golden', precomputed_input_tokens=100, context_window=200000,
                          inference_profile=copy.deepcopy(profile))
        built = set(body)
        attach_tool_request_metadata(task, body, messages, tools, model=model, round_num=1, tid=task['id'])
        body.update(copy.deepcopy(overrides))
        # Assembly builds this lookup from a set; only membership is read, so
        # a sorted copy keeps the fixture independent of PYTHONHASHSEED.
        body['_tool_discovery_policy_by_name'] = dict(sorted(body['_tool_discovery_policy_by_name'].items()))
        body['_conv_id'] = 'golden-conv'
        extra = {}
        for key in body:
            if key in built and key != 'tool_choice':
                continue
            value = body[key]
            json.dumps(value)  # every dispatch field the cases use is JSON
            extra[key] = copy.deepcopy(value)
        for key in ('_executable_tool_catalog', '_tool_wire_catalog'):
            if isinstance(extra.get(key), list):
                extra[key] = catalogs.ref(extra[key])
        case = {'name': name, 'protocol': protocol, 'model': model, 'base_url': base_url,
                'inference_profile': profile, 'lineage': extra.pop('_conv_id'),
                'tools': catalogs.ref(copy.deepcopy(tools)), 'body_extra': extra}
        try:
            plan = prepare_request(body, api_key='sk-golden', base_url=base_url, api_protocol=protocol)
        except Exception as error:  # the oracle's exception is the expected result
            case['error'] = {'class': type(error).__name__, 'message': str(error)}
        else:
            payload = plan.body.payload
            case.update({
                'url': plan.url, 'headers': [[k, v] for k, v in plan.hdrs.items() if k != 'M-TraceId'],
                'payload_sha256': hashlib.sha256(payload).hexdigest(), 'payload_len': len(payload),
                'payload_skeleton': _skeleton(json.loads(payload)),
            })
        if variant in UNSUPPORTED_VARIANTS:
            case['expect_unsupported'] = True
        cases.append(case)
    return json.dumps({
        'generator': 'packages/tofu-core-rs/scripts/gen_tool_search_golden.py',
        'max_tokens': 4096, 'temperature': 1.0, 'api_key': 'sk-golden', 'working_dir': WORKING_DIR,
        'gateway_profiles': profiles, 'messages': LOOP_MESSAGES, 'catalogs': catalogs.rows, 'cases': cases,
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
