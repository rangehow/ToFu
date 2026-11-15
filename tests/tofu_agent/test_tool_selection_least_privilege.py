"""Least-privilege tool selection for headless runs (lib/agent_core/run_contract.py).

CODE-HEALTH-EDGE-1's review of 8abbcaef9b, verified by running it: ``tofu-agent
run --tools ''`` left every capability key absent, and the consumers
substitute ON for an absent key (model_config searchMode/fetchEnabled, the MCP
gate), so a "no optional tools" session still exposed web search, fetch_url
and MCP. A single tag (``--tools browser``) was dropped at DEBUG.

These specs follow a real CLI parse through build_agent_config, the
storage-free policy, the harness profile and the real consumers
(_resolve_model_config, _assemble_tool_list), as ``tofu-agent run`` does;
never a CLI dict at a fake runtime.
"""
from __future__ import annotations

import pytest

from lib.agent_core.run_contract import (
    TOOL_TAG_MAP,
    TOOLS_NONE,
    ToolSelectionError,
    apply_storage_free_runtime_policy,
    build_agent_config,
)
from lib.tasks_pkg.model_config import _assemble_tool_list, _resolve_model_config
from tofu_agent import cli
from tofu_agent.harness_profile import apply_harness_profile

pytestmark = pytest.mark.unit

# Tools the capability flags govern: network, MCP, memory, the browser
# extension, desktop, image generation, human guidance and the scheduler.
# browser_preview_page is the page_preview default family (a server-side
# renderer, not gated by browserEnabled); the repair session's ToolSpec
# allowlist excludes it with the other default families.
FORBIDDEN_PREFIXES = ('web_search', 'fetch_url', 'search_mcp', 'mcp__', 'memory', 'browser_', 'desktop_',
                      'generate_image', 'image_', 'ask_human', 'schedule', 'timer_')
DEFAULT_FAMILY_TOOLS = {'browser_preview_page'}


def _session(extra: list[str], tmp_path, argv: list[str] | None = None):
    """The capability consumers' view of one ``tofu-agent run`` invocation."""
    args = cli._parser().parse_args(argv or ['run', '--task', 'x', '--cwd', str(tmp_path), *extra])
    _task, config = cli._prepare_run(args)
    cfg = apply_storage_free_runtime_policy(build_agent_config('probe-model', config, None))
    cfg = apply_harness_profile(cfg, 'auto')
    resolved = _resolve_model_config(dict(cfg), 'least-privilege')
    tools, _has_tools = _assemble_tool_list(
        cfg, resolved['project_path'], resolved['project_enabled'], 'least-privilege',
        resolved['search_mode'], resolved['search_enabled'], resolved['fetch_enabled'],
        resolved['code_exec_enabled'], resolved['browser_enabled'], resolved['desktop_enabled'],
        image_gen_enabled=resolved.get('image_gen_enabled', False),
        human_guidance_enabled=resolved.get('human_guidance_enabled', False),
        scheduler_enabled=resolved.get('scheduler_enabled', False),
        messages=[], conv_id='')
    names = {tool['function']['name'] for tool in tools or []}
    return cfg, resolved, names


def test_an_empty_selection_resolves_every_optional_capability_off(tmp_path):
    cfg, resolved, names = _session(['--tools', ''], tmp_path)
    assert resolved['search_enabled'] is False and resolved['search_mode'] == 'off'
    assert resolved['fetch_enabled'] is False
    assert resolved['memory_enabled'] is False
    assert resolved['browser_enabled'] is False and resolved['desktop_enabled'] is False
    assert resolved['code_exec_enabled'] is False
    # The MCP gate reads cfg.get('mcpEnabled', True): the key must be present and off.
    assert cfg.get('mcpEnabled', True) is False
    assert not {name for name in names - DEFAULT_FAMILY_TOOLS if name.startswith(FORBIDDEN_PREFIXES)}
    # The project tools remain: the session can still read, edit and test its lane.
    assert {'read_files', 'write_file', 'run_command'} <= names


