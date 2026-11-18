"""Agent-authored message attachments: the one admission boundary.

Responsibility: turn the ``attachments`` references a sender puts on a message
to another conversation (``delegate_project_work`` brief, ``control_conversation``
follow_up instruction, ``send_project_message``) into retained owner-scoped
media attachments (``lib.media_attachments``), the records a human's chat
attachment creates, so the recipient sees them through the same pipeline: a
Turn input carries them as ``attachments`` and model projection shows images,
document text and every other file's stored original; a mailbox message
reaches a running model with that same projection.

Authorization is decided here once, default deny: the sender attaches only
what it can already read.
- a path inside the sending task's verified roots (the read_files roots),
  read without following symlinks (``lib.artifacts.file_snapshot``); a lead
  (Global Router or Project Router, ``lead_scope``) also reads every registered
  project root of the owner (``lib.router.registered_projects``) and
  ``worker:<assignment_id>/<path>`` inside the registered checkout of a Worker
  its binding dispatched (``lead_worker_checkout``), docs/ROUTER.md R9 "Leads
  reach the owner directly";
- ``att_media_<id>``: one of the owner's own attachments (owner-scoped lookup);
- ``/api/images/<file>``: an uploaded image this conversation's messages show,
  including one the owner steered into the running Turn.
A file that carries a credential (``lib.conversations.attachment_secrets``) is refused too.
Anything else is refused before any record is written. No size or count limit
applies beyond the attachment platform's own. An attachment is context only:
an agent-authored message grants no human authority, attachments included.

Entry points: ``admit`` (references -> stored ``{attachmentId, name, mimeType}``
records), ``turn_attachments`` (records -> canonical Turn attachments),
``model_blocks`` (records -> the model projection a human attachment gets).
Dependencies: ``lib.media_attachments``, ``lib.attachments``,
``lib.tools.rich_response.task_roots`` and ``lib.tasks_pkg.workspace_delivery``
(lazily).
"""
from __future__ import annotations

import errno
import mimetypes
import os
import tempfile
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_MEDIA_PREFIX = 'att_media_'


class AttachmentRefused(PermissionError):
    """A reference the sender may not attach; nothing was sent."""

    code = 'attachment_refused'
    execution_started = False
    recovery = ('Nothing was sent. Attach only files you can read: a path inside your workspace roots '
                '(a lead: also any registered project root, or worker:<assignment_id>/<path> in its own '
                'Worker\u2019s checkout), an att_media_<id> attachment of the owner, or an /api/images/ image '
                'shown in this conversation, and never a file carrying a credential; then send again.')


def _record(ref: dict) -> dict:
    record = {'attachmentId': str(ref['attachmentId']), 'name': str(ref.get('name') or 'attachment')[:240]}
    if ref.get('mimeType'):
        record['mimeType'] = str(ref['mimeType'])[:255]
    return record


def _roots(task: dict) -> tuple[list[str], dict[str, str]]:
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots
    from lib.tools.rich_response import task_roots
    try:
        roots = task_roots(task, (task.get('config') or {}).get('projectPath'))
        labels = {name.lower(): state['path']
                  for name, state in ((ensure_execution_roots(task) or {}).get('roots') or {}).items()}
    except PermissionError:
        return [], {}
    return [root for root in roots if isinstance(root, str) and os.path.isabs(root)], labels


WORKER_PREFIX = 'worker:'


def lead_scope(task: dict) -> dict | None:
    """The coordinator scope of a lead's task (Global Router or Project Router), else None.

    Workers, the Watchdog and ordinary conversations are not leads. A lead's
    continuation Turn (a wake under its own dispatch grant) is still the lead:
    ``coordinator_scope`` reads the grant's Coordinator scope, None for a Worker's.
    """
    from lib.router.watchdog.binding import is_watchdog_scope
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    scope = coordinator_scope(task)
    return None if scope is None or is_watchdog_scope(scope) else scope


