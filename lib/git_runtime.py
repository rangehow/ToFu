"""Resolve a runnable Git and the environment of every model- and harness-run Git.

Entry points: git_executable, without_automatic_git_maintenance,
beyond_git_ceiling (Git's discovery ceiling for Tofu's own ``.git`` walkers) and
GIT_STALL_SECONDS, the liveness bound of supervised harness Git. Probe PATH
candidates before any repository command; never retry a failed write or change
the process environment. Cache successful selection by PATH and loader
environment; failed probes remain retryable. Explicit repository maintenance is
scripts/git_maintenance.py (docs/runbooks/GIT_WORKSPACE_GUARD.md).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

# Metadata-only commands identify their inputs explicitly. Starting their child
# at the filesystem root avoids cwd resolution on the project's mounted tree.
GIT_METADATA_CWD = os.path.abspath(os.sep)

# GIT_CONFIG_PARAMETERS outranks repository configuration and legacy Git
# binaries read it too; an explicit ``git -c`` in the command still wins.
AUTOMATIC_GIT_MAINTENANCE_OFF = ("'gc.auto=0' 'maintenance.auto=false' "
                                 "'gc.autoDetach=false' 'maintenance.autoDetach=false'")

# Fixed-class liveness bound (contracts/resource_policy_v1.json): constant,
# never derived from hardware, tree size or command. On the shared checkout
# volume a Git command waits as long as the volume does, whether it reads one
# ref or hashes the whole tree, so harness Git that admission depends on runs
# under lib/project_mod/activity_supervised_process.py with no total deadline.
# The window renews whenever any thread of the Git process tree executes: it
# ends only a command blocked on an unresponsive volume, never a slow one.
GIT_STALL_SECONDS = 90.0


def without_automatic_git_maintenance(environment: dict[str, str]) -> dict[str, str]:
    """Return a copy in which no Git command starts automatic maintenance.

    Commit, merge, rebase, am and fetch otherwise start ``gc --auto``. Detached,
    it escapes the command's process group with inherited workspace leases; in
    the foreground it is unbounded hidden work inside the command, rerun by
    every later writer while Git cannot bring its object counts below the
    threshold. Explicit opt-ins (``git -c gc.auto=N``) stay in the foreground.
    """
    result = dict(environment)
    parameters = result.get('GIT_CONFIG_PARAMETERS', '').strip()
    if not parameters.endswith(AUTOMATIC_GIT_MAINTENANCE_OFF):
        result['GIT_CONFIG_PARAMETERS'] = (
            parameters + ' ' + AUTOMATIC_GIT_MAINTENANCE_OFF).strip()
    return result


def beyond_git_ceiling(start, root) -> bool:
    """Whether Git, searching upward from directory START, never reaches ROOT.

    Git never moves up into a ``GIT_CEILING_DIRECTORIES`` entry that is a
    proper ancestor of START (START itself is always searched). It ignores
    relative entries and compares entries after an empty one without resolving
    symlinks. Tofu's own ``.git`` walkers apply this rule to the checkout they
    find, so a stray repository above a ceiling is never theirs either.
    """
    entries = os.environ.get('GIT_CEILING_DIRECTORIES', '')
    if not entries:
        return False
    start, root = os.path.realpath(start), os.path.realpath(root)
    resolve = True
    for entry in entries.split(os.pathsep):
        if not entry:
            resolve = False
        elif os.path.isabs(entry):
            ceiling = os.path.realpath(entry) if resolve else os.path.normpath(entry)
            if start != ceiling and _within(start, ceiling) and _within(ceiling, root):
                return True
    return False


def _within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


class GitExecutableUnavailable(RuntimeError):
    """No PATH candidate completed a valid Git version probe."""


def _supervised_probe(executable: str) -> subprocess.CompletedProcess:
    # A binary's shared libraries may be on the same slow volume as a checkout.
    # The fixed 5 s subprocess.run deadline rejected a healthy Git under host
    # load; admission uses the shared stall window.
    from lib.project_mod.activity_supervised_process import run_activity_supervised
    from lib.tools.process_environment import inherited_child_environment
    return run_activity_supervised(
        [executable, '--version'], cwd=GIT_METADATA_CWD, env=inherited_child_environment(),
        stall_seconds=GIT_STALL_SECONDS)


_RESOLVED_GITS: dict[tuple[str, str, str], str] = {}
_RESOLVED_GITS_LOCK = threading.Lock()


def git_executable() -> str:
    key = (os.environ.get('PATH', os.defpath),
           os.environ.get('LD_LIBRARY_PATH', ''),
           os.environ.get('LD_PRELOAD', ''))
    with _RESOLVED_GITS_LOCK:
        selected = _RESOLVED_GITS.get(key)
        if selected is not None:
            _RESOLVED_GITS[key] = _RESOLVED_GITS.pop(key)
            return selected
    # Never hold the cache lock across process or filesystem work: one slow
    # probe must not block an unrelated loader/PATH environment.
    selected = _resolve(*key)
    with _RESOLVED_GITS_LOCK:
        if len(_RESOLVED_GITS) >= 16:
            _RESOLVED_GITS.pop(next(iter(_RESOLVED_GITS)))
        _RESOLVED_GITS[key] = selected
    return selected


def _resolve(path: str, library_path: str, preload: str) -> str:
    failures = []
    seen = set()
    for directory in [*path.split(os.pathsep), *os.defpath.split(os.pathsep)]:
        # Internal operations must not select a cwd-dependent executable.
        if not os.path.isabs(directory):
            continue
        candidate = shutil.which('git', path=directory)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = _supervised_probe(candidate)
            if result.returncode == 0 and result.stdout.startswith('git version '):
                return candidate
            detail = (result.stderr or result.stdout).strip()[:300]
        except (OSError, RuntimeError) as error:
            detail = str(error)[:300]
        failures.append(f'{candidate}: {detail}')
    raise GitExecutableUnavailable('No runnable Git executable: ' + ('; '.join(failures)[:1000]
                                   or 'Git is not installed on PATH'))
