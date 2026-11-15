"""Why a blocking ``poll`` wait must end now: one rule for every handle family.

Responsibility
--------------
``poll`` waits inside its tool call without a model request, for background
commands, swarm agents, tasks in other conversations and a Coordinator's
Workers alike. A wait ends early when the task is stopped, when this call is
cancelled (its execution deadline), or when input waits for the task's next
round boundary, which delivers it right after the tool result:

* a steer (a human, forwarded or channel message) or another background
  completion in this process's inbox (``lib.agent_inbox``);
* a steer another server process committed to this task's live Turn
  (``lib.tasks_pkg.committed_steers``): the inbox is process-local, so a steer
  accepted by the API process for a Turn executing in the execution process
  exists only as that durable record;
* a finished command of a root task's own run, for a task without a
  conversation;
* a project message submitted after the wait started
  (``lib.conversations.project_messages``); one already waiting was injected
  into the round that made this call.

The in-memory checks run on every call. The two durable reads (the live Turn's
steer records and the project mailbox) run on the first call and then at most
once per ``DURABLE_RECHECK_SECONDS``, so a wait may consult the predicate as
often as its own wait slice. Waits consult it only while time remains: a zero
wait reads state without paying those reads.

Entry points
------------
``WaitInterruption`` (the predicate and its ``waiting()`` description),
``PENDING_INPUT_MODES``, ``DURABLE_RECHECK_SECONDS``.

Dependencies
------------
agent_inbox, tasks_pkg.committed_steers, tasks_pkg.conversation_delivery,
tasks_pkg.background_commands and conversations.project_messages, imported
lazily.
"""
from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# Inbox lanes the next round boundary delivers: a steer (human, forwarded or
# channel input) and another background completion of this conversation.
PENDING_INPUT_MODES = ['user-steer', 'background-command']
# How often a wait re-reads durable evidence, the cadence the Worker wait has
# always rechecked its settlements at.
DURABLE_RECHECK_SECONDS = 5.0


class WaitInterruption:
    """Callable predicate: ``''`` while a wait may continue, else why it ends.

    ``'aborted'`` means the task was stopped, ``'cancelled'`` this call's own
    cancellation and ``'pending_input'`` input for the next round. Handles in
    ``polled`` end the wait with their own result, never as input.
    """

    def __init__(self, task: dict[str, Any], polled=(), runtime_context=None):
        from lib.tasks_pkg.conversation_delivery import (
            completion_conversation_id, completion_delivery)
        self._task = task
        self._polled = frozenset(polled)
        self._runtime_context = runtime_context
        self._conversation_id = completion_conversation_id(task)
        self._run_lane = not self._conversation_id and completion_delivery(task) == 'run'
        self._durable_due = None
        self._known_messages = None
        # Durable input seen by the latest read: committed steers and project
        # messages submitted after the wait started.
        self.committed: list[dict[str, Any]] = []
        self.arrived: list[dict[str, Any]] = []

    def __call__(self) -> str:
        task = self._task
        if task.get('aborted'):
            return 'aborted'
        context = self._runtime_context
        if context is not None and context.cancellation_requested:
            return 'cancelled'
        if self._local_input():
            return 'pending_input'
        now = time.monotonic()
        if self._durable_due is None or now >= self._durable_due:
            self._read_durable()
            self._durable_due = now + DURABLE_RECHECK_SECONDS
        return 'pending_input' if self.committed or self.arrived else ''

    def _local_input(self) -> bool:
        if self._conversation_id:
            from lib.agent_inbox import has_pending
            if has_pending(self._conversation_id, modes=PENDING_INPUT_MODES):
                return True
        if self._run_lane:
            from lib.tasks_pkg.background_commands import has_run_completions
            return has_run_completions(task_id=str(self._task.get('id') or ''),
                                       user_id=self._task.get('_userId'), excluding=self._polled)
        return False

    def _read_durable(self) -> None:
        if self._conversation_id:
            self.committed = _committed_steers(self._task)
        current = _incoming_project_messages(self._task)
        if current is None:
            return
        if self._known_messages is None:
            self._known_messages = frozenset(current)
        self.arrived = [message for key, message in current.items()
                        if key not in self._known_messages]

    def waiting(self) -> list[dict[str, Any]]:
        """What waits for the next round: kind, source and the bounded injection preview."""
        from lib.agent_inbox import pending_items
        from lib.tasks_pkg.committed_steers import INJECTION_PREVIEW_CHARS
        items = (pending_items(self._conversation_id, modes=PENDING_INPUT_MODES)
                 if self._conversation_id else [])
        blocks = {item.get('blockId') for item in items}
        items += [item for item in self.committed if item['blockId'] not in blocks]
        waiting = []
        for item in items:
            preview = str(item.get('value') or '')[:INJECTION_PREVIEW_CHARS]
            if item.get('mode') == 'background-command':
                waiting.append({'kind': 'background_command', 'source': item.get('command') or 'run_command',
                                'id': item.get('commandId'), 'preview': preview})
                continue
            author = item.get('steerAuthor')
            if isinstance(author, dict):
                origin = author.get('origin') if isinstance(author.get('origin'), dict) else {}
                channel = origin.get('channel') if isinstance(origin.get('channel'), dict) else None
                source = ('Global Router forward' if origin.get('forwardedFrom')
                          else f"{channel.get('channel') or 'external'} channel" if channel
                          else 'typed in this conversation')
                waiting.append({'kind': 'human', 'source': source, 'preview': preview})
                continue
            message = item.get('_user_msg') if isinstance(item.get('_user_msg'), dict) else {}
            origin = message.get('origin') if isinstance(message.get('origin'), dict) else {}
            initiator = origin.get('initiator') or message.get('_initiator') or 'unattributed'
            sender = origin.get('sourceConversationId')
            waiting.append({'kind': 'nonhuman', 'source': f'{initiator} {sender}' if sender else initiator,
                            'preview': preview})
        waiting += [{'kind': 'project_message', 'source': message['sender'], 'id': message['id'],
                     'preview': message['content'][:INJECTION_PREVIEW_CHARS]} for message in self.arrived]
        return waiting


def _committed_steers(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Steers another server process committed to this task's live Turn."""
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.committed_steers import committed_steers
    from lib.turn_lifecycle import LifecycleNotFound
    try:
        return committed_steers(task)
    except (StorageError, LifecycleNotFound):
        logger.warning('Blocking wait could not read its live Turn task=%s', task.get('id'), exc_info=True)
        return []


def _incoming_project_messages(task: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Incoming project messages still awaiting input by ID, or None without a mailbox."""
    config = task.get('config') or {}
    # Only a task bound to a project has a mailbox; the lookup would read the
    # conversation just to refuse.
    if not (config.get('canonicalProjectPath') or config.get('projectPath')):
        return None
    from lib.conversations.project_messages import ProjectMessageError, execute
    from lib.storage.errors import StorageError
    try:
        page = execute('get_project_messages', {'direction': 'incoming'}, task)
    except ProjectMessageError:
        return None
    except StorageError:
        logger.warning('Blocking wait could not read its project mailbox task=%s', task.get('id'), exc_info=True)
        return None
    # A new submission is always on the newest page.
    return {message['id']: message for message in page['messages'] if message['state'] == 'submitted'}


__all__ = ['DURABLE_RECHECK_SECONDS', 'PENDING_INPUT_MODES', 'WaitInterruption']