def lead_worker_checkout(task: dict, assignment_id: str) -> str:
    """The registered checkout of a Worker this lead's binding dispatched (``AttachmentRefused`` else).

    The checkout is the assignment's registered integration workspace, read by
    its work id; another lead's Worker, a discarded checkout or one never
    registered is refused.
    """
    from pathlib import Path
    from lib.project_dispatch.repository import ProjectDispatchError, ProjectDispatchRepository
    scope = lead_scope(task)
    if scope is None:
        raise AttachmentRefused('Only a project lead attaches files of its Workers\u2019 checkouts')
    repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    try:
        row = repository.read(assignment_id)
    except ProjectDispatchError as error:
        raise AttachmentRefused(f'{assignment_id} is not an assignment of this lead\u2019s project') from error
    if row['request']['coordinatorId'] != scope['conversation_id']:
        raise AttachmentRefused(f'Assignment {assignment_id} belongs to another lead')
    workspace = repository.get_workspace(str(Path(repository.project).expanduser().resolve()),
                                         repository.workspace_task_id(row), user_id=repository.owner)
    path = (workspace or {}).get('workspace_path')
    if not path or workspace.get('state') == 'discarded' or not os.path.isdir(path):
        raise AttachmentRefused(f'Assignment {assignment_id} has no registered checkout to read')
    return os.path.realpath(path)


def _registered_roots(task: dict) -> list[str]:
    """Every registered project root of the owner, for a lead's task only."""
    scope = lead_scope(task)
    if scope is None:
        return []
    from lib.router.registered_projects import registered_project_roots
    return registered_project_roots(scope['user_id'])


def _route(task: dict, reference: str, labels: dict[str, str]) -> str:
    """The read_files spelling of ``reference`` (file://, rootname:relative, checkout mapping)."""
    from lib.tasks_pkg.workspace_delivery import route_project_tool_args
    value = reference[len('file://'):] if reference.startswith('file://') else reference
    label, colon, rest = value.partition(':')
    if colon and label and '/' not in label and label.lower() in labels:
        return os.path.join(labels[label.lower()], rest.lstrip('/'))
    return route_project_tool_args(task, 'read_files', {'path': value})['path']


def _read_workspace_file(task: dict, reference: str) -> tuple[bytes, str]:
    """Bytes of a regular file inside the task's roots, read without following links.

    A lead also reads every registered project root of the owner, and
    ``worker:<assignment_id>/<path>`` inside a checkout of its own Worker.
    """
    from lib.artifacts.file_snapshot import read_project_snapshot
    if reference.startswith(WORKER_PREFIX):
        assignment_id, _, relative = reference[len(WORKER_PREFIX):].partition('/')
        if not assignment_id or not relative:
            raise AttachmentRefused(f'{reference}: name a Worker file as worker:<assignment_id>/<path>')
        roots, labels = [lead_worker_checkout(task, assignment_id)], {}
        path = os.path.join(roots[0], relative)
    else:
        roots, labels = _roots(task)
        roots = list(dict.fromkeys(roots + _registered_roots(task)))
        if not roots:
            raise AttachmentRefused(f'{reference}: this conversation has no readable workspace')
        path = _route(task, reference, labels)
    if not os.path.isabs(path):
        path = os.path.join(roots[0], path)
    # Roots are the owner's configured spellings; inside them nothing is followed.
    real_roots = [os.path.realpath(root) for root in roots]
    absolute = os.path.abspath(path)
    for root, real in zip(roots, real_roots):
        spelled = os.path.abspath(root)
        if os.path.commonpath([absolute, spelled]) == spelled:
            path = os.path.join(real, os.path.relpath(absolute, spelled))
            break
    try:
        snapshot = read_project_snapshot(path, real_roots, include_bytes=True, fingerprint=False)
    except PermissionError as error:
        raise AttachmentRefused(f'{reference} is outside the files you can read '
                                f'({", ".join(roots)})') from error
    except ValueError as error:
        raise AttachmentRefused(f'{reference}: {error}; attach a non-empty regular file '
                                'by its path inside your workspace') from error
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise AttachmentRefused(f'{reference} is, or passes through, a symbolic link; '
                                    'attach the real file') from error
        if error.errno == errno.ENOENT:
            raise AttachmentRefused(f'{reference} does not exist') from error
        raise AttachmentRefused(f'{reference} cannot be read ({error.strerror})') from error
    from lib.conversations.attachment_secrets import AttachmentSecretRefused, refuse_secrets
    try:
        refuse_secrets(snapshot.path, snapshot.content or b'')
    except AttachmentSecretRefused as refusal:
        raise AttachmentRefused(str(refusal)) from refusal
    return snapshot.content or b'', os.path.basename(snapshot.path)


