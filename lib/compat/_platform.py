"""lib/compat.py — Cross-platform compatibility helpers.

Provides platform-aware abstractions for:
  - Shell invocation (Linux/macOS: /bin/sh, Windows: cmd.exe for operator
    scripts, PowerShell for model-authored commands)
  - Non-blocking pipe I/O (fcntl on Unix, threading on Windows)
  - Process introspection (stdin detection via /proc on Linux, disabled elsewhere)
  - Temp directory paths
  - Username detection
  - Process existence checks
  - Signal compatibility

Usage::

    from lib.compat import (
        get_shell_args, get_username, get_temp_dir,
        is_process_alive, is_process_named, HAS_PROCFS,
        set_pipe_nonblocking, safe_select_pipes,
    )
"""

import getpass
import os
import sys
import tempfile

from lib.log import get_logger
from runtime_path_policy import get_runtime_base

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Platform detection
# ═══════════════════════════════════════════════════════════════════════

IS_WINDOWS = (os.name == 'nt')
IS_MACOS = (sys.platform == 'darwin')
IS_LINUX = (sys.platform.startswith('linux'))

# /proc filesystem is only available on Linux
HAS_PROCFS = IS_LINUX and os.path.isdir('/proc')


# ═══════════════════════════════════════════════════════════════════════
#  Shell invocation
# ═══════════════════════════════════════════════════════════════════════

def get_shell_args(command: str) -> list:
    """Return the argv list to execute *command* via the platform's shell.

    - Linux/macOS: ``['/bin/sh', '-c', command]``
    - Windows: ``['cmd.exe', '/c', command]``

    Args:
        command: The shell command string to execute.

    Returns:
        List of arguments suitable for ``subprocess.Popen`` / ``subprocess.run``.
    """
    if IS_WINDOWS:
        # cmd.exe is always available on Windows
        return ['cmd.exe', '/c', command]
    else:
        return ['/bin/sh', '-c', command]


# CreateProcessW: lpCommandLine holds at most 32,767 characters including NUL.
WINDOWS_COMMAND_LINE_LIMIT = 32767

# Runs the model's text as one PowerShell script. The text arrives as a
# single-quoted literal inside -EncodedCommand (no quoting layer, and not
# subject to execution policy, unlike -File). It is parsed before running so
# a syntax error reports the user's own line numbers, then dot-sourced with a
# trailer that exits with the last statement's status: a failing native
# command's own exit code, else 1 for a failed cmdlet, else 0. Output is
# UTF-8 without ANSI styling or progress records because it is captured.
_POWERSHELL_BOOTSTRAP = (
    "$ProgressPreference='SilentlyContinue'\n"
    "try{[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)}catch{}\n"
    "$OutputEncoding=[Text.UTF8Encoding]::new($false)\n"
    "if($PSStyle){$PSStyle.OutputRendering='PlainText'}\n"
    # PowerShell 7 still styles error records on redirected streams otherwise.
    "$env:__SuppressAnsiEscapeSequences='1'\n"
    "$__tofu_source='{source}'\n"
    "$__tofu_errors=$null\n"
    "[void][Management.Automation.Language.Parser]::ParseInput("
    "$__tofu_source,[ref]$null,[ref]$__tofu_errors)\n"
    "if($__tofu_errors){foreach($__tofu_error in $__tofu_errors){"
    "[Console]::Error.WriteLine('At line '+$__tofu_error.Extent.StartLineNumber+"
    "' char '+$__tofu_error.Extent.StartColumnNumber+': '+$__tofu_error.Message)};exit 1}\n"
    ". ([ScriptBlock]::Create($__tofu_source+\"`n\"+"
    "'$__tofu_ok=$?;$__tofu_code=$global:LASTEXITCODE;'+"
    "'if(-not $__tofu_ok){if($__tofu_code){exit $__tofu_code};exit 1};exit 0'))\n"
)


def windows_powershell_executable() -> str:
    """PowerShell 7 when installed, else the Windows PowerShell every Windows has."""
    import shutil
    found = shutil.which('pwsh') or shutil.which('powershell')
    if found:
        return found
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    return os.path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')


