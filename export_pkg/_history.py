"""Rebuild source commits in a separate, public-only Git object database.

Entry point: synchronize_public_history. Policy and sanitization come from the
existing export owners; the publication byte gate is shared with _publish.
No source Git objects, refs, tags, notes, signatures, or alternates are copied.
The first run needs an explicit source cutoff. Reviewed baseline bridges retain
public ancestry while representing exact filtered cutoff/branch-parent trees.
The final reconciliation uses the caller's already-verified export index tree.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re

import select
import time
import subprocess
import tempfile
import uuid
import zipfile

from export_pkg._excludes import _safe_export_relative_path, _should_exclude
from export_pkg._lists import (
    _OPENSOURCE_KEEP_FILES, _PUBLISH_MAX_FILE_BYTES, _PUBLISH_MAX_TREE_BYTES,
    _PUBLISH_PLACEHOLDERS, _PUBLISH_FORBIDDEN_NAMES, _PUBLISH_FORBIDDEN_ROOTS,
    _PUBLISH_FORBIDDEN_SUFFIXES, _PUBLISH_SOURCE_SQL_FILES,
)
from export_pkg._publish import ExportPublishSafetyError, _verify_opensource_blob
from export_pkg._sanitize import (
    _sanitize_defaults_for_export, _sanitize_source_opensource,
)

_HASH = re.compile(r'^[0-9a-f]{40}$')
_ROUTING_ENV = {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_NAMESPACE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'}

# These publication bounds stop cumulative history/archive growth, rather than
# changing runtime worker capacity. Review a smaller incremental cutoff on refusal.
_HISTORY_MAX_IMPORTED_BYTES = 8 * 1024 ** 3
_HISTORY_MAX_READ_BYTES = 16 * 1024 ** 3
_HISTORY_MAX_UNPACKED_BYTES = 8 * 1024 ** 3
_HISTORY_MAX_MEMBERS = 10_000_000
_HISTORY_MAX_METADATA_BYTES = 64 * 1024 ** 2
_HISTORY_MAX_SECONDS = 1800
_HISTORY_IO_SECONDS = 120


class _HistoryBudget:
    def __init__(self):
        self.deadline = time.monotonic() + _HISTORY_MAX_SECONDS
        self.imported = self.read = self.unpacked = self.members = self.metadata = 0

    def remaining(self, limit=120):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ExportPublishSafetyError('Public history batch deadline exceeded')
        return min(limit, remaining)

    def consume(self, kind: str, count: int):
        limits = {'imported': _HISTORY_MAX_IMPORTED_BYTES, 'read': _HISTORY_MAX_READ_BYTES,
                  'unpacked': _HISTORY_MAX_UNPACKED_BYTES, 'members': _HISTORY_MAX_MEMBERS,
                  'metadata': _HISTORY_MAX_METADATA_BYTES}
        if count < 0 or getattr(self, kind) + count > limits[kind]:
            raise ExportPublishSafetyError('Public history cumulative ' + kind + ' budget limit exceeded')
        self.remaining()
        setattr(self, kind, getattr(self, kind) + count)


class _HistoryPipe:
    """Nonblocking batch protocol IO; neither read nor write can outrun a deadline."""
    def __init__(self, process, budget: _HistoryBudget):
        self.process, self.budget = process, budget
        self.buffer = bytearray()
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)

    def _deadline(self):
        return time.monotonic() + self.budget.remaining(_HISTORY_IO_SECONDS)

    def _wait(self, stream, deadline, *, writing=False):
        remaining = min(deadline - time.monotonic(), self.budget.remaining())
        if remaining <= 0:
            raise ExportPublishSafetyError('Public history batch IO deadline exceeded')
        readable, writable, _ = select.select([] if writing else [stream],
                                              [stream] if writing else [], [], remaining)
        if not (writable if writing else readable):
            raise ExportPublishSafetyError('Public history batch IO deadline exceeded')

    def write(self, value: bytes):
        deadline = self._deadline()
        view = memoryview(value)
        try:
            while view:
                self._wait(self.process.stdin, deadline, writing=True)
                try:
                    count = os.write(self.process.stdin.fileno(), view[:65536])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise ExportPublishSafetyError('Public history batch pipe closed')
                view = view[count:]
        except BrokenPipeError as error:
            raise ExportPublishSafetyError('Public history batch pipe closed') from error

    def _receive(self, deadline):
        self._wait(self.process.stdout, deadline)
        try:
            chunk = os.read(self.process.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        if not chunk:
            raise ExportPublishSafetyError('Incomplete public history batch output')
        self.buffer.extend(chunk)

    def line(self, limit=1024) -> bytes:
        deadline = self._deadline()
        while True:
            newline = self.buffer.find(b'\n')
            if newline >= 0:
                if newline >= limit:
                    raise ExportPublishSafetyError('Public history batch header exceeds limit')
                value = bytes(self.buffer[:newline + 1])
                del self.buffer[:newline + 1]
                return value
            if len(self.buffer) >= limit:
                raise ExportPublishSafetyError('Public history batch header exceeds limit')
            self._receive(deadline)

    def exact(self, size: int) -> bytes:
        deadline = self._deadline()
        value = bytearray()
        while len(value) < size:
            if not self.buffer:
                self._receive(deadline)
            count = min(size - len(value), len(self.buffer))
            value.extend(self.buffer[:count])
            del self.buffer[:count]
        return bytes(value)

@dataclass(frozen=True)
class HistorySyncResult:
    public_tip: str
    source_tip: str
    created: int
    skipped: int
    mapping: dict[str, str]


def _environment():
    return {**{key: value for key, value in os.environ.items() if key not in _ROUTING_ENV},
            'GIT_NO_REPLACE_OBJECTS': '1'}


def _git(root: Path, *args: str, data: bytes | None = None, check=True,
         budget: _HistoryBudget | None = None) -> bytes:
    try:
        result = subprocess.run(['git', *args], cwd=root, input=data,
                                capture_output=True, env=_environment(),
                                timeout=budget.remaining() if budget else 120)
    except subprocess.TimeoutExpired as error:
        raise ExportPublishSafetyError('Public history Git deadline exceeded') from error
    if check and result.returncode:
        # No caller payload or authenticated URL enters this diagnostic.
        raise ExportPublishSafetyError('Public history Git operation failed: ' +
                                       result.stderr.decode('utf-8', 'replace')[-1000:])
    return result.stdout


def _resolve(root: Path, ref: str, *, budget=None) -> str:
    if not ref or ref.startswith('-'):
        raise ExportPublishSafetyError('Invalid public history source reference')
    result = _git(root, 'rev-parse', '--verify', ref + '^{commit}', budget=budget).decode().strip()
    if not _HASH.fullmatch(result):
        raise ExportPublishSafetyError('Invalid public history commit identity')
    return result


def _git_dir(root: Path, *, budget=None) -> Path:
    raw = _git(root, 'rev-parse', '--git-common-dir', budget=budget).decode().strip()
    return (root / raw).resolve()


def _verify_object_database(root: Path, git_dir: Path, *, budget=None) -> None:
    if any((git_dir / rel).exists() for rel in ('shallow', 'info/grafts', 'objects/info/alternates')):
        raise ExportPublishSafetyError('History refuses shallow, grafted or alternate Git objects')
    if _git(root, 'for-each-ref', '--format=%(refname)', 'refs/replace', budget=budget).strip():
        raise ExportPublishSafetyError('History refuses replaced Git objects')
    objects = git_dir / 'objects'
    if objects.is_symlink() or not objects.is_dir():
        raise ExportPublishSafetyError('History refuses symbolic or missing Git object directories')
    # Loose-object fanout and pack/info are exactly one level deep. Do not
    # follow a pack, loose-object directory or individual object symlink.
    for directory in objects.iterdir():
        if budget:
            budget.remaining()
        if directory.is_symlink():
            raise ExportPublishSafetyError('History refuses shared symbolic Git objects')
        if directory.is_dir():
            for member in directory.iterdir():
                if member.is_symlink():
                    raise ExportPublishSafetyError('History refuses shared symbolic Git objects')

def _policy_digest() -> str:
    owner = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ('_lists.py', '_excludes.py', '_sanitize.py', '_publish.py', '_history.py'):
        digest.update(name.encode() + b'\0' + (owner / name).read_bytes())
    return digest.hexdigest()


def _seal(document: dict) -> dict:
    value = {key: item for key, item in document.items() if key != 'integrity'}
    payload = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return {**value, 'integrity': hashlib.sha256(payload).hexdigest()}


def _read_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ExportPublishSafetyError('Unsafe public history cursor')
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ExportPublishSafetyError('Unreadable public history cursor') from error
    if not isinstance(value, dict) or value.get('integrity') != _seal(value)['integrity']:
        raise ExportPublishSafetyError('Invalid public history cursor integrity')
    return value


def _write_state(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(_seal(value), stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if hasattr(os, 'O_DIRECTORY'):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _safe_path(path: str) -> None:
    try:
        _safe_export_relative_path(path)
    except (ValueError, UnicodeError) as error:
        raise ExportPublishSafetyError('Unsafe public history path') from error
    _verify_opensource_blob(path.encode('utf-8'), 'history path')


def _included(path: str) -> bool:
    return path in _OPENSOURCE_KEEP_FILES or not _should_exclude(
        path, PurePosixPath(path).name, 'opensource')


def _tree_entries(root: Path, ref: str, *, budget=None):
    entries = []
    for record in _git(root, 'ls-tree', '-r', '-l', '-z', ref, budget=budget).split(b'\0'):
        if not record:
            continue
        metadata, path = record.split(b'\t', 1)
        mode, kind, oid, size = metadata.split()
        label = path.decode('utf-8', 'strict')

        if budget:
            budget.consume('members', 1)
        entries.append((label, mode.decode(), kind.decode(), oid.decode(), size))
    return entries


class _ObjectReader:
    def __init__(self, root: Path, budget: _HistoryBudget):
        self.budget = budget
        self.process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=root,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=_environment(), bufsize=0)
        self.pipe = _HistoryPipe(self.process, budget)

    def read(self, oid: str) -> bytes:
        self.pipe.write(oid.encode() + b'\n')
        header = self.pipe.line().split()
        if len(header) != 3 or header[0].decode() != oid or header[1] != b'blob':
            raise ExportPublishSafetyError('Could not read a public history source blob')
        size = int(header[2])
        if size < 0 or size >= _PUBLISH_MAX_FILE_BYTES:
            raise ExportPublishSafetyError('Public history blob exceeds publication size limit')
        self.budget.consume('read', size)
        value = self.pipe.exact(size)
        if self.pipe.exact(1) != b'\n':
            raise ExportPublishSafetyError('Incomplete public history source blob')
        return value

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def _public_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return path in _PUBLISH_PLACEHOLDERS or (_included(path) and not (
        parts[0] in _PUBLISH_FORBIDDEN_ROOTS or name in _PUBLISH_FORBIDDEN_NAMES
        or (name.startswith('.env.') and name != '.env.example')
        or (PurePosixPath(name).suffix.lower() in _PUBLISH_FORBIDDEN_SUFFIXES
            and path not in _PUBLISH_SOURCE_SQL_FILES)))


def _check_blob(value: bytes, path: str, budget: _HistoryBudget, depth=0, archive_bytes=None) -> None:
    budget.remaining()
    _verify_opensource_blob(value, path)
    unsupported = (b'\x1f\x8b', b'\xfd7zXZ\x00', b'BZh', b'7z\xbc\xaf\x27\x1c',
                   b'Rar!\x1a\x07', b'\x28\xb5\x2f\xfd')
    if value.startswith(unsupported) or value[257:262] == b'ustar':
        raise ExportPublishSafetyError('Unsupported public history archive compression')
    if not zipfile.is_zipfile(io.BytesIO(value)):
        return
    if depth >= 3:
        raise ExportPublishSafetyError('Public history archive nesting exceeds verification limit')
    archive_bytes = [0] if archive_bytes is None else archive_bytes
    names = set()
    try:
        with zipfile.ZipFile(io.BytesIO(value)) as archive:
            for member in archive.infolist():

                budget.consume('members', 1)
                raw_name = member.orig_filename
                budget.consume('metadata', len(raw_name.encode('utf-8')) + len(member.extra) + len(member.comment))
                directory = raw_name.endswith('/')
                name = raw_name[:-1] if directory else raw_name
                _safe_path(name)
                if name in names or not _public_path(name) or member.flag_bits & 1:
                    raise ExportPublishSafetyError('Duplicate, private or encrypted public history archive member')
                names.add(name)
                file_type = (member.external_attr >> 16) & 0o170000
                if file_type not in (0, 0o040000 if directory else 0o100000):
                    raise ExportPublishSafetyError('Unsupported public history archive member type')
                if directory:
                    if member.file_size != 0 or member.CRC != 0 or archive.read(member) != b'':
                        raise ExportPublishSafetyError('Public history archive directory contains payload')
                    continue
                archive_bytes[0] += member.file_size
                budget.consume('unpacked', member.file_size)
                if (member.file_size >= _PUBLISH_MAX_FILE_BYTES
                        or archive_bytes[0] >= _PUBLISH_MAX_TREE_BYTES
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ExportPublishSafetyError('Unsafe public history archive member')
                _check_blob(archive.read(member), path + ':' + name, budget, depth + 1, archive_bytes)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        if isinstance(error, ExportPublishSafetyError):
            raise
        raise ExportPublishSafetyError('Public history archive verification failed') from error


def _verify_public_anchor(public: Path, tip: str, budget: _HistoryBudget) -> None:
    """Admit the actual parent ancestry, not merely a separate .git directory."""
    if not tip:
        return
    # A maintainer source can contain previously published public merges.
    # Object-ID overlap alone proves neither private provenance nor safety;
    # admit the ancestor only after inspecting its actual paths/bytes/metadata.
    public_commits = _git(public, 'rev-list', tip, budget=budget).decode().splitlines()
    if len(public_commits) > 10000:
        raise ExportPublishSafetyError('Public anchor history exceeds commit verification limit')
    reader = _ObjectReader(public, budget)
    checked_blobs = set()
    checked_trees = set()
    try:
        for oid in public_commits:
            # Scan raw metadata before any sanitizer can conceal an unsafe anchor.
            size = int(_git(public, 'cat-file', '-s', oid, budget=budget))
            budget.consume('metadata', size)
            _verify_opensource_blob(_git(public, 'cat-file', 'commit', oid, budget=budget),
                                    'public anchor metadata')
            tree = _git(public, 'rev-parse', oid + '^{tree}', budget=budget).decode().strip()
            if tree in checked_trees:
                continue
            checked_trees.add(tree)
            total = 0
            for path, mode, kind, blob, size in _tree_entries(public, tree, budget=budget):
                _safe_path(path)
                if not _public_path(path) or kind != 'blob' or mode not in ('100644', '100755'):
                    raise ExportPublishSafetyError('Public anchor contains a private or unsupported path')
                total += int(size)
                if total >= _PUBLISH_MAX_TREE_BYTES:
                    raise ExportPublishSafetyError('Public anchor exceeds publication tree size limit')
                if blob not in checked_blobs:
                    _check_blob(reader.read(blob), path, budget)
                    checked_blobs.add(blob)
    finally:
        reader.close()


def _verify_resume_admission(public: Path, state: dict, published_tip: str | None,
                             budget: _HistoryBudget) -> None:
    """A cursor is not authority to disclose grandfathered ancestors elsewhere."""
    if 'acknowledged_anchor' not in state:
        raise ExportPublishSafetyError('Public history cursor lacks receiving ancestry evidence; review before retrying')
    anchor = state['acknowledged_anchor']
    if anchor is None:
        return  # This initial ancestry passed the complete raw-object gate.
    if (not isinstance(anchor, str) or not _HASH.fullmatch(anchor)
            or not published_tip or not _HASH.fullmatch(published_tip)
            or _git(public, 'merge-base', anchor, state['public_tip'], budget=budget).decode().strip() != anchor
            or _git(public, 'merge-base', anchor, published_tip, budget=budget).decode().strip() != anchor):
        raise ExportPublishSafetyError(
            'Previously acknowledged public ancestry must still exist at every receiving endpoint')


def _tree_hash(entries: list[tuple[str, str, str, int]]) -> str:
    tree = {}
    for path, mode, oid, _ in entries:
        node = tree
        parts = path.split('/')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, oid)

    def encode(node):
        members = []
        for name, child in node.items():
            directory = isinstance(child, dict)
            mode, oid = ('40000', encode(child)) if directory else child
            members.append((name.encode() + (b'/' if directory else b''),
                            mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)))
        data = b''.join(value for _, value in sorted(members))
        return hashlib.sha1(b'tree ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return encode(tree)


def _raw_date(value: str) -> str:
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ExportPublishSafetyError('Unattributed public history author date')
    return str(int(date.timestamp())) + ' ' + date.strftime('%z')


def _metadata(source: Path, oid: str, budget: _HistoryBudget):
    size = int(_git(source, 'cat-file', '-s', oid, budget=budget))
    budget.consume('metadata', size)
    raw = _git(source, 'show', '-s', '--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B',
               oid, budget=budget)
    author, email, author_date, committer, committer_email, commit_date, message = raw.decode(
        'utf-8', 'strict').split('\0', 6)
    values = []
    for field in (author, email, committer, committer_email):
        field = _sanitize_source_opensource(field, 'commit-metadata.txt').strip()
        if any(char in field for char in '\r\n<>\0'):
            raise ExportPublishSafetyError('Unsafe public history contributor identity')
        if '@' in field and 'internal.example.com' in field:
            field = 'rangehow@users.noreply.github.com'
        _verify_opensource_blob(field.encode(), 'commit contributor')
        values.append(field)
    message = _sanitize_source_opensource(message, 'commit-message.md').rstrip()
    # Original IDs remain in the private cursor, not public metadata/trailers.
    message = re.sub(r'\b[0-9a-f]{40}\b', lambda match: 'source-id:' + hashlib.sha256(
        match[0].encode()).hexdigest()[:20], message)
    _verify_opensource_blob(message.encode(), 'commit message')
    return values[0], values[1], _raw_date(author_date), values[2], values[3], _raw_date(commit_date), message


class _Importer:
    def __init__(self, public: Path, budget: _HistoryBudget):
        self.public, self.budget = public, budget
        self.ref = 'refs/tofu/history-staging/' + uuid.uuid4().hex
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(['git', 'fast-import', '--quiet', '--date-format=raw'],
                                        cwd=public, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, env=_environment(), bufsize=0)
        self.pipe = _HistoryPipe(self.process, budget)
        self.next_mark = 1
        self.marks = {}
        self.blobs = set()
        self.expected_trees = {}
        self.expected_parents = {}

    def _send(self, data: bytes):
        self.pipe.write(data)

    def _mark(self) -> int:
        value = self.next_mark
        self.next_mark += 1
        return value

    def _oid(self, mark: int) -> str:
        self._send(f'get-mark :{mark}\n'.encode())
        value = self.pipe.line().decode().strip()
        if not _HASH.fullmatch(value):
            raise ExportPublishSafetyError('Public history import did not acknowledge an object')
        return value

    def blob(self, value: bytes) -> str:
        expected = hashlib.sha1(b'blob ' + str(len(value)).encode() + b'\0' + value).hexdigest()
        if expected not in self.blobs:

            self.budget.consume('imported', len(value))
            mark = self._mark()
            self._send(f'blob\nmark :{mark}\ndata {len(value)}\n'.encode() + value + b'\n')
            if self._oid(mark) != expected:
                raise ExportPublishSafetyError('Public history blob identity mismatch')
            self.blobs.add(expected)
        return expected

    def commit(self, entries, parents, metadata, source_id):
        author, email, author_date, committer, committer_email, commit_date, message = metadata
        message += '\n\nPublic-Source-Id: ' + hashlib.sha256(source_id.encode()).hexdigest()[:24] + '\n'
        _verify_opensource_blob(message.encode(), 'public commit message')
        mark = self._mark()
        if not parents:
            self._send(f'reset {self.ref}\n\n'.encode())
        header = (f'commit {self.ref}\nmark :{mark}\nauthor {author} <{email}> {author_date}\n'
                  f'committer {committer} <{committer_email}> {commit_date}\ndata {len(message.encode())}\n')
        self._send(header.encode() + message.encode() + b'\n')

        self.budget.consume('metadata', len(header.encode()) + len(message.encode()))
        for index, parent in enumerate(parents):
            reference = ':' + str(self.marks[parent]) if parent in self.marks else parent
            self._send((('from ' if index == 0 else 'merge ') + reference + '\n').encode())
        self._send(b'deleteall\n')
        for path, mode, oid, _ in entries:
            quoted = json.dumps(path, ensure_ascii=False)
            self._send(f'M {mode} {oid} {quoted}\n'.encode())
        self._send(b'\n')
        oid = self._oid(mark)
        self.marks[oid] = mark
        self.expected_trees[oid] = _tree_hash(entries)
        self.expected_parents[oid] = list(parents)
        return oid

    def finish(self):
        self._send(b'done\n')
        self.process.stdin.close()
        try:
            status = self.process.wait(timeout=self.budget.remaining())
        except subprocess.TimeoutExpired as error:
            raise ExportPublishSafetyError('Public history import did not settle') from error
        if status:
            self.stderr.seek(0)
            detail = self.stderr.read(1000).decode('utf-8', 'replace')
            raise ExportPublishSafetyError('Public history import failed: ' + detail)
        # Verify actual commit trees, not archive projections or importer assumptions.
        for oid, expected in self.expected_trees.items():
            actual = _git(self.public, 'rev-parse', oid + '^{tree}', budget=self.budget).decode().strip()
            if actual != expected:
                raise ExportPublishSafetyError('Public history committed-tree mismatch')

            parents = _git(self.public, 'show', '-s', '--format=%P', oid, budget=self.budget).decode().split()
            if parents != self.expected_parents[oid]:
                raise ExportPublishSafetyError('Public history committed-parent mismatch')

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()
        _git(self.public, 'update-ref', '-d', self.ref, check=False)


def synchronize_public_history(source: Path, public: Path, source_sha: str,
                               verified_tree: str, commit_message: str | None = None,
                               *, history_base: str | None = None,
                               published_tip: str | None = None) -> HistorySyncResult:
    """Append filtered commits plus exact export reconciliation; never push here.

    An explicit first cutoff creates auditable baseline bridges, not a claim
    that an old snapshot title/version proves source/public equivalence.
    Subsequent runs require the private, integrity-checked cursor. A new source
    ancestry, foreign public advance or policy change requires fresh review.
    """
    budget = _HistoryBudget()
    source, public = source.resolve(), public.resolve()
    source_git, public_git = _git_dir(source, budget=budget), _git_dir(public, budget=budget)
    _verify_object_database(source, source_git, budget=budget)
    _verify_object_database(public, public_git, budget=budget)
    if source_git == public_git or os.path.samefile(source_git / 'objects', public_git / 'objects'):
        raise ExportPublishSafetyError('History destination must be an independent public Git repository')
    source_sha = _resolve(source, source_sha, budget=budget)
    public_tip = _git(public, 'rev-parse', '--verify', 'HEAD', check=False, budget=budget).decode().strip()
    branch = _git(public, 'symbolic-ref', 'HEAD', budget=budget).decode().strip()
    if not branch.startswith('refs/heads/'):
        raise ExportPublishSafetyError('Public history needs an explicit local branch')
    repository = hashlib.sha256(str(source_git).encode()).hexdigest()
    policy = _policy_digest()
    cursor_path = public_git / 'tofu-export-history.json'
    pending_path = public_git / 'tofu-export-history.pending.json'
    lock_path = public_git / 'tofu-export-history.lock'
    try:
        lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock)
            raise
    except OSError as error:
        raise ExportPublishSafetyError('Another public history publication owns the cursor lock') from error
    reader = importer = None
    try:
        cursor = _read_state(cursor_path)
        pending = _read_state(pending_path)
        if pending:
            if (pending.get('schema') != 3 or pending.get('source_tip') != source_sha or pending.get('policy') != policy
                    or pending.get('repository') != repository or pending.get('tree') != verified_tree):
                raise ExportPublishSafetyError('Pending public history cursor needs reconciliation')
            _verify_resume_admission(public, pending, published_tip, budget)
            if public_tip == pending['public_tip']:
                _write_state(cursor_path, pending)
                pending_path.unlink()
                return HistorySyncResult(public_tip, source_sha, 0, 0, pending['mapping'])
            if public_tip != pending['previous_public_tip']:
                raise ExportPublishSafetyError('Pending public history cursor lost its public parent')
        if cursor:
            if (cursor.get('schema') != 3 or cursor.get('repository') != repository
                    or cursor.get('policy') != policy or cursor.get('public_tip') != public_tip):
                raise ExportPublishSafetyError('Public history cursor/source/public/policy mismatch; review before retrying')
            _verify_resume_admission(public, cursor, published_tip, budget)
            mapping = dict(cursor['mapping'])
            base = _resolve(source, cursor['source_tip'], budget=budget)
            if history_base and _resolve(source, history_base, budget=budget) != cursor['source_base']:
                raise ExportPublishSafetyError('Explicit history-base disagrees with the private cursor')
            if _git(source, 'merge-base', base, source_sha, budget=budget).decode().strip() != base:
                raise ExportPublishSafetyError('Source history diverged from the publication cursor')
            if base == source_sha and cursor.get('tree') == verified_tree:
                return HistorySyncResult(public_tip, source_sha, 0, 0, mapping)
            source_base = cursor['source_base']
        else:
            if not history_base:
                raise ExportPublishSafetyError('First commit synchronization requires an explicit --history-base')
            base = _resolve(source, history_base, budget=budget)
            if _git(source, 'merge-base', base, source_sha, budget=budget).decode().strip() != base:
                raise ExportPublishSafetyError('history-base must be an ancestor of the pinned source')
            mapping = {}
            source_base = base
        acknowledged_anchor = cursor['acknowledged_anchor'] if cursor else (
            pending['acknowledged_anchor'] if pending else published_tip)
        if not cursor:
            if published_tip is not None:
                if published_tip != public_tip or not _HASH.fullmatch(published_tip):
                    raise ExportPublishSafetyError('Published anchor differs from the verified public tip')
                # _git_push supplies this only when EVERY configured target
                # already acknowledges this exact commit and its ancestry.
                # Existing published bytes are not new disclosures; never
                # grandfather an unverified clone or a missing target branch.
            else:
                _verify_public_anchor(public, public_tip, budget)
        order = _git(source, 'rev-list', '--reverse', '--topo-order', base + '..' + source_sha,
                     budget=budget).decode().splitlines()
        if len(order) > 10000:
            raise ExportPublishSafetyError('History range is too broad; select a reviewed incremental cutoff')
        reader = _ObjectReader(source, budget)
        importer = _Importer(public, budget)
        cache = {}
        path_policy = {}
        trees = {}
        created = skipped = 0
        default = _metadata(source, source_sha, budget)

        def project(oid):
            entries = []
            version = _git(source, 'show', oid + ':VERSION', budget=budget).decode().strip()
            total = 0
            for path, mode, kind, raw_oid, size in _tree_entries(source, oid, budget=budget):
                allowed = path_policy.setdefault(path, _included(path))
                if not allowed:
                    continue
                _safe_path(path)

                if not _public_path(path):
                    raise ExportPublishSafetyError('Public history source contains a forbidden private path')
                if mode not in ('100644', '100755') or kind != 'blob':
                    raise ExportPublishSafetyError('Unsupported public history Git entry: ' + path)
                if int(size) >= _PUBLISH_MAX_FILE_BYTES:
                    raise ExportPublishSafetyError('Public history source file is too large: ' + path)
                key = (raw_oid, path, version if path.endswith(('.html', '.js')) else '')
                if key not in cache:
                    value = reader.read(raw_oid)
                    try:
                        text = value.decode('utf-8')
                        if '\0' not in text:
                            value = _sanitize_source_opensource(
                                _sanitize_defaults_for_export(text, path, version), path).encode('utf-8')
                    except UnicodeDecodeError:
                        pass
                    _check_blob(value, path, budget)
                    cache[key] = (importer.blob(value), len(value))
                blob, byte_size = cache[key]
                total += byte_size
                entries.append((path, mode, blob, byte_size))
            if total >= _PUBLISH_MAX_TREE_BYTES:
                raise ExportPublishSafetyError('Public history source tree exceeds publication size limit')
            return entries

        def boundary(oid):
            nonlocal created
            if oid in mapping:
                mapped = mapping[oid]
                if mapped not in trees:
                    trees[mapped] = _git(public, 'rev-parse', mapped + '^{tree}', budget=budget).decode().strip()
                return mapped
            entries = project(oid)
            metadata = (*default[:6], 'Establish sanitized public history baseline')
            parent = [public_tip] if public_tip else []
            public_oid = importer.commit(entries, parent, metadata, 'baseline:' + oid)
            mapping[oid] = public_oid
            trees[public_oid] = _tree_hash(entries)
            created += 1
            return public_oid

        if not cursor:
            boundary(base)
        else:
            trees[public_tip] = _git(public, 'rev-parse', public_tip + '^{tree}', budget=budget).decode().strip()
        for oid in order:
            raw_parents = _git(source, 'show', '-s', '--format=%P', oid, budget=budget).decode().split()
            parents = list(dict.fromkeys(boundary(parent) for parent in raw_parents))
            entries = project(oid)
            tree = _tree_hash(entries)
            metadata = _metadata(source, oid, budget)
            if len(parents) == 1 and trees.get(parents[0]) == tree:
                mapping[oid] = parents[0]
                skipped += 1
                continue
            mapped = importer.commit(entries, parents, metadata, oid)
            mapping[oid] = mapped
            trees[mapped] = tree
            created += 1
        projected_source_tip = mapping.get(source_sha) or boundary(source_sha)
        unchanged_projection = bool(cursor and mapping.get(base) == projected_source_tip
                                    and cursor.get('tree') == verified_tree)
        projected_tip = public_tip if unchanged_projection else projected_source_tip
        needs_parent_bridge = bool(cursor and mapping.get(base) != public_tip)
        if not unchanged_projection and (trees.get(projected_source_tip) != verified_tree
                                         or needs_parent_bridge):
            final = []
            public_reader = _ObjectReader(public, budget)
            try:
                total = 0
                for path, mode, kind, oid, size in _tree_entries(public, verified_tree, budget=budget):
                    _safe_path(path)
                    if not _public_path(path):
                        raise ExportPublishSafetyError('Verified export contains a private history path: ' + path)
                    if kind != 'blob' or mode not in ('100644', '100755'):
                        raise ExportPublishSafetyError('Unsupported verified public export entry')
                    value = public_reader.read(oid)
                    _check_blob(value, path, budget)
                    total += len(value)
                    final.append((path, mode, oid, len(value)))
                if total >= _PUBLISH_MAX_TREE_BYTES:
                    raise ExportPublishSafetyError('Verified public export exceeds size limit')
            finally:
                public_reader.close()
            message = _sanitize_source_opensource(
                commit_message or 'Reconcile verified public release outputs', 'commit-message.md')
            parents = [projected_source_tip]
            if needs_parent_bridge and public_tip != projected_source_tip:
                parents.append(public_tip)
            projected_tip = importer.commit(final, parents, (*default[:6], message),
                                            'export:' + source_sha + ':' + verified_tree)
            created += 1
        importer.finish()
        actual_tree = _git(public, 'rev-parse', projected_tip + '^{tree}', budget=budget).decode().strip()
        if actual_tree != verified_tree:
            raise ExportPublishSafetyError('Public history final tree differs from the gated export')
        if public_tip and _git(public, 'merge-base', public_tip, projected_tip, budget=budget).decode().strip() != public_tip:
            raise ExportPublishSafetyError('Public history does not preserve the reviewed public parent')
        document = dict(schema=3, repository=repository, policy=policy, source_base=source_base,
                        acknowledged_anchor=acknowledged_anchor,
                        source_tip=source_sha, previous_public_tip=public_tip,
                        public_tip=projected_tip, tree=verified_tree, mapping=mapping)
        _write_state(pending_path, document)
        _git(public, 'update-ref', branch, projected_tip, public_tip or '0' * 40, budget=budget)
        _write_state(cursor_path, document)
        pending_path.unlink()
        print(f'  Public commit history: {created} rebuilt, {skipped} private/empty commits folded.')
        return HistorySyncResult(projected_tip, source_sha, created, skipped, mapping)
    finally:
        if reader:
            reader.close()
        if importer:
            importer.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