def test_the_repair_session_exposes_exactly_the_intended_tool_set(tmp_path):
    """The O4 adapter's own argv, parsed by the real CLI: lane tools only."""
    from serverctl_pkg.oob_runtime import PinnedRuntime
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('repair', encoding='utf-8')
    routing = tmp_path / 'routing.json'
    routing.write_text('{}', encoding='utf-8')
    argv = PinnedRuntime(root=tmp_path / 'runtime', commit='abc').argv(
        prompt=prompt, output=tmp_path / 'result.json', workspace=tmp_path,
        routing=routing)
    cfg, resolved, names = _session([], tmp_path, argv=argv[argv.index('tofu_agent') + 1:])
    assert names == MINIMAL_TOOL_SET
    assert resolved['search_enabled'] is False and resolved['fetch_enabled'] is False
    assert cfg.get('mcpEnabled', True) is False


def test_star_keeps_the_full_storage_free_surface(tmp_path):
    _cfg, resolved, names = _session(['--tools', '*'], tmp_path)
    assert resolved['search_enabled'] is True and resolved['fetch_enabled'] is True
    assert 'web_search' in names and 'fetch_url' in names


@pytest.mark.parametrize('selection', ['', 'none', [], [''], ['none']])
def test_every_empty_spelling_is_the_complete_off_map(selection):
    cfg = build_agent_config('m', {'tools': selection}, None)
    assert {key: cfg[key] for key in TOOLS_NONE} == TOOLS_NONE


def test_the_off_map_covers_every_capability_a_tag_can_enable():
    assert set(TOOLS_NONE) == {key for key, _value in TOOL_TAG_MAP.values()}
    assert TOOLS_NONE['searchMode'] == 'off'
    assert all(value is False for key, value in TOOLS_NONE.items() if key != 'searchMode')


def test_a_single_tag_is_parsed_not_dropped(tmp_path):
    assert build_agent_config('m', {'tools': 'browser'}, None)['browserEnabled'] is True
    _cfg, resolved, _names = _session(['--tools', 'browser'], tmp_path)
    assert resolved['browser_enabled'] is True


def test_none_plus_tags_enables_exactly_those_tags():
    cfg = build_agent_config('m', {'tools': ['none', 'fetch']}, None)
    assert cfg['fetchEnabled'] is True and cfg['searchMode'] == 'off' and cfg['mcpEnabled'] is False


def test_an_unknown_tag_is_refused_with_a_typed_error(tmp_path):
    with pytest.raises(ToolSelectionError, match="'nope'"):
        build_agent_config('m', {'tools': ['search', 'nope']}, None)
    with pytest.raises(ToolSelectionError):
        build_agent_config('m', {'tools': 7}, None)
    assert issubclass(ToolSelectionError, ValueError)
    args = cli._parser().parse_args(['run', '--task', 'x', '--cwd', str(tmp_path), '--tools', 'nope'])
    with pytest.raises(ToolSelectionError):
        cli._prepare_run(args)


def test_the_cli_refuses_an_unknown_tag_before_any_run(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(['--env-file', str(tmp_path / 'absent.env'), 'run', '--task', 'x', '--tools', 'nope'])
    assert exit_info.value.code == 2
    assert 'nope' in capsys.readouterr().err


def test_the_request_refuses_an_unknown_tag_for_every_entry_point():
    from tofu_agent.models import AgentConfigurationError, AgentRequest
    with pytest.raises(AgentConfigurationError, match='unknown tool tag'):
        AgentRequest(messages=[{'role': 'user', 'content': 'x'}], config={'tools': ['nope']})
    with pytest.raises(AgentConfigurationError, match='unknown tool tag'):
        AgentRequest(messages=[{'role': 'user', 'content': 'x'}], capabilities={'tools': 'nope'})
    AgentRequest(messages=[{'role': 'user', 'content': 'x'}], config={'tools': {'nativeExposure': 'full'}})


# Pinned on purpose: a new tool reaching a no-optional-tools session must be a
# reviewed change of this set, not an accident of a new registry default.
MINIMAL_TOOL_SET = {'edit_file', 'find_files', 'kill', 'poll', 'read_files', 'repo_map', 'run_command',
                    'search_files', 'todo_write', 'write_file'}
