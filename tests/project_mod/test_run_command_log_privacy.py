"""Commands reach the logs by identity, never by text (docs/LOGGING.md, "Commands are logged by identity").

MEMORY-SELF-QUERY, 2026-11-12: ``run_command: $ <command>`` wrote every
command's full text to logs/app.log at INFO, so anything a model typed into a
command (a heredoc script, personal text, a search key) outlived every deletion:
the title of the memory the owner ordered deleted was still there. A command now
reaches application, error and audit logs only as its UTF-8 size, a short
SHA-256 digest, its workspace root and the conversation, Turn and tool-call ids.
The transcript keeps the text as the tool call's argument.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
import threading
from pathlib import Path

import pytest

from lib.tasks_pkg import background_commands
from lib.tasks_pkg.executor._registry import tool_registry
from lib.tasks_pkg.handlers import _background_command as background
from lib.tasks_pkg.handlers import project as project_handler

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MARKER = 'tofu-log-privacy-5b81c0e2'
COMMAND = f'printf ok # {MARKER}'
CONV, TURN = 'conv-log-privacy', 'turn-log-privacy'


@pytest.fixture(autouse=True)
def _isolated(caplog, monkeypatch, tmp_path):
    from collections import OrderedDict
    from lib.project_mod import config
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger='lib')
    monkeypatch.setattr(config, '_conv_roots', OrderedDict())
    monkeypatch.setattr(config, '_conv_primary', {})
    config.set_conv_roots(CONV, str(tmp_path))
    with background_commands._lock:
        background_commands._registry.clear()
    yield
    with background_commands._lock:
        background_commands._registry.clear()


def _run(monkeypatch, root, arguments, call_id):
    from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
    monkeypatch.setattr(project_handler, '_finalize_tool_round', lambda *args, **kwargs: None)
    task = {'id': 'task-log-privacy', 'convId': CONV, '_turnId': TURN, '_userId': 7,
            'aborted': False, 'toolRounds': []}
    round_entry = {'query': 'run_command', 'toolCallId': call_id, 'toolName': 'run_command'}
    # As the executor does: the call's runtime context is registered before its handler runs.
    context = context_for_task(task, round_num=1, tool_call_id=call_id,
                               tool_name='run_command', round_entry=round_entry)
    try:
        _, content, _ = project_handler._handle_project_tool(
            task, None, 'run_command', call_id, arguments, 1, round_entry, {}, str(root), True)
    finally:
        unregister_context(context)
    return content


def _logged(caplog) -> list[str]:
    lines = []
    for record in caplog.records:
        lines.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            lines.append(logging.Formatter().formatException(record.exc_info))
    return lines


def _identity(command: str) -> str:
    data = command.encode('utf-8')
    return f'cmd_bytes={len(data)} cmd_sha256={hashlib.sha256(data).hexdigest()[:16]}'


def test_a_command_is_logged_by_identity_and_ids_never_by_text(monkeypatch, tmp_path, caplog):
    content = _run(monkeypatch, tmp_path, {'command': COMMAND}, 'call-log-privacy')
    assert 'ok' in content and MARKER in content  # the tool result, the transcript's copy
    lines = _logged(caplog)
    assert [line for line in lines if MARKER in line] == []
    [started] = [line for line in lines if line.startswith('run_command: ')]
    assert started.startswith(f'run_command: {_identity(COMMAND)} ')
    for field in (f'root={tmp_path.name}', f'conv={CONV}', f'turn={TURN}',
                  'call=call-log-privacy', 'timeout=unlimited'):
        assert field in started


def test_a_background_command_is_logged_by_identity_and_ids_never_by_text(
        monkeypatch, tmp_path, caplog):
    delivered, queued = threading.Event(), []
    monkeypatch.setattr(background, '_queue_completion',
                        lambda **kwargs: (queued.append(kwargs), delivered.set()))
    content = _run(monkeypatch, tmp_path, {'command': COMMAND, 'background': True},
                   'call-log-privacy-bg')
    [command_id] = sorted(set(re.findall(r'bg_[0-9a-f]{12}', content)))
    assert delivered.wait(30)
    assert 'ok' in queued[0]['result']
    lines = _logged(caplog)
    assert [line for line in lines if MARKER in line] == []
    [started] = [line for line in lines
                 if line.startswith(f'[BackgroundCommand:{command_id}] started ')]
    for field in (_identity(COMMAND), f'root={tmp_path.name}', f'conv={CONV}', f'turn={TURN}',
                  'call=call-log-privacy-bg'):
        assert field in started
    # The runner executes the durable wrapper and names it by the command bound to it.
    runner = [line for line in lines if line.startswith('run_command: ')]
    assert runner and all(_identity(COMMAND) in line for line in runner)


def test_tool_argument_previews_show_a_command_only_by_size_and_digest():
    data = COMMAND.encode('utf-8')
    logged = tool_registry.log_arguments('run_command', {'command': COMMAND, 'timeout': 5})
    assert logged == {'command': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                      'timeout': 5}


# Every module that runs or schedules a shell command a model or owner typed.
COMMAND_OWNERS = (
    'lib/project_mod/run_command.py', 'lib/project_mod/tools.py',
    'lib/project_mod/foreground_command_records.py',
    'lib/tasks_pkg/handlers/project.py', 'lib/tasks_pkg/handlers/code_exec.py',
    'lib/tasks_pkg/handlers/_project_execution.py',
    'lib/tasks_pkg/handlers/_background_command.py', 'lib/tasks_pkg/background_commands.py',
    'lib/tasks_pkg/command_controls.py', 'lib/tasks_pkg/command_control_runtime.py',
    'lib/tasks_pkg/tool_hooks.py',
    'lib/scheduler/manager.py', 'lib/scheduler/_shared.py', 'lib/scheduler/proactive.py',
    'lib/scheduler/timer/_crud.py', 'lib/scheduler/timer/_poll.py',
)
_LOG_METHODS = frozenset({'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'})
_COMMAND_TEXT = re.compile(r'(?:^|_)(?:command|cmd|command_str|wrapped|predicate)$', re.IGNORECASE)
# Calls that render a command without its text.
_IDENTITY = frozenset({'command_log_fields', 'command_identity', 'len'})


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == 'audit_log'
    return (isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS
            and 'log' in ast.unparse(func.value).lower())


def _command_text(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
        if name in _IDENTITY:
            return []
        if (name == 'get' and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and _COMMAND_TEXT.search(node.args[0].value)):
            return [ast.unparse(node)]
    elif isinstance(node, ast.Name) and _COMMAND_TEXT.search(node.id):
        return [node.id]
    elif isinstance(node, ast.Attribute) and _COMMAND_TEXT.search(node.attr):
        return [ast.unparse(node)]
    elif (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
          and isinstance(node.slice.value, str) and _COMMAND_TEXT.search(node.slice.value)):
        return [ast.unparse(node)]
    return [found for child in ast.iter_child_nodes(node) for found in _command_text(child)]


def test_no_log_or_audit_call_of_a_command_owner_writes_command_text():
    violations = []
    for path in COMMAND_OWNERS:
        tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_log_call(node):
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                violations.extend(f'{path}:{node.lineno}: {text}'
                                  for argument in arguments for text in _command_text(argument))
    assert not violations, (
        'log or audit calls write command text; name the command with '
        'lib.project_mod.command_log instead:\n' + '\n'.join(violations))
