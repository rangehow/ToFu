"""Lead tool schemas state each shared rule once.

The stage-2 lead audit (2026-11-18) found the same attachments paragraph sent
three times on a Project Router's wire, and the owner-delegation trigger rules
restated in six tool descriptions beside the trigger fields' own documentation;
three of the restatements had already drifted (they omitted under_delegation).
The full attachments text lives in send_project_message, present wherever
delegate_project_work or control_conversation is; the trigger rules live in the
ownerDelegationAuthority fields (contracts/router_v1.schema.json).
"""
import json
import re

import pytest

from tests.router.test_router_tool_name_budget import _assemble as _assemble_router
from tests.router.test_router_tool_name_budget import router as router  # noqa: F401
from tests.tasks_pkg.test_coordinator_policy import coordinator as coordinator  # noqa: F401
from tests.tasks_pkg.test_coordinator_tool_name_budget import (  # noqa: F401
    homes, project_router_with_command_chat, router_with_channels)
from tests.tasks_pkg.test_coordinator_tool_surface import assemble
__all__ = ['project_router_with_command_chat', 'router_with_channels']

pytestmark = pytest.mark.unit

ATTACHMENT_RULE = 'through the shared attachment pipeline'
DELEGATION_TOOLS = ('restart_server', 'owner_settings', 'repair_project', 'router_project',
                    'control_conversation', 'schedule_manage')
TRIGGER_FIELDS = re.compile(r'\b(source_turn_id|source_steer_id|instruction_id|event_id)\b')


def _surfaces(project_router_with_command_chat, router_with_channels):
    task, _ = project_router_with_command_chat
    lead = assemble(task)
    router_task, home = router_with_channels
    return {'project_router': lead, 'global_router': _assemble_router(router_task, home)}


def _by_name(tools):
    return {tool['function']['name']: tool for tool in tools}


def test_the_attachments_rule_is_sent_once(project_router_with_command_chat, router_with_channels):
    for role, tools in _surfaces(project_router_with_command_chat, router_with_channels).items():
        text = json.dumps(tools, ensure_ascii=False)
        assert text.count(ATTACHMENT_RULE) == 1, role
        names = _by_name(tools)
        holder = names['send_project_message']['function']['parameters']['properties']['attachments']
        assert ATTACHMENT_RULE in holder['description'], role
        for other in {'delegate_project_work', 'control_conversation'} & set(names):
            pointer = names[other]['function']['parameters']['properties']['attachments']['description']
            assert 'send_project_message' in pointer, (role, other)


def test_delegation_tools_leave_the_trigger_rules_to_the_trigger_fields(
        project_router_with_command_chat, router_with_channels):
    tools = _by_name(_surfaces(project_router_with_command_chat, router_with_channels)['global_router'])
    for name in DELEGATION_TOOLS:
        description = tools[name]['function']['description']
        assert not TRIGGER_FIELDS.search(description), (name, TRIGGER_FIELDS.findall(description))
        parameters = json.dumps(tools[name]['function']['parameters'])
        assert 'under_delegation' in parameters, name


def test_session_model_schema_preserves_explicit_authority_as_optional(
        project_router_with_command_chat, router_with_channels):
    tools = _by_name(_surfaces(project_router_with_command_chat, router_with_channels)['global_router'])
    parameters = tools['control_conversation']['function']['parameters']
    assert 'set_model' in parameters['properties']['action']['enum']
    assert 'owner_delegation' not in parameters['required']
    assert 'under_delegation' in parameters['properties']['owner_delegation']['properties']
    assert {'conversation_id', 'turn_id', 'attempt_id', 'idempotency_key'} <= set(parameters['required'])
