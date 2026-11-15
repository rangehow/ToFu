"""The project journal's portable whole-file lock (``lib.project_mod.file_lock``).

The POSIX branch is ``flock`` and runs natively here. The Windows branch
(``LockFileEx``/``UnlockFileEx``) is specified against a fake kernel32: these
tests prove the arguments and the busy-to-``BlockingIOError`` mapping, not the
native Win32 behavior.
"""
import ctypes
import os

import pytest

from lib.project_mod import file_lock

pytestmark = pytest.mark.unit


class _FakeKernel32:
    def __init__(self, fail_with=0):
        self.fail_with = fail_with
        self.last_error = 0
        self.calls = []

    def LockFileEx(self, handle, flags, reserved, low, high, overlapped):
        target = ctypes.cast(overlapped, ctypes.POINTER(file_lock._Overlapped)).contents
        self.calls.append(('lock', handle, flags, reserved, low, high,
                           target.Offset, target.OffsetHigh, target.hEvent))
        self.last_error = self.fail_with
        return 0 if self.fail_with else 1

    def UnlockFileEx(self, handle, reserved, low, high, overlapped):
        target = ctypes.cast(overlapped, ctypes.POINTER(file_lock._Overlapped)).contents
        self.calls.append(('unlock', handle, reserved, low, high,
                           target.Offset, target.OffsetHigh))
        return 1


@pytest.fixture
def windows(monkeypatch):
    def install(fail_with=0):
        kernel32 = _FakeKernel32(fail_with)
        monkeypatch.setattr(file_lock, '_WINDOWS', True)
        monkeypatch.setattr(file_lock, '_windows_api', lambda: file_lock._WindowsApi(
            kernel32.LockFileEx, kernel32.UnlockFileEx,
            lambda descriptor: 4000 + descriptor, lambda: kernel32.last_error))
        return kernel32
    return install


LAST_BYTE = ((1 << 63) - 1) & 0xFFFFFFFF, (1 << 63) - 1 >> 32


@pytest.mark.parametrize(('shared', 'blocking', 'flags'), [
    (False, True, 0x2), (False, False, 0x2 | 0x1), (True, True, 0), (True, False, 0x1)])
def test_windows_lock_maps_mode_to_lockfileex_flags_on_one_high_byte(windows, shared, blocking, flags):
    kernel32 = windows()
    file_lock.lock(7, shared=shared, blocking=blocking)
    assert kernel32.calls == [('lock', 4007, flags, 0, 1, 0, *LAST_BYTE, None)]


@pytest.mark.parametrize('error', [33, 997])  # ERROR_LOCK_VIOLATION, ERROR_IO_PENDING
def test_windows_busy_lock_raises_the_posix_blocking_error(windows, error):
    windows(fail_with=error)
    with pytest.raises(BlockingIOError):
        file_lock.lock(7, shared=False, blocking=False)


def test_windows_other_failure_is_a_plain_os_error(windows):
    windows(fail_with=5)  # ERROR_ACCESS_DENIED
    with pytest.raises(OSError) as raised:
        file_lock.lock(7, shared=True, blocking=True)
    assert not isinstance(raised.value, BlockingIOError)


def test_windows_unlock_releases_the_same_byte(windows):
    kernel32 = windows()
    file_lock.unlock(7)
    assert kernel32.calls == [('unlock', 4007, 0, 1, 0, *LAST_BYTE)]


def test_posix_exclusive_nonblocking_conflict_and_shared_coexistence(tmp_path):
    path = tmp_path / 'held.lock'
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR)
    try:
        file_lock.lock(first, shared=True, blocking=False)
        file_lock.lock(second, shared=True, blocking=False)
        file_lock.unlock(second)
        with pytest.raises(BlockingIOError):
            file_lock.lock(second, shared=False, blocking=False)
        file_lock.unlock(first)
        file_lock.lock(second, shared=False, blocking=True)
        with pytest.raises(BlockingIOError):
            file_lock.lock(first, shared=True, blocking=False)
    finally:
        os.close(first)
        os.close(second)


def test_journal_modules_take_locks_only_through_the_portable_primitive():
    from pathlib import Path
    package = Path(file_lock.__file__).parent
    for name in ('journal_locks.py', 'undo_payloads.py'):
        assert 'fcntl' not in (package / name).read_text(encoding='utf-8'), name