def powershell_args(command: str, executable: str | None = None) -> list:
    """Return the argv that runs *command* as a non-interactive PowerShell script.

    Raises ValueError when Windows could not start the resulting command line.
    """
    import base64
    import subprocess
    script = _POWERSHELL_BOOTSTRAP.replace('{source}', command.replace("'", "''"))
    argv = [executable or windows_powershell_executable(), '-NoLogo', '-NoProfile',
            '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-OutputFormat', 'Text',
            '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')]
    if len(subprocess.list2cmdline(argv)) >= WINDOWS_COMMAND_LINE_LIMIT:
        raise ValueError(
            'This command is too long for a Windows command line '
            f'({WINDOWS_COMMAND_LINE_LIMIT - 1} characters once encoded for PowerShell). '
            'Write the script to a .ps1 file first, then run that file.')
    return argv


def get_command_shell_args(command: str) -> list:
    """Return the argv for a model-authored ``run_command`` script.

    POSIX keeps :func:`get_shell_args`. Windows uses PowerShell: models write
    PowerShell (and its ls/cat/rm/cp aliases) far more reliably than cmd.exe
    batch syntax, and its argv needs no cmd.exe re-quoting.
    """
    return powershell_args(command) if IS_WINDOWS else get_shell_args(command)


# ═══════════════════════════════════════════════════════════════════════
#  Username
# ═══════════════════════════════════════════════════════════════════════

def get_username(fallback: str = 'postgres') -> str:
    """Get the current OS username, cross-platform.

    Uses ``getpass.getuser()`` which works on Linux (``USER``),
    macOS (``USER``), and Windows (``USERNAME`` / win32api).

    Args:
        fallback: Value to return if username detection fails.

    Returns:
        The current username string.
    """
    try:
        return getpass.getuser()
    except Exception as e:
        logger.debug('[compat] getpass.getuser failed: %s', e)
        return fallback


# ═══════════════════════════════════════════════════════════════════════
#  Temp directory
# ═══════════════════════════════════════════════════════════════════════

def get_temp_dir() -> str:
    """Return ``data/tmp/`` in the runtime data root.

    Uses it instead of the system ``/tmp`` because ``/tmp`` may not be
    accessible (or may be on a different filesystem) on all deployment
    machines, while the runtime data root is always writable.

    Falls back to ``tempfile.gettempdir()`` only if that directory cannot
    be created.

    Returns:
        Absolute path to a usable temp directory.
    """
    _project_tmp = os.path.join(get_runtime_base(), 'data', 'tmp')
    try:
        os.makedirs(_project_tmp, exist_ok=True)
        return _project_tmp
    except OSError as _e_audit:
        logger.debug('[_platform] get_temp_dir caught %s: %s', type(_e_audit).__name__, _e_audit)
        return tempfile.gettempdir()


# ═══════════════════════════════════════════════════════════════════════
#  Process introspection
# ═══════════════════════════════════════════════════════════════════════

