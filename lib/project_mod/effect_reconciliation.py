"""Bridge one tracked local write occurrence to existing file-publication evidence.

Entry points: bind_planned_write (before the file lock), confirm_prepared_write
(under the file lock, before replace), inspect_prepared_write (read).
The undo index and write-intent journal remain the only file authorities. Queries
never recover, delete, replay, or overwrite a file. They prove publication, not
current content, tool completion, downstream effects, or task acceptance.
"""
import os

from lib.project_mod import journal_locks, modifications
from lib.project_mod.write_intents import IntentStore, classify


def reference_for(record):
    return {'kind': 'project_file_write', **{key: record[key] for key in (
        'changeId', 'basePath', 'path', 'taskId', 'rootIdentity', 'postDigest', 'postBytes')}}


def _bound_context():
    from lib.project_mod.tool_admission import cancellation
    control = cancellation()
    context = control.runtime_context if control is not None else None
    if context is None or control.tool_name != 'write_file':
        return None
    return context


def _refuse_if_cancelled(context):
    from lib.tasks_pkg.tool_runtime.context import ToolCancelled
    if context.cancellation_requested:
        raise ToolCancelled('Cancelled before file publication')


def bind_planned_write(*, change_id, base_path, path, post_content, conv_id, task_id):
    """Commit the reconciliation reference before taking the file's journal lock.

    The durable ACK waits on storage; held under a journal lock it serialized
    writers behind storage latency. Every
    reference field is known before the intent exists; confirm_prepared_write
    proves the prepared intent matches before any replacement. Returns the
    committed reference, or None when this occurrence binds nothing.
    """
    from lib.project_mod.write_intents import _digest, _identity
    context = _bound_context()
    if context is None:
        return None
    from lib.project_mod.tool_admission import journal_conv_id
    from lib.tasks_pkg.executor._effects import bind_tool_effect_reconciliation
    if journal_conv_id(context.task.get('id')) != conv_id or context.task.get('id') != task_id:
        raise ValueError('Prepared write attribution differs from its tool occurrence')
    base_path = os.path.abspath(base_path)
    root = os.stat(os.path.realpath(base_path), follow_symlinks=False)
    reference = reference_for({
        'changeId': change_id, 'basePath': base_path, 'path': path, 'taskId': task_id,
        'rootIdentity': _identity(root), 'postDigest': _digest(post_content),
        'postBytes': len(post_content)})
    bind_tool_effect_reconciliation(context, reference)
    _refuse_if_cancelled(context)
    return reference


def confirm_prepared_write(reference, record):
    """Refuse publication unless the durable intent is the referenced change."""
    if reference is None:
        return
    if reference_for(record) != reference:
        from lib.project_mod.write_intents import IntentError
        raise IntentError('Prepared write differs from its committed reconciliation reference')
    context = _bound_context()
    if context is not None:
        _refuse_if_cancelled(context)


def _matches(record, reference, conversation_id, *, committed=False):
    if record.get('convId') != conversation_id:
        return False
    if any(record.get(key) != reference[key] for key in ('changeId', 'basePath', 'path', 'taskId')):
        return False
    if committed:
        return record.get('type') == 'write_file' and record.get('postImage') == {
            'kind': 'content', 'bytes': reference['postBytes'],
            'digest': reference['postDigest'], 'root': reference['rootIdentity'],
        }
    return all(record.get(key) == reference[key] for key in ('rootIdentity', 'postDigest', 'postBytes'))


def inspect_prepared_write(reference, *, conversation_id):
    """Caller first resolves the exact occurrence through an owner-scoped Turn.

    A committed undo receipt proves historical publication even if a user later
    edits or undoes the file. Prepared-only evidence must still pass the existing
    root/inode/content classifier. Missing evidence is never proof of no effect.
    The path's file lock is tried without waiting and held while reading: held
    elsewhere means that file's writer is live (``writer_active``).
    """
    session_dir = modifications._get_session_dir(reference['basePath'], create=False)
    if not session_dir or not os.path.isdir(session_dir):
        return 'unknown', 'missing_evidence'
    try:
        with journal_locks.try_path_lock(session_dir, reference['path']) as ownerless:
            if not ownerless:
                return 'unknown', 'writer_active'
            rows = [row for row in modifications._load_from_disk(session_dir)
                    if row.get('changeId') == reference['changeId']]
            if rows:
                if len(rows) == 1 and _matches(rows[0], reference, conversation_id, committed=True):
                    return 'file_published', 'undo_committed'
                return 'unknown', 'evidence_conflict'
            directory = os.path.join(session_dir, 'write-intents-v1')
            if not os.path.isdir(directory):
                return 'unknown', 'missing_evidence'
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                records = [record for record in IntentStore(directory, descriptor).read()
                           if record['changeId'] == reference['changeId']]
            finally:
                os.close(descriptor)
            if not records:
                return 'unknown', 'missing_evidence'
            record = records[0]
            if len(records) != 1 or not _matches(record, reference, conversation_id):
                return 'unknown', 'evidence_conflict'
            status = classify(record)
            if status == 'applied':
                return 'file_published', 'prepared_file_applied'
            if status == 'unpublished':
                return 'file_not_published', 'prepared_file_unpublished'
            return 'unknown', 'evidence_conflict'
    except (OSError, ValueError):
        return 'unknown', 'evidence_unavailable'