def _owner_steered_images(task: dict) -> list:
    """Images of the owner's own messages steered into the Turn this task executes.

    A steer never enters ``task['messages']``; its images live on its record:
    this executor's delivered ones and those committed to the live Turn. The
    committed author decides it was the owner's input in this conversation;
    nonhuman steers and Router forwards carry none.
    """
    from lib.tasks_pkg.committed_steers import live_turn_steer_records
    from lib.turn_initiation import require_human_turn, steer_author_turn
    images: list = []
    for record in [*(task.get('_userSteerInjects') or ()), *live_turn_steer_records(task)]:
        author = record.get('author') if isinstance(record, dict) else None
        if not isinstance(author, dict):
            continue
        try:
            require_human_turn(steer_author_turn(author))
        except ValueError:
            continue
        for preview in record.get('previews') or ():
            if isinstance(preview, dict):
                images.extend(preview.get('images') or ())
    return images


def _conversation_images(task: dict) -> set[str]:
    from lib.attachments import canonical_image_ref
    shown: set[str] = set()
    images = [image for message in task.get('messages') or () if isinstance(message, dict)
              for image in message.get('images') or ()]
    for image in images + _owner_steered_images(task):
        values = image.values() if isinstance(image, dict) else (image,)
        for value in values:
            if isinstance(value, str) and canonical_image_ref(value):
                shown.add(canonical_image_ref(value))
    return shown


def _read_conversation_image(task: dict, reference: str) -> tuple[bytes, str]:
    from lib.attachments import canonical_image_ref, resolve_attachment
    canonical = canonical_image_ref(reference)
    filename = canonical[len('/api/images/'):]
    if not filename or os.path.basename(filename) != filename or filename in ('.', '..'):
        raise AttachmentRefused(f'{reference} is not an uploaded image reference')
    if canonical not in _conversation_images(task):
        raise AttachmentRefused(f'{reference} is not an image shown in this conversation')
    resolved = resolve_attachment(canonical)
    if not resolved or not resolved.get('raw'):
        raise AttachmentRefused(f'{reference} is no longer stored')
    return resolved['raw'], filename


def _ingest(data: bytes, name: str, *, owner: int, sender: str) -> dict:
    """Store bytes once as a retained attachment, exactly as received channel files are."""
    from lib.media_attachments import ingest_received_file
    if not data:
        raise AttachmentRefused(f'{name} is empty')
    with tempfile.TemporaryDirectory(prefix='tofu-lead-attachment-') as scratch:
        path = os.path.join(scratch, 'original')
        with open(path, 'wb') as stream:
            stream.write(data)
        return ingest_received_file(
            path, name, mimetypes.guess_type(name)[0] or '', user_id=owner,
            origin={'origin': 'agent_message', 'senderConversation': sender})


def admit(task: dict, owner: int, references: Any) -> list[dict]:
    """Admit every reference or refuse the whole message (``AttachmentRefused``).

    Returns stored records in reference order; identical files collapse to one.
    Idempotent: the same bytes resolve to the same retained attachment.
    """
    if not references:
        return []
    if not isinstance(references, list) or not all(isinstance(item, str) and item.strip()
                                                    for item in references):
        raise AttachmentRefused('attachments must be a list of non-empty file references')
    from lib.attachments import canonical_image_ref
    from lib.media_attachments import resolve_client_refs
    sender = str(task.get('convId') or '')
    records: list[dict] = []
    for reference in (item.strip() for item in references):
        if reference.startswith(_MEDIA_PREFIX):
            attachment_id = reference[len(_MEDIA_PREFIX):]
            found = resolve_client_refs([{'attachmentId': attachment_id}], user_id=owner, retain=True)
            if not found:
                raise AttachmentRefused(f'{reference} is not one of the owner\u2019s attachments')
            ref = found[0]
        elif canonical_image_ref(reference):
            data, name = _read_conversation_image(task, reference)
            ref = _ingest(data, name, owner=owner, sender=sender)
        else:
            data, name = _read_workspace_file(task, reference)
            ref = _ingest(data, name, owner=owner, sender=sender)
        record = _record(ref)
        if all(record['attachmentId'] != kept['attachmentId'] for kept in records):
            records.append(record)
    logger.info('[LeadAttachments] admitted %d attachment(s) conv=%s', len(records), sender)
    return records


def turn_attachments(records: Any, *, owner: int) -> list[dict]:
    """Canonical ``TurnMediaAttachment`` refs for a recipient Turn input."""
    from lib.media_attachments import resolve_client_refs
    return resolve_client_refs(list(records or ()), user_id=owner, retain=True)


def model_blocks(records: Any, *, owner: int, query: str, model: str) -> list[dict]:
    """The content blocks a human attachment of these files projects for ``model``."""
    if not records:
        return []
    from lib.media_attachments import project_for_model
    return project_for_model(list(records), user_id=owner, query=query, model=model)['blocks']


__all__ = ['AttachmentRefused', 'admit', 'model_blocks', 'turn_attachments']
