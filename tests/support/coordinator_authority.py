"""Ordinary Coordinator binding and Attempt preparation on an owned authority.

The caller owns authority lifetime, client routing, settlement and fault injection.
These helpers register lifecycle state only; they never run an executor.
"""
from types import SimpleNamespace

from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.conversations.repository import ConversationRepository
from lib import turn_lifecycle as turns


def create_bound_coordinator(factory, owner, project, *, folder_id, command_id):
    conversations = ConversationRepository(factory)
    bindings = ProjectCoordinationRepository(owner, client=factory, conversations=conversations)
    binding = bindings.create(project, folder_id=folder_id, expected_revision=0,
                              command_id=command_id)
    return SimpleNamespace(conversations=conversations, bindings=bindings, binding=binding)


def start_coordinator_turn(conversation_id, owner, *, command_id, task_id, content):
    pair = turns.create_turn_pair(conversation_id, command_id=command_id, config={},
                                 user_id=owner, input_projection={'content': content})
    attempt = pair['attempt']['attemptId']
    turns.bind_task(attempt, task_id, user_id=owner)
    turns.mark_task_started(attempt, task_id, user_id=owner)
    return pair
