"""Local, deterministic experiment ZIP seals; no execution or authorization.

create_experiment_source returns the job_sources-compatible {uri, sha256} source
reference. verify_experiment_source takes that reference and the same expected
inputs and returns its verified manifest. Both require an SDK Actor, exact
provided code bytes and their audited SHA256, a JSON-object scientific plan,
provider/cell_name/arm strings, integer seed and positive integer attempt.

Only experiment.py, canonical plan.json and manifest.json are included. Sources
are create-only .experiment-sources/<ZIP SHA256>.zip files under the explicit
actor.project_root. Verification pins a no-follow file descriptor and compares
all bytes to the deterministic archive, a stricter check than safe extraction.
The resulting reference can be consumed by job_sources.extract_job_source.
Seals detect later filesystem tampering; they do not prevent an owner with
filesystem write access from deleting files or grant permission to run code.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import secrets
import zipfile


_DIRECTORY = '.experiment-sources'


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _bundle(actor, code, code_sha256, plan, provider, cell_name, arm, seed, attempt):
    if not isinstance(code, bytes) or hashlib.sha256(code).hexdigest() != code_sha256:
        raise ValueError('Supplied code does not match audited code_sha256')
    if not isinstance(plan, dict):
        raise ValueError('Scientific plan must be a JSON object')
    if any(not isinstance(value, str) or not value for value in (provider, cell_name, arm)):
        raise ValueError('provider, cell_name and arm must be nonempty strings')
    if type(seed) is not int or type(attempt) is not int or attempt < 1:
        raise ValueError('seed must be an integer and attempt a positive integer')
    if not actor.project_root:
        raise ValueError('Explicit actor.project_root is required')
    root = Path(actor.project_root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('project_root must be absolute without traversal')
    plan_bytes = _canonical(plan)
    # Round-trip rejects non-JSON objects and keys whose coercion would lose identity.
    if json.loads(plan_bytes) != plan:
        raise ValueError('Scientific plan must contain only JSON values and string keys')
    manifest = {
        'schema': 'tofu.experiment-source/v1',
        'owner_scope': actor.scope, 'project_root': str(root),
        'provider': provider, 'cell': {'name': cell_name, 'arm': arm, 'seed': seed},
        'attempt': attempt, 'code_sha256': code_sha256,
        'scientific_plan_sha256': hashlib.sha256(plan_bytes).hexdigest(),
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, content in (('experiment.py', code), ('plan.json', plan_bytes),
                              ('manifest.json', _canonical(manifest))):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o444) << 16
            archive.writestr(entry, content)
    payload = stream.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    source = {'uri': (root / _DIRECTORY / f'{digest}.zip').as_uri(), 'sha256': digest}
    return root, payload, source, manifest


@contextmanager
def _source_directory(root, *, create):
    """Pin each directory component so symlink swaps cannot redirect I/O."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        if create:
            try:
                os.mkdir(_DIRECTORY, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
        child = os.open(_DIRECTORY, flags, dir_fd=fd)
        os.close(fd)
        fd = child
        yield fd
    finally:
        os.close(fd)


def _verify_file(directory, name, payload):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(payload):
            raise ValueError('Source is not the expected regular ZIP file')
        if stream.read(len(payload) + 1) != payload:
            raise ValueError('Source hash, members or sealed content changed')


def create_experiment_source(*, actor, code, code_sha256, plan, provider,
                             cell_name, arm, seed, attempt):
    """Publish an atomic create-only seal, or verify an identical existing seal.

    Returns {'uri': <absolute file URI>, 'sha256': <ZIP digest>}. Code is never
    executed. Filesystem errors and invalid/changed seals fail without replacing
    existing content. A private temporary file is removed on success or failure.
    """
    root, payload, source, _ = _bundle(actor, code, code_sha256, plan, provider,
                                       cell_name, arm, seed, attempt)
    name = f'{source["sha256"]}.zip'
    with _source_directory(root, create=True) as directory:
        # dir_fd keeps publication in the pinned project even during renames.
        temporary = None
        try:
            candidate = f'.pending-{secrets.token_hex(16)}'
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            temporary = candidate
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
                os.fsync(directory)
            except FileExistsError:
                pass
            _verify_file(directory, name, payload)
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=directory)
    return source


def verify_experiment_source(source, *, actor, code, code_sha256, plan, provider,
                             cell_name, arm, seed, attempt):
    """Verify the full expected binding and exact ZIP; return its manifest.

    source must be exactly the reference returned by create_experiment_source,
    not an arbitrary URI or path. No archive entries are executed or extracted.
    """
    root, payload, expected, manifest = _bundle(actor, code, code_sha256, plan, provider,
                                               cell_name, arm, seed, attempt)
    if source != expected:
        raise ValueError('Source reference does not match expected experiment binding')
    with _source_directory(root, create=False) as directory:
        _verify_file(directory, f'{expected["sha256"]}.zip', payload)
    return manifest
