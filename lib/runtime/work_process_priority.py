"""Make Tofu-started work the kernel's first choice under memory exhaustion.

Responsibility
--------------
When memory runs out, the kernel kills the process with the highest
``oom_score``: its memory share plus its ``oom_score_adj``. Every process
inherits the container's ``oom_score_adj``, so the largest process, the
storage engine, dies first and takes every conversation with it, while the
commands that caused the pressure keep running. ``work_process_argv`` puts one
``/bin/sh`` step in front of a work command. That step raises its own
``oom_score_adj`` to the kernel maximum and then ``exec``\\ s the command, so
the kernel kills Tofu-started work, largest first, before any server process.
Raising one's own value needs no privilege. Descendants inherit the value
across fork and exec. ``exec`` keeps the PID, session and start time, so
process groups, Stop and command records still name the command itself.
Nothing is capped, refused or recycled; only the kernel's choice of victim
changes (docs/RESOURCE_CONTROL.md, "Tofu-started work is the kernel's first
OOM victim").

Entry points
------------
``work_process_argv(argv, cwd=None, env=None)``: the argv to give the spawn
call instead of ``argv``. ``cwd`` and ``env`` are the ones the spawn call
receives; they matter only when ``argv[0]`` is not an absolute path. Adopters
are listed in that RESOURCE_CONTROL.md section.

Dependencies
------------
Standard library and ``lib.log``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import shutil
import sys
import threading

from lib.log import get_logger

logger = get_logger(__name__)

# include/uapi/linux/oom.h
OOM_SCORE_ADJ_MAX = 1000
_SELF_OOM_SCORE_ADJ = '/proc/self/oom_score_adj'
_SHELL = '/bin/sh'
# A failed write leaves the command at its inherited priority and must not
# reach the command's own stderr.
_RAISE_THEN_EXEC = (
    f'{{ echo {OOM_SCORE_ADJ_MAX} >{_SELF_OOM_SCORE_ADJ}; }} 2>/dev/null; exec "$@"')

_supported: bool | None = None
_supported_lock = threading.Lock()


def _probe() -> bool:
    if not sys.platform.startswith('linux'):
        return False
    if os.access(_SELF_OOM_SCORE_ADJ, os.W_OK) and os.access(_SHELL, os.X_OK):
        return True
    logger.warning(
        '[work-process-priority] %s cannot be raised here (it is missing or read-only, '
        'or %s is unavailable); started work keeps the priority of the process that starts '
        'it, so under memory exhaustion the kernel may kill a server process first',
        _SELF_OOM_SCORE_ADJ, _SHELL)
    return False


def _oom_score_adj_supported() -> bool:
    global _supported
    if _supported is None:
        with _supported_lock:
            if _supported is None:
                _supported = _probe()
    return _supported


def _spawnable(program: str, cwd, env) -> bool:
    """Whether the spawn call itself would start ``program``, by its own lookup.

    A program it could not start stays unwrapped, so the spawn call still raises
    its launch error instead of the wrapper reporting a shell exit status.
    """
    if os.path.dirname(program):
        path = program if cwd is None else os.path.join(os.fspath(cwd), program)
        return os.access(path, os.X_OK) and not os.path.isdir(path)
    return shutil.which(program, path=os.pathsep.join(os.get_exec_path(env))) is not None


def work_process_argv(argv: Sequence[str], *, cwd: str | os.PathLike | None = None,
                      env: Mapping[str, str] | None = None) -> list[str]:
    """Return ``argv`` so that the started process is the preferred OOM victim."""
    command = list(argv)
    if not command or not _oom_score_adj_supported():
        return command
    if not _spawnable(os.fspath(command[0]), cwd, env):
        return command
    return [_SHELL, '-c', _RAISE_THEN_EXEC, 'sh', *command]


__all__ = ['OOM_SCORE_ADJ_MAX', 'work_process_argv']