def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID exists, cross-platform.

    Args:
        pid: Process ID to check.

    Returns:
        True if the process exists and we have permission to signal it.
    """
    if IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception as e:
            logger.debug('[compat] is_process_alive(%d) ctypes failed: %s', pid, e)
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError as _e_audit:
            logger.debug('[_platform] is_process_alive caught %s: %s', type(_e_audit).__name__, _e_audit)
            return False
        except PermissionError as _e_audit:
            logger.debug('[_platform] is_process_alive caught %s: %s', type(_e_audit).__name__, _e_audit)
            return True  # exists but we can't signal it


def is_process_named(pid: int, name_substring: str) -> bool:
    """Check if a running process's command name contains *name_substring*.

    Args:
        pid: Process ID to check.
        name_substring: Substring to look for in the process name (case-insensitive).

    Returns:
        True if the process exists and its name matches, False otherwise.
        Returns False on any error (process exited, no permission, etc.).
    """
    import subprocess
    name_substring = name_substring.lower()

    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=5
            )
            return name_substring in result.stdout.lower()
        except Exception as e:
            logger.debug('[compat] is_process_named(%d) tasklist failed: %s', pid, e)
            return False
    else:
        # Unix: use ps (works on both Linux and macOS)
        try:
            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'comm='],
                capture_output=True, text=True, timeout=5
            )
            return name_substring in result.stdout.lower()
        except Exception as e:
            logger.debug('[compat] is_process_named(%d) ps failed: %s', pid, e)
            return False


# ═══════════════════════════════════════════════════════════════════════
#  Non-blocking pipe I/O
# ═══════════════════════════════════════════════════════════════════════

def set_pipe_nonblocking(fd) -> bool:
    """Set a file descriptor / pipe to non-blocking mode.

    Args:
        fd: A file object with a ``fileno()`` method (e.g., ``proc.stdout``).

    Returns:
        True if non-blocking mode was set successfully, False on Windows
        (where fcntl is not available — use threading-based reading instead).
    """
    if IS_WINDOWS:
        # fcntl not available on Windows — caller must use threading
        return False
    try:
        import fcntl
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return True
    except Exception as e:
        logger.debug('[compat] set_pipe_nonblocking failed: %s', e)
        return False


def safe_select_pipes(read_fds, timeout=0.2):
    """Wait until any of ``read_fds`` is readable or at end-of-file.

    On Unix, uses ``select.poll()``, which has no descriptor-number ceiling:
    long-lived servers open descriptors above select()'s FD_SETSIZE (1024),
    where ``select.select()`` raises. A closed stream cannot be waited on;
    when no member can be, the call still waits ``timeout``, so a caller's
    polling loop never spins.
    On Windows, ``select()`` only works on sockets, so this returns
    all fds as "readable" (caller should use non-blocking read with
    exception handling).

    Args:
        read_fds: File objects or descriptors to wait on.
        timeout: Timeout in seconds.

    Returns:
        The members of ``read_fds`` that are readable or at end-of-file.
    """
    import time
    if IS_WINDOWS:
        # select() doesn't work on pipes on Windows.
        # Return all fds — caller should handle BlockingIOError.
        time.sleep(timeout)
        return list(read_fds)
    import select
    poller = select.poll()
    waited = {}
    for stream in read_fds:
        try:
            descriptor = stream if isinstance(stream, int) else stream.fileno()
        except (ValueError, OSError):
            continue  # closed: nothing left to read
        poller.register(descriptor, select.POLLIN)
        waited[descriptor] = stream
    if not waited:
        time.sleep(timeout)
        return []
    return [waited[descriptor]
            for descriptor, events in poller.poll(timeout * 1000)
            if not events & select.POLLNVAL]


# ═══════════════════════════════════════════════════════════════════════
#  Signal compatibility
# ═══════════════════════════════════════════════════════════════════════

def safe_signal(signum, handler):
    """Register a signal handler, returning the previous one.

    Silently skips if the signal doesn't exist on this platform
    (e.g., SIGTERM on Windows).

    Args:
        signum: Signal number (e.g., ``signal.SIGTERM``).
        handler: Signal handler function.

    Returns:
        The previous handler, or None if the signal was not available.
    """
    import signal
    try:
        return signal.signal(signum, handler)
    except (OSError, ValueError) as e:
        logger.debug('[compat] Cannot register signal %s: %s', signum, e)
        return None


# ═══════════════════════════════════════════════════════════════════════
#  Network mount detection
# ═══════════════════════════════════════════════════════════════════════

def is_network_mount(path: str) -> bool:
    """Detect if *path* is on a network/FUSE mount.

    Platform-specific heuristics:
      - Linux: path starts with ``/mnt/`` (DolphinFS, NFS, CIFS convention)
      - macOS: path under ``/Volumes/`` (excluding the boot volume)
      - Windows: UNC path (``\\\\server\\share``) or non-local drive

    This is a best-effort heuristic — not all network mounts are caught.

    Args:
        path: Absolute filesystem path.

    Returns:
        True if the path appears to be on a network/FUSE mount.
    """
    if IS_LINUX:
        return path.startswith('/mnt/')
    if IS_MACOS:
        # /Volumes/<name> is used for mounted volumes; the boot volume
        # is /Volumes/Macintosh HD (or similar) but we can't easily
        # distinguish local vs network here — be conservative.
        return False
    if IS_WINDOWS:
        # UNC paths are always network
        return path.startswith('\\\\')
    return False


# ═══════════════════════════════════════════════════════════════════════
#  Shell argument splitting
# ═══════════════════════════════════════════════════════════════════════

def safe_shlex_split(command: str) -> list:
    """Split a shell command string into tokens, cross-platform.

    On Unix, uses POSIX-mode splitting (handles backslash escapes).
    On Windows, uses non-POSIX mode (backslashes are path separators,
    only double-quotes for quoting).

    Args:
        command: Shell command string to split.

    Returns:
        List of argument tokens.
    """
    import shlex
    return shlex.split(command, posix=not IS_WINDOWS)
