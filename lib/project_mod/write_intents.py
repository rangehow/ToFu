"""Complete write-ahead evidence for project file replacement.

Entry point: intent_store(session_dir). The caller owns authorization and
must hold the intent path's file lock (``journal_locks``) from prepare through
file publication, journal commit and acknowledgement; intents of other paths
belong to their own live or crashed writers.
Classification reads only: it never restores, deletes or overwrites user files.
This module uses the journal durability primitives and descriptor-anchored path
walker; it does not register a startup hook or implicitly activate recovery.
"""
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
import base64
import binascii
import hashlib
import json
import os
import stat
import sys
import uuid

from lib.project_mod.snapshot_content import anchored_parent, file_fingerprint


class IntentError(OSError):
    """Recovery evidence is unavailable; publication must not proceed."""


@lru_cache(maxsize=1)
def contract():
    return json.loads((Path(__file__).resolve().parents[2]
                       / 'contracts/project_write_intent_v1.schema.json').read_text())


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(contract())


@lru_cache(maxsize=1)
def recovery_file_bytes():
    """Compatibility extent for callers; no launch-derived admission quota."""
    return sys.maxsize


def _limit(name):
    if name in ('originalBytes', 'postImageBytes', 'recordBytes'):
        return sys.maxsize
    return contract()['x-resource-limits'][name]


def _identity(metadata):
    return [metadata.st_dev, metadata.st_ino]


def validate_relative_path(value):

    """Validate a destination against the same contract used by prepared writes."""
    if (not isinstance(value, str) or not value or os.path.isabs(value)
            or '\\' in value or any(part in ('', '.', '..') for part in value.split('/'))):
        raise IntentError('Invalid write intent relative path')
    return value


