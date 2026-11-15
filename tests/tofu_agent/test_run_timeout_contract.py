"""Run deadline contract for the storage-free runtime.

docs/DEVELOPER_RUNTIME.md: null means no bound, positive values bound, zero
refuses. The Coordinator's correction (2026-11-13): an omitted timeout reaches
the runtime as null end to end (CLI default, AgentRequest, result waits); 0
stays refused; HTTP /agent.run keeps its own 600 s default (tofu_agent/server.py).
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from tofu_agent import cli
from tofu_agent.models import AgentConfigurationError, AgentRequest, AgentTimeoutError
from tofu_agent.runtime import AgentExecution

pytestmark = pytest.mark.unit

MESSAGES = [{'role': 'user', 'content': 'x'}]


def test_an_omitted_cli_timeout_is_null(monkeypatch):
    monkeypatch.delenv('TOFU_AGENT_RUN_TIMEOUT', raising=False)
    assert cli._parser().parse_args(['run', '--task', 'x']).timeout_s is None
    assert cli._parser().parse_args(['run', '--task', 'x', '--timeout-s', 'none']).timeout_s is None


def test_a_positive_cli_or_environment_timeout_still_bounds(monkeypatch):
    assert cli._parser().parse_args(['run', '--task', 'x', '--timeout-s', '30']).timeout_s == 30.0
    monkeypatch.setenv('TOFU_AGENT_RUN_TIMEOUT', '45')
    assert cli._parser().parse_args(['run', '--task', 'x']).timeout_s == 45.0


@pytest.mark.parametrize('value', ['0', '-1', 'soon'])
def test_a_zero_negative_or_invalid_cli_timeout_is_refused(value, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli._parser().parse_args(['run', '--task', 'x', '--timeout-s', value])
    assert exit_info.value.code == 2


def test_the_request_accepts_null_and_refuses_zero_and_negative():
    assert AgentRequest(messages=MESSAGES, timeout_s=None).timeout_s is None
    assert AgentRequest(messages=MESSAGES).timeout_s == 600.0  # the Python API default is unchanged
    for value in (0, 0.0, -1):
        with pytest.raises(AgentConfigurationError, match='timeout_s must be positive'):
            AgentRequest(messages=MESSAGES, timeout_s=value)


class _Runtime:
    _native_execution = None


def _execution(timeout_s):
    request = AgentRequest(messages=MESSAGES, timeout_s=timeout_s)
    return AgentExecution(_Runtime(), {'id': 'task-1', 'status': 'running'}, request,
                          model='m', public_provider_id='p')


def _finish_later(execution, delay=0.2):
    def finish():
        execution._task['status'] = 'done'
        execution._terminal.set()
    timer = threading.Timer(delay, finish)
    timer.start()
    return timer


def test_a_null_deadline_waits_for_the_terminal_event():
    execution = _execution(None)
    timer = _finish_later(execution)
    try:
        assert execution.result() is not None
    finally:
        timer.cancel()


def test_a_null_deadline_waits_asynchronously_too():
    execution = _execution(None)
    timer = _finish_later(execution)
    try:
        assert asyncio.run(execution.result_async()) is not None
    finally:
        timer.cancel()


def test_an_explicit_zero_wait_is_refused_and_a_positive_one_bounds():
    execution = _execution(None)
    with pytest.raises(AgentConfigurationError, match='timeout_s must be positive'):
        execution.result(timeout_s=0)
    with pytest.raises(AgentTimeoutError):
        execution.result(timeout_s=0.05)
    with pytest.raises(AgentTimeoutError):
        asyncio.run(execution.result_async(timeout_s=0.05))
