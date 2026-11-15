"""Reconcile prepared project writes that no live writer owns.

Entry points: recover_pending_writes and crashed_intents. Recovery never
restores user files. It records proven published writes, reclaims proven
private aborted stages, and preserves conflicting evidence with a visible
failure. A writer holds its path's file lock from intent preparation through
acknowledgement, so an intent whose path lock is free has no live writer and is
reconciled while that lock is held; a held lock means a live writer, whose
intent is left alone. Without intent evidence (has_pending_intents) there is
nothing to reconcile.
"""
import base64
import os

from lib.project_mod import journal_locks
from lib.project_mod.snapshot_content import anchored_parent
from lib.project_mod.write_intents import IntentError, classify, intent_store


def post_image(record):
    return {'kind': 'content', 'bytes': record['postBytes'],
            'digest': record['postDigest'], 'root': record['rootIdentity']}


def _recover_intent(base_path, store, record):
    """Reconcile one ownerless intent; caller holds its path lock. 1 if journaled."""
    from lib.project_mod import modifications as mods
    if record['basePath'] != os.path.abspath(base_path):
        raise IntentError('Write recovery root attribution mismatch')
    if record['state'] == 'aborted':
        store.discard_aborted(record)
        return 0
    session_dir = os.path.dirname(store.directory)
    mods._assert_journal_ready(session_dir)
    already_committed = mods._committed_change(session_dir, record['changeId']) is not None
    status = 'applied' if already_committed else classify(record)
    if status == 'unpublished':
        store.discard_aborted(store.abort(record))
        return 0
    if status != 'applied':
        raise IntentError('write-recovery-conflict: file or project changed; '
                          'original bytes preserved for explicit recovery')
    if not already_committed:
        # A previous replacement's parent flush may have failed. Confirm
        # the published name before confirming its journal transition.
        with anchored_parent(record['rootPath'], record['path'],
                             tuple(record['rootIdentity'])) as (parent, _):
            os.fsync(parent)
    original = (None if record['original'] is None else
                base64.b64decode(record['original'], validate=True))
    if mods._record_modification(record['basePath'], 'write_file', record['path'],
            original_content=original, conv_id=record['convId'], task_id=record['taskId'],
            change_id=record['changeId'], post_image=post_image(record)) is not True:
        raise IntentError('Prepared write journal recovery was not confirmed')
    store.acknowledge(record)
    return 1


def has_pending_intents(session_dir):
    """Whether prepared-write evidence exists, listed without any lock.

    Evidence that a crashed writer left behind is stable, so it is always seen.
    An intent created while the listing runs belongs to a writer that is still
    live and acknowledges it itself; missing it never skips a needed recovery.
    """
    try:
        with os.scandir(os.path.join(session_dir, 'write-intents-v1')) as entries:
            return any(entry.name.endswith('.json') for entry in entries)
    except (FileNotFoundError, NotADirectoryError):
        return False


def crashed_intents(session_dir, store, held=()):
    """Intents of paths in ``held`` (this caller's locks) or with no live writer."""
    crashed = []
    for record in store.read():
        if record['path'] in held:
            crashed.append(record)
            continue
        with journal_locks.try_path_lock(session_dir, record['path']) as ownerless:
            if ownerless and store.reread(record['changeId']) is not None:
                crashed.append(record)
    return crashed


def recover_pending_writes(base_path, *, held=()):
    """Reconcile every intent without a live writer; returns the journaled count.

    ``held`` names attributed paths whose file locks the caller already holds
    and has not yet prepared under (a writer before its own preparation): their
    intents are a crashed writer's and are reconciled under the caller's lock.
    Every other path is tried without waiting; a busy path is a live writer and
    is skipped, never delayed. A conflicting intent still fails visibly.
    """
    from lib.project_mod import modifications as mods
    session_dir = mods._get_session_dir(base_path, create=False)
    if not session_dir or not has_pending_intents(session_dir):
        return 0
    recovered = 0
    reconciled = False
    with intent_store(session_dir) as store:
        for record in store.read():
            if record['path'] in held:
                recovered += _recover_intent(base_path, store, record)
                reconciled = True
                continue
            with journal_locks.try_path_lock(session_dir, record['path']) as ownerless:
                if not ownerless:
                    continue
                current = store.reread(record['changeId'])
                if current is None:
                    continue
                recovered += _recover_intent(base_path, store, current)
                reconciled = True
    if reconciled:
        # Intent evidence marks a crashed or failed writer, whose packed
        # payloads may now be unreferenced.
        mods._sweep_crash_artifacts(session_dir, crash_evidence=True)
    return recovered
