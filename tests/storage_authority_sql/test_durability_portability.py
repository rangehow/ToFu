"""Maintenance fsync works on Windows handles as well as POSIX ones.

Windows ``os.fsync`` is ``FlushFileBuffers``, which refuses a read-only handle
with EBADF; GitHub's windows-latest lane failed every SQLite backup that way.
"""

from __future__ import annotations

import os
from pathlib import Path
import types

import pytest

from lib.storage_authority_sql import durability

pytestmark = pytest.mark.unit

fcntl = pytest.importorskip('fcntl')


def _fsync_access_mode(monkeypatch, path: Path, os_name: str) -> int:
    modes = []

    def fsync(descriptor):
        modes.append(fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE)
        os.fsync(descriptor)

    monkeypatch.setattr(durability, 'os', types.SimpleNamespace(name=os_name, fsync=fsync))
    durability.fsync_file(path)
    assert len(modes) == 1
    return modes[0]


def test_windows_fsync_file_flushes_a_writable_handle_without_changing_content(
        tmp_path, monkeypatch):
    path = tmp_path / 'backup.sqlite3'
    path.write_bytes(b'backup bytes')

    assert _fsync_access_mode(monkeypatch, path, 'nt') == os.O_RDWR
    assert path.read_bytes() == b'backup bytes'


def test_posix_fsync_file_keeps_a_read_only_handle(tmp_path, monkeypatch):
    path = tmp_path / 'backup.sqlite3'
    path.write_bytes(b'backup bytes')

    assert _fsync_access_mode(monkeypatch, path, 'posix') == os.O_RDONLY
