"""Every coordination role can look up what Tofu knows about the owner; none can write memory.

Design record "Owner understanding", Decision 4: one read-only owner-memory
tool (search_memories, mode=owner and memory_id) for the Global Router,
Project Coordinators and the Watchdog, granted through the one Coordinator
ceiling boundary; the memory write tools stay outside every one of them.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.router.home import router_home_location
from lib.router.policy import ROUTER_TOOLS
from lib.router.watchdog.binding import watchdog_location
from lib.router.watchdog.policy import WATCHDOG_TOOLS
from lib.tasks_pkg.coordinator_policy import (
    COORDINATOR_TOOLS, coordinator_call_rejection, coordinator_tool_ceiling, filter_coordinator_tools,
)

pytestmark = pytest.mark.unit
OWNER = 7
MEMORY_WRITES = ('create_memory', 'update_memory', 'delete_memory', 'merge_memories')


@pytest.fixture
def roles(tmp_path, monkeypatch):
    from lib.conversations import project_coordination
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    state = {'projectPath': '/project', 'conversationId': 'coordinator',
             'availability': 'available', 'revision': 1}
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda path: deepcopy(state)))

    def task(path, conversation):
        return {'id': 'task-' + conversation, 'convId': conversation, '_userId': OWNER, 'config': {},
                '_coordinatorDispatchScope': {'project_path': path, 'binding_revision': 1,
                                              'conversation_id': conversation, 'user_id': OWNER}}
    return {'router': task(router_home_location(OWNER), 'router'),
            'watchdog': task(watchdog_location(OWNER), 'watchdog'),
            'coordinator': task('/project', 'coordinator')}


def _schema(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def test_every_coordination_ceiling_holds_the_lookup_and_no_memory_write(roles):
    expected = {'router': ROUTER_TOOLS, 'watchdog': WATCHDOG_TOOLS, 'coordinator': COORDINATOR_TOOLS}
    for role, task in roles.items():
        ceiling = coordinator_tool_ceiling(task['_coordinatorDispatchScope'])
        assert ceiling == expected[role]
        assert 'search_memories' in ceiling, role
        assert not ceiling & set(MEMORY_WRITES), role


@pytest.mark.parametrize('role', ['router', 'watchdog', 'coordinator'])
def test_each_role_may_call_the_lookup_but_never_a_memory_write(roles, role):
    task = roles[role]
    assert coordinator_call_rejection(task, 'search_memories', {'mode': 'owner', 'query': 'q'}) is None
    assert coordinator_call_rejection(task, 'search_memories', {'memory_id': 'ctx_1'}) is None
    for name in MEMORY_WRITES:
        assert coordinator_call_rejection(task, name, {'memory_id': 'm'}), name
    smuggled = {'calls': [{'name': 'search_memories', 'arguments': {'query': 'q'}},
                          {'name': 'update_memory', 'arguments': {'memory_id': 'm'}}]}
    assert coordinator_call_rejection(task, 'execute_tools', smuggled)
    batch = {'calls': [{'name': 'search_memories', 'arguments': {'mode': 'owner', 'query': 'q'}}]}
    assert coordinator_call_rejection(task, 'execute_tools', batch) is None


def test_the_catalog_projection_keeps_the_lookup_and_drops_the_writes(roles):
    catalog = [_schema(name) for name in ('search_memories', *MEMORY_WRITES, 'get_conversation')]
    kept = [tool['function']['name'] for tool in filter_coordinator_tools(roles['coordinator'], catalog)]
    assert kept == ['search_memories', 'get_conversation']


@pytest.mark.parametrize('role', ['router', 'watchdog', 'coordinator'])
def test_the_assembled_role_surface_carries_the_lookup_and_no_memory_write(roles, role):
    """The real registry builds the memory family; the role surface keeps only the lookup."""
    from lib.tools.registry._spec import ToolContext, assemble_tool_list
    from lib.tasks_pkg.coordinator_policy import prepare_coordinator_tool_surface

    task = roles[role]
    path = task['_coordinatorDispatchScope']['project_path']
    cfg = task['config']
    cfg.update(projectPath=path, memoryEnabled=True, mcpEnabled=False)
    tools, _real = assemble_tool_list(ToolContext(
        cfg=cfg, task_id=task['id'], conv_id=task['convId'], project_path=path,
        project_enabled=True, search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=True, browser_enabled=False, desktop_enabled=False,
        image_gen_enabled=False, human_guidance_enabled=True, scheduler_enabled=False,
        messages=[], owner_user_id=OWNER))
    cfg['_executableToolCatalog'] = list(tools)
    surface = {tool['function']['name']
               for tool in prepare_coordinator_tool_surface(task, cfg, tools, path)}
    assert 'search_memories' in surface
    assert not surface & set(MEMORY_WRITES)
