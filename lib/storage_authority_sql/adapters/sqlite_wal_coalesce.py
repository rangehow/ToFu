"""Build and load the SQLite VFS that writes each commit's WAL frames at once.

Responsibility: SQLite writes every WAL frame as two calls, which costs about
0.19 ms each on DolphinFS. ``sqlite_wal_coalesce.c`` is a VFS shim that hands
a transaction's contiguous WAL appends to the filesystem as one write, with
the same bytes and the same order relative to every sync (docs/STORAGE.md,
"The Sidecar coalesces each commit's WAL writes"). This module compiles that
source, as read when this module was imported, into a private per-user
directory under the host temporary root, loads it into this process's SQLite
library once, and reports whether connections can use it.

Entry points: :func:`wal_coalescing` (one prepared state per process) and
:func:`connection_target` (the ``sqlite3.connect`` arguments for a path).
Dependencies: standard library, ``serverctl_pkg.local_placement``; a C
compiler (``cc``, ``gcc`` or ``clang``) and the SQLite extension headers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any

from lib.log import get_logger
from serverctl_pkg.local_placement import temporary_root

VFS_NAME = 'tofu-wal-coalesce'
_LIBRARY_NAME = 'tofu_wal_coalesce.so'
_SOURCE_PATH = Path(__file__).with_name('sqlite_wal_coalesce.c')
# sqlite3_filename_database, which pairs a WAL with its database file.
_MINIMUM_SQLITE = (3, 31, 0)
_COMPILERS = ('cc', 'gcc', 'clang')
_HEADERS = ('sqlite3.h', 'sqlite3ext.h')

logger = get_logger('tofu.storage.sidecar.sqlite')

try:
    _SOURCE: bytes | None = _SOURCE_PATH.read_bytes()
    _SOURCE_UNAVAILABLE = ''
except OSError as _error:
    _SOURCE = None
    _SOURCE_UNAVAILABLE = f'{type(_error).__name__}: {_error}'


@dataclass(frozen=True)
class WalCoalescing:
    """Whether this process's connections can use the coalescing VFS."""

    vfs: str | None
    reason: str
    library: str = ''

    def report(self) -> dict[str, Any]:
        return {'active': self.vfs is not None, 'vfs': self.vfs or '',
                'reason': self.reason, 'library': self.library}


_prepare_lock = threading.Lock()
_prepared: WalCoalescing | None = None


def wal_coalescing() -> WalCoalescing:
    """Prepare the VFS once per process; later calls return the same state."""
    global _prepared
    prepared = _prepared
    if prepared is not None:
        return prepared
    with _prepare_lock:
        if _prepared is None:
            _prepared = _prepare()
            if _prepared.vfs is None:
                logger.warning('SQLite WAL write coalescing is unavailable; '
                               'connections use the default VFS: %s', _prepared.reason)
        return _prepared


def connection_target(path: str | os.PathLike[str], vfs: str | None) -> tuple[str, bool]:
    """Return ``(database, uri)`` for ``sqlite3.connect`` through ``vfs``."""
    if vfs is None:
        return os.fspath(path), False
    return f'{Path(os.path.abspath(path)).as_uri()}?vfs={vfs}', True


class _Unavailable(Exception):
    pass


def _prepare() -> WalCoalescing:
    try:
        library = _load(_build())
    except _Unavailable as reason:
        return WalCoalescing(None, str(reason))
    return WalCoalescing(VFS_NAME, 'loaded', str(library))


def _build() -> Path:
    if os.name != 'posix':
        raise _Unavailable('the shim is built only on POSIX hosts')
    if _SOURCE is None:
        raise _Unavailable(f'source was unavailable at import ({_SOURCE_UNAVAILABLE})')
    if sqlite3.sqlite_version_info < _MINIMUM_SQLITE:
        raise _Unavailable(f'SQLite {sqlite3.sqlite_version} predates 3.31')
    if not hasattr(sqlite3.Connection, 'enable_load_extension'):
        raise _Unavailable("Python's sqlite3 was built without extension loading")
    compiler = next(filter(None, (shutil.which(name) for name in _COMPILERS)), None)
    if compiler is None:
        raise _Unavailable('no C compiler (cc, gcc or clang) on PATH')
    include = _header_directory()
    digest = hashlib.sha256(_SOURCE)
    digest.update(os.path.realpath(compiler).encode())
    if include is not None:
        for header in _HEADERS:
            digest.update((include / header).read_bytes())
    try:
        directory = _private_directory()
    except OSError as exc:
        raise _Unavailable(f'build directory: {exc}') from exc
    target = directory / digest.hexdigest()[:24] / _LIBRARY_NAME
    if target.is_file():
        return target
    try:
        target.parent.mkdir(mode=0o700, exist_ok=True)
        return _compile(compiler, include, target)
    except OSError as exc:
        raise _Unavailable(f'compiler {compiler} could not run: {exc}') from exc


def _header_directory() -> Path | None:
    """The headers shipped with the interpreter's SQLite, else the compiler's."""
    for prefix in dict.fromkeys((sys.base_prefix, sys.prefix)):
        candidate = Path(prefix) / 'include'
        if all((candidate / header).is_file() for header in _HEADERS):
            return candidate
    return None


def _private_directory() -> Path:
    owner = os.getuid()
    directory = Path(temporary_root()) / f'tofu-sqlite-vfs-{owner}'
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    status = os.lstat(directory)
    # Another user can pre-create this predictable name in a shared /tmp, and
    # this process loads native code from it.
    if (not stat.S_ISDIR(status.st_mode) or status.st_uid != owner
            or stat.S_IMODE(status.st_mode) & 0o077):
        raise OSError(f'{directory} is not private to this user')
    return directory


def _compile(compiler: str, include: Path | None, target: Path) -> Path:
    staging_directory = Path(tempfile.mkdtemp(dir=target.parent, prefix='.staging-'))
    try:
        source = staging_directory / _SOURCE_PATH.name
        source.write_bytes(_SOURCE or b'')
        library = staging_directory / target.name
        command = [compiler, '-O2', '-shared', '-fPIC']
        if include is not None:
            command.append(f'-I{include}')
        command += ['-o', str(library), str(source)]
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False)
        if completed.returncode != 0:
            raise _Unavailable(
                f'{Path(compiler).name} exited {completed.returncode}: '
                f'{completed.stderr.strip()[-2000:]}')
        # Concurrent builders publish identical bytes; a reader never sees a
        # partial library.
        os.replace(library, target)
        return target
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)


def _load(library: Path) -> Path:
    connection = sqlite3.connect(':memory:')
    try:
        connection.enable_load_extension(True)
        try:
            # SQLite derives the entry point from the file name:
            # tofu_wal_coalesce.so -> sqlite3_tofuwalcoalesce_init.
            connection.load_extension(str(library))
        finally:
            connection.enable_load_extension(False)
    except sqlite3.Error as exc:
        raise _Unavailable(f'SQLite refused {library}: {exc}') from exc
    finally:
        connection.close()
    try:
        sqlite3.connect(f'file::memory:?vfs={VFS_NAME}', uri=True).close()
    except sqlite3.Error as exc:
        raise _Unavailable(f'{VFS_NAME} is not registered after loading: {exc}') from exc
    return library
