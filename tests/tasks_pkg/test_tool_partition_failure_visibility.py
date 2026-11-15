"""Tool partition failures stay visible (no behavior change).

The write-tool and confirmation-tool sets decide write serialization and
which calls need approval. When building them fails, dispatch keeps working
with the sets it could build (fail open), but production drops DEBUG, so
the failure must leave a WARNING naming the gate and its cause.
"""
import logging

import pytest

import lib.tools.registry as registry
from lib.tasks_pkg.tool_dispatch import _flags

pytestmark = pytest.mark.unit


def _boom():
    raise RuntimeError('registry offline')


def _gate_warnings(caplog, gate):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING and gate in record.getMessage()]


def test_write_set_failure_keeps_the_base_sets_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(registry, 'all_specs', _boom)
    with caplog.at_level(logging.WARNING, logger=_flags.__name__):
        write, cacheable = _flags._registry_tool_flags()
    assert write == frozenset(_flags._WRITE_TOOLS_BASE)
    assert cacheable == frozenset(_flags._CACHEABLE_TOOLS_BASE)
    [message] = _gate_warnings(caplog, 'write-tool set')
    assert 'RuntimeError' in message and 'registry offline' in message


def test_confirmation_set_failure_keeps_the_empty_set_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(registry, 'all_specs', _boom)
    with caplog.at_level(logging.WARNING, logger=_flags.__name__):
        assert _flags._registry_confirmation_tools() == frozenset()
    [message] = _gate_warnings(caplog, 'confirmation-tool set')
    assert 'RuntimeError' in message and 'registry offline' in message


class _BrokenEnv:
    @property
    def confirmation_names(self):
        raise RuntimeError('env confirmation unreadable')

    @property
    def write_names(self):
        raise RuntimeError('env write set unreadable')

    idempotent_names = frozenset()


def test_a_minimal_task_env_declares_no_tools_and_warns_nothing(monkeypatch, caplog):
    from types import SimpleNamespace

    import lib.mcp as mcp

    monkeypatch.setattr(mcp, 'get_bridge', lambda _owner: SimpleNamespace(connected=False))
    task = {'id': 't', '_userId': 1, '_tool_env': SimpleNamespace(resolve=lambda name: None)}
    with caplog.at_level(logging.WARNING, logger=_flags.__name__):
        write, _idempotent = _flags._task_partitions(task)
        names = _flags._task_confirmation_tools(task)
    assert 'write_file' in write
    assert names == _flags._registry_confirmation_tools()
    assert _gate_warnings(caplog, 'tool set') == []


def test_task_confirmation_failure_keeps_registry_names_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(_flags, '_registry_confirmation_tools', lambda: frozenset({'write_file'}))
    with caplog.at_level(logging.WARNING, logger=_flags.__name__):
        names = _flags._task_confirmation_tools({'_tool_env': _BrokenEnv()})
    assert names == frozenset({'write_file'})
    [message] = _gate_warnings(caplog, 'confirmation-tool set')
    assert 'env confirmation unreadable' in message


def test_task_write_set_failures_warn_for_mcp_and_custom_tools(monkeypatch, caplog):
    import lib.mcp as mcp

    def broken_bridge(_owner):
        raise RuntimeError('bridge unreachable')

    monkeypatch.setattr(mcp, 'get_bridge', broken_bridge)
    task = {'id': 't', '_userId': 1, '_tool_env': _BrokenEnv()}
    with caplog.at_level(logging.WARNING, logger=_flags.__name__):
        write, _idempotent = _flags._task_partitions(task)
    assert 'write_file' in write
    messages = _gate_warnings(caplog, 'write-tool set')
    assert any('MCP' in message and 'bridge unreachable' in message for message in messages)
    assert any('env write set unreadable' in message for message in messages)
