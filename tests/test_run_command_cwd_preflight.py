"""Invalid cwd is a not-run refusal, never a retryable child exit."""
from unittest.mock import patch

import pytest

from lib.project_mod.run_command import tool_run_command
from lib.project_mod.tools import _exec_run_command
from lib.project_mod import config
from lib.tools.meta import build_project_tool_meta

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_invalid_directory_never_spawns(tmp_path, kind):
    target = tmp_path / 'bad cwd?'
    if kind == 'file':
        target.write_text('not a directory')
    with patch('subprocess.Popen') as spawn:
        result = tool_run_command(str(target), 'echo should-not-run')
    spawn.assert_not_called()
    assert 'Command was not started' in result
    assert str(target) in result
    assert 'Correct working_dir' in result
    assert '[exit code:' not in result
    meta = build_project_tool_meta('run_command', {'command': 'echo should-not-run'}, result)
    assert meta['notRun'] is True


def test_invalid_explicit_cwd_preserves_sticky_and_blocks_fastpath(tmp_path):
    conv = 'test-cwd-preflight'
    sub = tmp_path / 'valid sub'
    sub.mkdir()
    config.set_conv_roots(conv, str(tmp_path))
    config.set_conv_cwd(conv, str(sub))
    try:
        with patch('lib.project_mod.tools.tool_list_dir') as reader, patch('lib.project_mod.tools.tool_run_command') as runner:
            result = _exec_run_command({'command': 'ls', 'working_dir': str(tmp_path / 'missing')}, str(tmp_path), conv, 'test', {})
        reader.assert_not_called()
        runner.assert_not_called()
        assert 'Command was not started' in result
        assert config.get_conv_cwd(conv) == str(sub)
    finally:
        config.clear_conv_state(conv)


def test_standalone_invalid_cwd_blocks_fastpath(tmp_path):
    from lib.project_mod.tools import execute_standalone_command
    with patch('lib.project_mod.tools.tool_list_dir') as reader:
        result = execute_standalone_command('run_command', {'command': 'ls'}, working_dir=str(tmp_path / 'missing'))
    reader.assert_not_called()
    assert 'Command was not started' in result


def test_existing_directory_with_spaces_runs(tmp_path):
    target = tmp_path / 'valid cwd?'
    target.mkdir()
    result = tool_run_command(str(target), 'pwd')
    assert str(target) in result
    assert '[exit code: 0]' in result
