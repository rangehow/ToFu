"""Make Tofu-started work yield to the servers: lowest CPU and I/O priority, first OOM victim.

Responsibility
--------------
Work Tofu starts (tool and background commands, MCP servers, scheduled jobs,
helpers, builds) shares the host with the API host, the execution host and
the storage engine. ``work_process_argv`` puts one ``/bin/sh`` step in front of
a work command that applies ``process_priority.work_process`` from
``contracts/resource_policy_v1.json`` to itself and then ``exec``\\ s the
command:

- ``oom_score_adj`` raised to the kernel maximum, so under memory exhaustion
  the kernel kills Tofu-started work, largest first, before any server process
  (docs/RESOURCE_CONTROL.md, "Tofu-started work is the kernel's first OOM
  victim");
- nice 19 and the idle I/O class, through ``nice`` and ``ionice``, so a burst
  of work can never starve the servers' event loops of CPU or disk
  (docs/RESOURCE_CONTROL.md, "Tofu-started work yields CPU and I/O to the
  servers").

All three need no privilege, apply to the new process before it starts any
thread, and are inherited by its descendants. Every step ``exec``\\ s, so the
PID, session and start time stay the command's own: process groups, Stop and
command records still name the command itself. Nothing is capped, refused,
queued or recycled; only the kernel's choice of victim and of who runs first
changes. The spawning server keeps its own values.

Entry points
------------
``work_process_argv(argv, cwd=None, env=None)``: the argv to give the spawn
call instead of ``argv``. ``cwd`` and ``env`` are the ones the spawn call
receives; they matter only when ``argv[0]`` is not an absolute path. Adopters
are listed in the OOM section of RESOURCE_CONTROL.md.

Dependencies
------------
Standard library, ``lib.log`` and ``lib.resource_control.contract``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import shlex
import shutil
import sys
import threading

from lib.log import get_logger
from lib.resource_control.contract import catalog_document

logger = get_logger(__name__)

# include/uapi/linux/oom.h
OOM_SCORE_ADJ_MAX = 1000
# ioprio_set(2) class numbers, as ``ionice -c`` takes them.
_IONICE_CLASS = {'realtime': 1, 'best-effort': 2, 'idle': 3}
_SELF_OOM_SCORE_ADJ = '/proc/self/oom_score_adj'
_SHELL = '/bin/sh'
# The priority tools are looked up in the system directories, never in a
# command's own PATH, so a project cannot shadow them.
_SYSTEM_PATH = '/usr/bin:/bin'

# ``None`` until probed; then the shell script that applies the policy and
# ``exec``\s the command, or ``''`` when this host supports none of it.
_supported: str | None = None
_supported_lock = threading.Lock()


def _work_policy() -> dict:
    return catalog_document()['process_priority']['work_process']


def _probe() -> str:
    if not sys.platform.startswith('linux') or not os.access(_SHELL, os.X_OK):
        return ''
    policy = _work_policy()
    prefix = ''
    if os.access(_SELF_OOM_SCORE_ADJ, os.W_OK):
        # A failed write leaves the command at its inherited value and must
        # not reach the command's own stderr.
        prefix = f'{{ echo {policy["oom_score_adj"]} >{_SELF_OOM_SCORE_ADJ}; }} 2>/dev/null; '
    else:
        logger.warning(
            '[work-process-priority] %s cannot be raised here (it is missing or read-only); '
            'started work keeps the OOM priority of the process that starts it, so under '
            'memory exhaustion the kernel may kill a server process first', _SELF_OOM_SCORE_ADJ)
    runners = []
    nice, ionice = shutil.which('nice', path=_SYSTEM_PATH), shutil.which('ionice', path=_SYSTEM_PATH)
    if nice:
        runners.append(f'{shlex.quote(nice)} -n {int(policy["nice"])} --')
    if ionice:
        # -t: a kernel without the I/O scheduler class still runs the command.
        runners.append(f'{shlex.quote(ionice)} -t -c {_IONICE_CLASS[policy["io_class"]]} --')
    if not (nice and ionice):
        logger.warning(
            '[work-process-priority] %s not found in %s; started work keeps the CPU or I/O '
            'priority of the process that starts it and can starve the servers',
            ' and '.join(name for name, path in (('nice', nice), ('ionice', ionice)) if not path),
            _SYSTEM_PATH)
    if not prefix and not runners:
        return ''
    return prefix + 'exec ' + ''.join(runner + ' ' for runner in runners) + '"$@"'


def _wrapper_script() -> str:
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
    """Return ``argv`` so that the started process yields to the servers."""
    command = list(argv)
    if not command:
        return command
    script = _wrapper_script()
    if not script or not _spawnable(os.fspath(command[0]), cwd, env):
        return command
    return [_SHELL, '-c', script, 'sh', *command]


__all__ = ['OOM_SCORE_ADJ_MAX', 'work_process_argv']
