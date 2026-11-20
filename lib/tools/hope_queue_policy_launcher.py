"""Install the owner Hope-queue guard ahead of ``hope`` and ``mlp`` on PATH.

The generated directory contains only tiny POSIX launchers. They execute the
stdlib-only policy helper from ``PinnedHelperSource.path()`` with the Tofu
interpreter in isolated mode, then exec the real CLI with unchanged arguments.
Policy location and caller project are baked into each launcher, so an agent's
shell cannot unset them to bypass the check. The real executable is resolved
against the child's actual PATH (including a project .venv) while excluding
our own shim directories.

Residual: this is a PATH guard, not a sandbox. An agent that invokes the real
CLI by absolute path, or another submission client, bypasses it; Tofu's MCP
boundary (``lib.mcp.queue_policy.submission_refusal``) is the second,
independent check.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
from typing import Optional

from lib.runtime.pinned_helper_source import PinnedHelperSource
from lib.tools.hope_queue_policy import POLICY_ENV, POLICY_FILENAME

_SHIM_SOURCE = PinnedHelperSource(Path(__file__).with_name('hope_queue_policy.py'))
_COMMANDS = ('hope', 'mlp')
_DIRECTORY_PREFIX = 'tofu-hope-queue-shims'
_LAUNCHER = '''#!/bin/sh
exec {python} -I -S {helper} --policy {policy} --project {project} --real {real} -- {real} "$@"
'''


def policy_path(owner_user_id: int) -> str:
    """The Global Router's policy file for one owner, without creating it."""
    from lib.router.home import router_home_location
    return os.path.join(router_home_location(owner_user_id), POLICY_FILENAME)


def _owner_id(environment: dict[str, str], owner_user_id: Optional[int]) -> Optional[int]:
    if owner_user_id is not None:
        if type(owner_user_id) is not int or owner_user_id < 1:
            raise ValueError('MCP owner_user_id must be a positive integer')
        return owner_user_id
    value = environment.get('TOFU_MCP_OWNER_ID', '')
    if value.startswith('tofu:') and value[5:].isdigit() and int(value[5:]) > 0:
        return int(value[5:])
    return None


def _private_root() -> Path:
    uid = os.getuid() if hasattr(os, 'getuid') else None
    root = Path(tempfile.gettempdir()) / (
        _DIRECTORY_PREFIX if uid is None else f'{_DIRECTORY_PREFIX}-{uid}')
    root.mkdir(mode=0o700, exist_ok=True)
    status = os.lstat(root)
    if not stat.S_ISDIR(status.st_mode) or (
            uid is not None and (status.st_uid != uid or stat.S_IMODE(status.st_mode) & 0o077)):
        raise OSError(f'Hope queue shim directory {root} is not private to this user')
    return root


# The last search: (PATH, cwd, directory witnesses, result). A command runs
# with the same PATH as the previous one; looking ``hope``/``mlp`` up in every
# PATH directory costs a lookup per entry (a missing name is a slow negative
# lookup on a network volume, and each releases the GIL), while statting the
# directories is answered from attribute caches. One entry: bounded, exact.
_LAST_SEARCH: tuple | None = None


def _directory_witnesses(search_path: str, cwd: Optional[str]) -> tuple | None:
    """Each searched directory's identity and mtime; None when one is racy.

    A directory's mtime changes whenever an entry is added, removed or renamed
    in it, so equal witnesses prove the same names resolve the same way. As in
    Git's racy-clean rule, a directory modified in the current second may
    change again within the same timestamp and is not trusted.
    """
    import time
    now_second = time.time_ns() // 1_000_000_000
    witnesses = []
    for entry in search_path.split(os.pathsep):
        directory = entry if os.path.isabs(entry) or cwd is None else os.path.join(cwd, entry)
        try:
            status = os.stat(directory)
        except OSError:
            witnesses.append((directory, None))
            continue
        if status.st_mtime_ns // 1_000_000_000 >= now_second:
            return None
        witnesses.append((directory, (status.st_dev, status.st_ino, status.st_mtime_ns)))
    return tuple(witnesses)


def _real_commands(environment: dict[str, str], cwd: Optional[str]) -> dict[str, str]:
    global _LAST_SEARCH
    from lib.tools.process_environment import resolve_executable, validate_environment
    search = dict(environment)
    search['PATH'] = os.pathsep.join(
        entry for entry in environment.get('PATH', os.defpath).split(os.pathsep)
        if _DIRECTORY_PREFIX not in entry)
    validate_environment(search)
    witnesses = _directory_witnesses(search['PATH'], cwd)
    last = _LAST_SEARCH
    if (witnesses is not None and last is not None and last[0] == search['PATH']
            and last[1] == cwd and last[2] == witnesses
            and all(os.access(path, os.X_OK) for path in last[3].values())):
        return dict(last[3])
    result = {}
    for name in _COMMANDS:
        resolved = resolve_executable(name, search, cwd=cwd)
        if resolved:
            result[name] = resolved
    if witnesses is not None:
        _LAST_SEARCH = (search['PATH'], cwd, witnesses, dict(result))
    return result


def _write_launcher(path: Path, body: str) -> None:
    if path.exists() and path.read_text(encoding='utf-8') == body:
        return
    # Concurrent commands prepare the same directory; each writer needs its own
    # temporary file, or one's rename consumes the other's (ENOENT).
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(body)
        os.chmod(temporary, 0o700)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def prepare_shim_directory(environment: dict[str, str], *, owner_user_id: Optional[int],
                           project_path: Optional[str], cwd: Optional[str] = None) -> Optional[str]:
    """Create/reuse the launchers for this owner/project and child PATH.

    Returns None when neither CLI is installed or no owner is identified. Any
    helper/pinning failure is raised instead of silently disabling the guard.
    """
    owner = _owner_id(environment, owner_user_id)
    if owner is None:
        return None
    real = _real_commands(environment, cwd)
    if not real:
        return None
    helper = _SHIM_SOURCE.path()
    policy = policy_path(owner)
    project = project_path or ''
    identity = json.dumps({
        'helper': helper, 'interpreter': sys.executable, 'policy': policy,
        'project': project, 'real': real,
    }, sort_keys=True, ensure_ascii=False).encode('utf-8')
    directory = _private_root() / hashlib.sha256(identity).hexdigest()[:32]
    directory.mkdir(mode=0o700, exist_ok=True)
    python = shlex.quote(os.path.abspath(sys.executable))
    for name, executable in real.items():
        body = _LAUNCHER.format(
            python=python, helper=shlex.quote(helper), policy=shlex.quote(policy),
            project=shlex.quote(project), real=shlex.quote(executable))
        _write_launcher(directory / name, body)
    return str(directory)


def apply_hope_queue_policy(environment: dict[str, str], *, owner_user_id: Optional[int] = None,
                            project_path: Optional[str] = None, cwd: Optional[str] = None) -> None:
    """Bind the policy path and prepend the guard, preserving all other tools."""
    owner = _owner_id(environment, owner_user_id)
    if owner is None:
        return
    environment[POLICY_ENV] = policy_path(owner)
    directory = prepare_shim_directory(
        environment, owner_user_id=owner, project_path=project_path, cwd=cwd)
    if directory:
        existing = [entry for entry in environment.get('PATH', os.defpath).split(os.pathsep)
                    if entry != directory]
        environment['PATH'] = os.pathsep.join([directory, *existing])