def _validate(record):
    if next(_validator().iter_errors(record), None):
        raise IntentError('Invalid write intent envelope')
    validate_relative_path(record['path'])
    if not os.path.isabs(record['basePath']) or not os.path.isabs(record['rootPath']):
        raise IntentError('Write intent root must be absolute')
    original = record['original']
    if original is not None:
        try:
            base64.b64decode(original, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise IntentError('Invalid original file encoding') from exc
    return record


def _read_fingerprint(metadata):
    # ctime is excluded: DolphinFS FUSE reports a just-renamed file's ctime late,
    # so fstat and a later stat of the same unchanged inode disagree under
    # concurrent writes. Every caller verifies the bytes it read (digest, exact
    # content or a validated envelope) and the inode, which is what proves the
    # evidence; a metadata-only change cannot alter either.
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _read_regular(parent, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise IntentError('Recovery evidence is not a regular file')
        data = handle.read()
        after = os.fstat(handle.fileno())
    if len(data) != before.st_size or _read_fingerprint(before) != _read_fingerprint(after):
        raise IntentError('Recovery evidence changed while reading')
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _read_fingerprint(current) != _read_fingerprint(before):
        raise IntentError('Recovery path changed while reading')
    return data, before


def _digest(data):
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def classify(record):
    """Return applied/unpublished/conflict, with no filesystem mutation.

    An extant matching stage plus unchanged original proves non-publication.
    Missing stage alone proves nothing. Content equality without staged inode
    identity also proves nothing; preserve the intent for explicit resolution.
    """
    _validate(record)
    root = tuple(record['rootIdentity'])
    if os.path.realpath(record['basePath']) != record['rootPath']:
        return 'conflict'
    try:
        with anchored_parent(record['rootPath'], record['path'], root) as (parent, name):
            try:
                current, metadata = _read_regular(parent, name)
            except FileNotFoundError:
                current, metadata = None, None
            expected_original = (None if record['original'] is None
                                 else base64.b64decode(record['original'], validate=True))
            if (metadata is not None and _identity(metadata) == record['stagedIdentity']
                    and len(current) == record['postBytes'] and _digest(current) == record['postDigest']):
                result = 'applied'
            elif current == expected_original:
                staged, stage_meta = _read_regular(parent, record['stageName'])
                result = ('unpublished' if _identity(stage_meta) == record['stagedIdentity']
                          and len(staged) == record['postBytes'] and _digest(staged) == record['postDigest']
                          else 'conflict')
            else:
                result = 'conflict'
            # Re-resolve the path: a detached parent handle must not become proof
            # about a replacement directory at the same user-visible location.
            with anchored_parent(record['rootPath'], record['path'], root) as (again, _):
                if _identity(os.fstat(again)) != _identity(os.fstat(parent)):
                    return 'conflict'
            return result
    except (OSError, ValueError, NotImplementedError):
        return 'conflict'


class IntentStore:
    """Operations require the enclosing intent_store context to remain open."""

    def __init__(self, directory, descriptor):
        self.directory = directory
        self.descriptor = descriptor

    def read(self):
        # scandir through a FRESH directory descriptor on every call: some
        # filesystems (notably DolphinFS FUSE) cache readdir results on an
        # open dir fd, so records written by prepare() inside this same locked
        # session would be invisible to the cached descriptor — freshly
        # recorded crash evidence would be unrecoverable exactly when needed.
        records = []
        descriptor = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if not entry.name.endswith('.json'):
                        continue
                    record = self._load(descriptor, entry.name)
                    if record is not None:
                        records.append(record)
        finally:
            os.close(descriptor)
        return records

    def reread(self, change_id):
        """The current intent of ``change_id``; None once it was acknowledged."""
        descriptor = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return self._load(descriptor, change_id + '.json')
        finally:
            os.close(descriptor)

    @staticmethod
    def _load(descriptor, name):
        # Other paths' writers hold no lock this reader shares: while listing,
        # an owner may acknowledge (unlink) or abort (atomic replace) its own
        # intent. Intents are never rewritten in place, so a change seen on the
        # same inode is corruption; a vanished name is skipped and a replaced
        # one is read again as the owner's new intent.
        while True:
            try:
                listed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                data, _ = _read_regular(descriptor, name)
                break
            except FileNotFoundError:
                return None
            except IntentError:
                try:
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                if _identity(current) == _identity(listed):
                    raise
        try:
            record = _validate(json.loads(data))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise IntentError('Unreadable write intent') from exc
        if name != record['changeId'] + '.json':
            raise IntentError('Write intent identity mismatch')
        return record

    def prepare(self, *, base_path, path, original, post_content, stage_name,
                conv_id=None, task_id=None, on_intent=None, change_id=None):
        """Persist pre-image and stage proof before file publication.

        The caller must finish and fsync the staged file before calling prepare.
        This method flushes its directory entry before publishing the intent.
        ``change_id`` lets a caller commit a reference to this change before
        journal admission; omitted, a fresh one is allocated.
        """
        from lib.project_mod.modifications import _atomic_json_write
        validate_relative_path(path)
        if original is not None and not isinstance(original, bytes):
            raise IntentError('Original must be exact bytes or explicit absence')
        self.read()  # Existing evidence must still validate before publication.
        base_path = os.path.abspath(base_path)
        root_path = os.path.realpath(base_path)
        root = os.stat(root_path, follow_symlinks=False)
        with anchored_parent(root_path, path, tuple(_identity(root))) as (parent, _):
            # Validate stage name before any lookup, even though record validation
            # below also enforces its canonical random-name shape.
            import re
            if not isinstance(stage_name, str) or not re.fullmatch(r'\.tofu_atomic_[0-9a-f]{32}', stage_name):
                raise IntentError('Invalid write stage name')
            staged, metadata = _read_regular(parent, stage_name)
            if staged != post_content:
                raise IntentError('Staged content differs from requested write')
            os.fsync(parent)  # Make the stage name durable before its intent.
        record = _validate({
            'version': 1, 'state': 'prepared', 'changeId': change_id or uuid.uuid4().hex, 'basePath': base_path,
            'path': path, 'rootPath': root_path, 'stageName': stage_name, 'rootIdentity': _identity(root),
            'stagedIdentity': _identity(metadata),
            'original': None if original is None else base64.b64encode(original).decode('ascii'),
            'postBytes': len(post_content), 'postDigest': _digest(post_content),
            'convId': conv_id, 'taskId': task_id,
        })
        if classify(record) != 'unpublished':
            raise IntentError('Original file changed before write preparation')
        if on_intent is not None:
            on_intent(dict(record))  # Identity only; durability is confirmed on return.
        _atomic_json_write(os.path.join(self.directory, record['changeId'] + '.json'), record)
        return record

    def abort(self, record):
        """Caller proved this operation never published; durably record that fact."""
        from lib.project_mod.modifications import _atomic_json_write
        aborted = _validate({**record, 'state': 'aborted'})
        _atomic_json_write(os.path.join(self.directory, record['changeId'] + '.json'), aborted)
        return aborted

    def discard_aborted(self, record):
        """Reclaim only the identified private stage; never touch the target."""
        _validate(record)
        if record['state'] != 'aborted':
            raise IntentError('Write intent has not been aborted')
        with anchored_parent(record['rootPath'], record['path'], tuple(record['rootIdentity'])) as (parent, _):
            try:
                data, metadata = _read_regular(parent, record['stageName'])
            except FileNotFoundError:
                pass
            else:
                if (_identity(metadata) != record['stagedIdentity']
                        or len(data) != record['postBytes'] or _digest(data) != record['postDigest']):
                    raise IntentError('Aborted write stage changed; preserving evidence')
                os.unlink(record['stageName'], dir_fd=parent)
            os.fsync(parent)
        self.acknowledge(record)

    def acknowledge(self, record):
        """Caller confirms durable journal commit or proven non-publication first."""
        _validate(record)
        os.unlink(record['changeId'] + '.json', dir_fd=self.descriptor)
        os.fsync(self.descriptor)


@contextmanager
def intent_store(session_dir):
    """Open the session's intent directory; takes no lock (see module header)."""
    from lib.project_mod.modifications import _ensure_durable_session_directory
    directory = os.path.join(session_dir, 'write-intents-v1')
    _ensure_durable_session_directory(directory)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield IntentStore(directory, parent)
    finally:
        os.close(parent)
