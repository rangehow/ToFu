"""How a shell command appears in application, error and audit logs: by identity, never by text.

Responsibility: the one rendering of a command for log lines and audit records
(docs/LOGGING.md, "Commands are logged by identity"): its UTF-8 size, a short
SHA-256 digest, the workspace root it runs in and the conversation, Turn and
tool-call ids. The transcript keeps the text as the tool call's argument;
``debug/inspect_conversation.py --tools`` prints each call's id.

Entry points: ``command_identity(command)`` -> ``(bytes, digest)`` and
``command_log_fields(command, *, cwd, task, tool_call_id, runtime_context)``.
Dependencies: ``lib.project_mod.config`` (workspace root names) and
``lib.project_mod.tool_admission`` (a background wrapper's bound command).
"""
from __future__ import annotations

import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)

# Sixteen hex digits (64 bits) of the SHA-256 tell one conversation's commands
# apart in a log line; a tool-argument preview's full digest starts with them.
COMMAND_DIGEST_HEX_DIGITS = 16


def command_identity(command) -> tuple[int, str]:
    """``(UTF-8 bytes, short SHA-256)`` naming *command* without its text."""
    text = command if isinstance(command, str) else str(command or '')
    data = text.encode('utf-8', 'surrogatepass')
    return len(data), hashlib.sha256(data).hexdigest()[:COMMAND_DIGEST_HEX_DIGITS]


def _bound_command(command):
    """The command a recorded background wrapper runs, so its lines share the start line's digest."""
    from lib.project_mod.tool_admission import recorded_command_source
    try:
        return recorded_command_source(command) or command
    except ValueError:
        return command


def _root_name(cwd, root_key) -> str:
    """The registered workspace root holding *cwd* (the deepest one), or ''."""
    if not cwd:
        return ''
    from lib.project_mod.config import get_conv_roots, get_roots
    target = os.path.abspath(os.path.expanduser(str(cwd)))
    name, depth = '', -1
    for root_name, state in (get_conv_roots(root_key) or get_roots()).items():
        root = os.path.abspath(str((state or {}).get('path') or ''))
        if (root != os.sep and (target == root or target.startswith(root + os.sep))
                and len(root) > depth):
            name, depth = root_name, len(root)
    return name


def command_log_fields(command, *, cwd='', task=None, tool_call_id='', runtime_context=None) -> str:
    """``cmd_bytes=… cmd_sha256=… root=… conv=… turn=… call=…`` for one command.

    A blank field is omitted. ``command=None`` names a command by its ids alone,
    for a record that keeps no complete text. *runtime_context* supplies the
    task and tool call when they are not given.
    """
    if runtime_context is not None:
        task = task if task is not None else getattr(runtime_context, 'task', None)
        tool_call_id = tool_call_id or getattr(runtime_context, 'tool_call_id', '')
    task = task if task is not None else {}
    conv = str(task.get('convId') or '')
    fields = []
    if command is not None:
        size, digest = command_identity(_bound_command(command))
        fields += [f'cmd_bytes={size}', f'cmd_sha256={digest}']
    try:
        root = _root_name(cwd, conv or str(task.get('id') or ''))
    except Exception as exc:  # a log field never fails the command it describes
        logger.debug('[CommandLog] workspace root name unavailable: %s', type(exc).__name__)
        root = ''
    for key, value in (('root', root), ('conv', conv), ('turn', str(task.get('_turnId') or '')),
                       ('call', str(tool_call_id or ''))):
        if value:
            fields.append(f'{key}={value}')
    return ' '.join(fields)


__all__ = ['COMMAND_DIGEST_HEX_DIGITS', 'command_identity', 'command_log_fields']
