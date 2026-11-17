"""Executable contract: desktop tools address one owner device and describe it.

docs/modules/remote_execution.md, "Addressing one of several devices".
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest
from jsonschema import Draft202012Validator

from lib.desktop import bridge as db
from lib.desktop_wire import contract
from lib.desktop_tools import DESKTOP_TOOLS, DESKTOP_TOOL_NAMES

OWNER = '1'
WINDOWS = {'os': 'windows', 'release': '11', 'version': '10.0.22631', 'arch': 'AMD64',
           'shells': {'available': ['sh', 'powershell', 'cmd'], 'default': 'powershell'}}
LINUX = {'os': 'linux', 'release': '6.8.0', 'version': '#1 SMP', 'arch': 'x86_64',
         'shells': {'available': ['sh'], 'default': 'sh'}}


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setattr(db, '_last_poll', [0.0])

    def clear():
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()
            db._streams.clear()
        with db._async_waiters_lock:
            db._async_waiters.clear()
    clear()
    yield
    clear()


def _register(agent_id, name, *, owner=OWNER, system=None):
    frame = {'name': name, 'platform': 'win32' if system is WINDOWS else 'linux',
             'capabilities': {'exec': True}}
    if system is not None:
        frame['system'] = system
    db.register_agent(agent_id, frame, user_id=owner)


def _drain(agent_id, owner=OWNER):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(db.take_pending_commands_async(
            agent_id=agent_id, user_id=owner, timeout=0.2))
    finally:
        loop.close()


def _run_tool(fn_args, *, owner=OWNER):
    from lib.tasks_pkg.handlers.misc import _agents
    return _agents._run_desktop('desktop_run_command', fn_args, user_id=owner)


def _serve_one(agent_id, result, owner=OWNER):
    deadline = time.time() + 3
    while db.pending_commands_count() == 0 and time.time() < deadline:
        time.sleep(0.02)
    commands = _drain(agent_id, owner)
    for command in commands:
        db.resolve_results([{'id': command['id'], 'result': result, 'error': None}],
                           agent_id=agent_id, user_id=owner)
    return commands


@pytest.mark.unit
class TestToolSchemas:
    def test_every_desktop_tool_takes_an_optional_device(self):
        assert {tool['function']['name'] for tool in DESKTOP_TOOLS} == DESKTOP_TOOL_NAMES
        for tool in DESKTOP_TOOLS:
            parameters = tool['function']['parameters']
            assert parameters['properties']['device']['type'] == 'string'
            assert 'device' not in parameters.get('required', [])


@pytest.mark.unit
class TestAddressedTools:
    @pytest.mark.parametrize('selector', ['WORKSTATION', 'agent-win'])
    def test_addressed_call_reaches_the_named_device_while_two_are_online(self, selector):
        _register('agent-win', 'workstation', system=WINDOWS)
        _register('agent-linux', 'server', system=LINUX)
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(text=_run_tool(
            {'command': 'Get-ChildItem', 'device': selector})))
        worker.start()
        assert _drain('agent-linux') == []
        commands = _serve_one('agent-win', {'exit_code': 0, 'stdout': 'ok', 'stderr': ''})
        worker.join(timeout=3)
        assert [c['params'] for c in commands] == [{'command': 'Get-ChildItem'}]
        assert commands[0]['target_agent_id'] == 'agent-win'
        assert outcome['text'] == 'stdout: ok\nexit_code: 0'

    def test_unaddressed_call_lists_online_devices_with_os_and_default_shell(self):
        _register('agent-win', 'workstation', system=WINDOWS)
        _register('agent-linux', 'server', system=LINUX)
        _register('agent-foreign', 'foreign-box', owner='2', system=LINUX)
        text = _run_tool({'command': 'ls'})
        assert text.startswith('Desktop Agent Error:')
        assert 'device' in text
        assert 'workstation (agent agent-win): windows 11, AMD64, default shell powershell' in text
        assert 'server (agent agent-linux): linux 6.8.0, x86_64, default shell sh' in text
        assert 'foreign-box' not in text
        assert db.pending_commands_count() == 0

    def test_foreign_owner_device_name_never_resolves(self):
        _register('agent-mine', 'laptop', system=LINUX)
        _register('agent-foreign', 'mac', owner='2', system=LINUX)
        for selector in ('mac', 'MAC', 'agent-foreign'):
            text = _run_tool({'command': 'ls', 'device': selector})
            assert text.startswith('Desktop Agent Error:')
            assert 'laptop (agent agent-mine)' in text
            assert 'agent-foreign' not in text.replace(repr(selector), '')
        assert db.pending_commands_count() == 0
        assert _drain('agent-foreign', owner='2') == []

    def test_duplicate_names_are_ambiguous_and_name_the_agent_ids(self):
        _register('agent-1', 'box', system=LINUX)
        _register('agent-2', 'Box', system=LINUX)
        text = _run_tool({'command': 'ls', 'device': 'box'})
        assert 'agent-1' in text and 'agent-2' in text
        assert db.pending_commands_count() == 0

    def test_known_offline_device_is_refused_before_enqueue(self):
        _register('agent-win', 'workstation', system=WINDOWS)
        with db.command_queue_lock:
            db._agents['agent-win']['last_seen'] = time.time() - 3600
        text = _run_tool({'command': 'dir', 'device': 'workstation'})
        assert 'offline' in text
        assert db.pending_commands_count() == 0


@pytest.mark.unit
class TestSystemFacts:
    def test_frame_without_system_still_registers(self):
        db.register_agent('agent-old', {'name': 'old', 'platform': 'win32'}, user_id=OWNER)
        agent = db.list_agents(user_id=OWNER)[0]
        assert agent['online'] is True and agent['system'] is None
        assert 'old (agent agent-old): platform win32' in db.device_summary(agent)

    def test_valid_system_is_stored_and_exposed_owner_scoped(self):
        _register('agent-win', 'workstation', system=WINDOWS)
        assert db.list_agents(user_id=OWNER)[0]['system'] == WINDOWS
        assert db.list_agents(user_id='2') == []

    def test_heartbeat_without_system_keeps_the_known_facts(self):
        _register('agent-win', 'workstation', system=WINDOWS)
        db.register_agent('agent-win', {'name': 'workstation'}, user_id=OWNER)
        assert db.list_agents(user_id=OWNER)[0]['system'] == WINDOWS

    @pytest.mark.parametrize('system', [
        {'os': 'beos', 'shells': {'available': ['sh'], 'default': 'sh'}},
        {'os': 'linux', 'shells': {'available': ['sh'], 'default': 'cmd'}},
        {'os': 'linux', 'shells': {'available': ['zsh'], 'default': 'sh'}},
        {'os': 'linux', 'release': 'x' * 10_000, 'shells': {'available': ['sh'], 'default': 'sh'}},
        'linux',
    ])
    def test_malformed_system_is_dropped(self, system):
        db.register_agent('agent-x', {'name': 'x', 'system': system}, user_id=OWNER)
        assert db.list_agents(user_id=OWNER)[0]['system'] is None

    def test_unknown_system_fields_are_not_stored(self):
        _register('agent-new', 'new', system=dict(LINUX, gpu='x' * 100_000))
        assert db.list_agents(user_id=OWNER)[0]['system'] == LINUX

    def test_schema_declares_system_as_optional_registration_field(self):
        schema = contract()
        validator = Draft202012Validator({'$defs': schema['$defs'],
                                          '$ref': '#/$defs/AgentRegistration'})
        base = {'agent_id': 'a', 'protocol_version': 3, 'execution_slots': 1,
                'active_command_ids': []}
        validator.validate(base)
        validator.validate(dict(base, system=WINDOWS))
        assert not validator.is_valid(dict(base, system={'os': 'beos'}))


@pytest.mark.unit
class TestAgentFrame:
    @pytest.fixture
    def run(self):
        from lib.desktop_agent import _run
        return _run

    def _frame(self, run):
        return run._build_agent_frame('agent', {})

    def test_windows_frame_reports_powershell_default_and_detected_sh(self, run, monkeypatch):
        from lib.desktop_agent import _exec
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setattr(_exec, '_windows_posix_shell', lambda: 'C:/Git/bin/sh.exe')
        system = self._frame(run)['system']
        assert system['os'] == 'windows'
        assert system['shells'] == {'available': ['sh', 'powershell', 'cmd'],
                                    'default': 'powershell'}
        monkeypatch.setattr(_exec, '_windows_posix_shell', lambda: None)
        assert self._frame(run)['system']['shells']['available'] == ['powershell', 'cmd']

    def test_posix_frame_reports_sh_default_and_pwsh_when_installed(self, run, monkeypatch):
        import shutil
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/pwsh' if name == 'pwsh' else None)
        system = self._frame(run)['system']
        assert system['os'] == 'linux'
        assert system['shells'] == {'available': ['sh', 'powershell'], 'default': 'sh'}
        monkeypatch.setattr(shutil, 'which', lambda name: None)
        assert self._frame(run)['system']['shells']['available'] == ['sh']

    def test_frame_system_facts_validate_and_register(self, run):
        frame = self._frame(run)
        schema = contract()
        Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/SystemFacts'}).validate(
            frame['system'])
        db.register_agent('agent', frame, user_id=OWNER)
        assert db.list_agents(user_id=OWNER)[0]['system'] == frame['system']


@pytest.mark.unit
class TestModelContext:
    def _environment(self, tool_names, user_id=1):
        from lib.tasks_pkg.context_composer._models import ComposeRequest
        from lib.tasks_pkg.context_composer._providers import _desktop_devices_context
        return _desktop_devices_context(ComposeRequest(
            has_real_tools=True, user_id=user_id, tool_names=frozenset(tool_names)))

    def test_online_owner_devices_are_listed_when_desktop_tools_are_available(self):
        _register('agent-win', 'workstation', system=WINDOWS)
        _register('agent-linux', 'server', system=LINUX)
        _register('agent-foreign', 'foreign-box', owner='2', system=LINUX)
        text = self._environment({'desktop_run_command'})
        assert text.splitlines()[1].startswith(' - Desktop devices online')
        assert text.splitlines()[2:] == [
            '   server (agent agent-linux): linux 6.8.0, x86_64, default shell sh '
            '(available: sh); allowed: read, exec',
            '   workstation (agent agent-win): windows 11, AMD64, default shell powershell '
            '(available: sh, powershell, cmd); allowed: read, exec',
        ]
        assert text == self._environment({'desktop_run_command'})

    def test_no_line_without_desktop_tools_or_devices(self):
        _register('agent-win', 'workstation', system=WINDOWS)
        assert self._environment({'run_command'}) == ''
        assert self._environment({'desktop_run_command'}, user_id=2) == ''
