"""Portable whole-file lock for project journal lock files and undo payload packs.

Entry points: ``lock(descriptor, shared=, blocking=)`` and ``unlock(descriptor)``.
POSIX uses ``flock``. Windows uses ``LockFileEx``/``UnlockFileEx`` on one byte
at the largest signed 64-bit file offset: Windows byte-range locks are
mandatory, so locking real content would make the holder's own shared lock
refuse its pack write and make busy-holder diagnostics unreadable. No file
content reaches that byte, so on both platforms the lock excludes only other
lockers. A busy non-blocking lock raises ``BlockingIOError`` on both
platforms. Like ``flock``, a Windows lock belongs to the open file and is
released when its last handle closes; ``unlock`` releases it promptly.
Dependencies: the standard library only; ``fcntl`` or ``ctypes``/``msvcrt``
are imported lazily for the running platform.
"""
import ctypes
import errno
import os
from typing import Callable, NamedTuple

_WINDOWS = os.name == 'nt'

# Win32 LockFileEx flags and error codes (winbase.h / winerror.h).
_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_ERROR_LOCK_VIOLATION = 33
_ERROR_IO_PENDING = 997
_LOCK_BYTE = (1 << 63) - 1


class _Overlapped(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', ctypes.c_uint32), ('OffsetHigh', ctypes.c_uint32),
                ('hEvent', ctypes.c_void_p)]


class _WindowsApi(NamedTuple):
    lock_file_ex: Callable
    unlock_file_ex: Callable
    os_handle: Callable
    last_error: Callable


_api = None


def _windows_api():
    global _api
    if _api is None:
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        pointer = ctypes.POINTER(_Overlapped)
        kernel32.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.DWORD, pointer)
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, pointer)
        kernel32.UnlockFileEx.restype = wintypes.BOOL
        _api = _WindowsApi(kernel32.LockFileEx, kernel32.UnlockFileEx,
                           msvcrt.get_osfhandle, ctypes.get_last_error)
    return _api


def _lock_byte():
    overlapped = _Overlapped()
    overlapped.Offset = _LOCK_BYTE & 0xFFFFFFFF
    overlapped.OffsetHigh = _LOCK_BYTE >> 32
    return overlapped


def lock(descriptor, *, shared, blocking):
    """Lock ``descriptor``'s file shared or exclusive; busy and not blocking raises BlockingIOError."""
    if not _WINDOWS:
        import fcntl
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(descriptor, operation if blocking else operation | fcntl.LOCK_NB)
        return
    api = _windows_api()
    flags = ((0 if shared else _LOCKFILE_EXCLUSIVE_LOCK)
             | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY))
    overlapped = _lock_byte()
    if not api.lock_file_ex(api.os_handle(descriptor), flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = api.last_error()
        if error in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            raise BlockingIOError(errno.EAGAIN, 'File lock is held by another handle')
        raise OSError(errno.EIO, f'LockFileEx failed (Windows error {error})', None, error)


def unlock(descriptor):
    if not _WINDOWS:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    api = _windows_api()
    overlapped = _lock_byte()
    if not api.unlock_file_ex(api.os_handle(descriptor), 0, 1, 0, ctypes.byref(overlapped)):
        error = api.last_error()
        raise OSError(errno.EIO, f'UnlockFileEx failed (Windows error {error})', None, error)
