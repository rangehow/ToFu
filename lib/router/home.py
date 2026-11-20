"""Owner-scoped router home: the Git repository the Global Router coordinates.

Responsibility: locate and idempotently create the owner's router home under
Tofu's canonical writable data directory (``lib.runtime.runtime_paths``). The
home is an ordinary local Git repository with at least one commit, so the
existing project guards accept it as a project root and Workers of the Global
Router run in isolated worktrees of it, exactly like project Workers.

Entry points: ``router_home_location(user_id)`` (pure path, no effects),
``router_home_path(user_id)`` (creates and returns the home),
``is_router_home(user_id, path)`` and ``owner_resources(user_id)`` (the
owner's shared resource registry, docs/ROUTER.md R13).

Creation writes README.md, .gitignore, the write-once ``RESOURCES.md``
seed (a bundle's ``router_home/RESOURCES.md``, else an empty registry; never
secrets) and the write-once ``HOPE_QUEUE_POLICY.json`` seed (the contract's
fail-safe default, R16), runs ``git init`` (which makes the
home its own repository top level even when the data directory lies inside a
source checkout), then reuses ``prepare_managed_repository`` for the baseline
commit, generated AGENTS.md guidance and shared-data policy. Existing files and
history are never modified. Dependencies: runtime_paths, git_runtime,
git_checkpoint_policy, integration_control (per-root lock),
project_mod.managed_repository.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

ROUTER_HOME_DIRECTORY = 'router'
_README = '''# Global Router home

Tofu created this repository for one owner's Global Router (docs/ROUTER.md).
Workers that the router delegates for non-project work, such as summaries,
drafts and investigations, run in isolated worktrees of this repository, and
their reviewed results are published here. Projects keep their own
repositories; the router reaches them through their Project Routers.

Do not store credentials here.

## Owner resource registry

`RESOURCES.md` is the owner-wide registry of GPUs, queues, keys and storage
the owner can lend a project (docs/ROUTER.md R13). Leads read it through
`list_project_leads`; a Router-home Worker reviews and publishes each update.
It names resources and how to obtain them — never a secret value.
'''
_GITIGNORE = '''# Tofu Global Router home. Runtime, dependency and cache trees are never versioned.
.tofu*/
node_modules/
__pycache__/
.venv/
'''


_RESOURCES = '''# Owner resource registry

GPUs, queues, keys and storage the owner can lend a project. Leads read this
file through `list_project_leads` (R13); a Router-home Worker reviews and
publishes every update, including this seed. **Never store a secret here** —
name the resource and how to obtain it, never its value.

Every row names its source, when it was read and how. Re-read a source before
planning work: capacity and authorization change without notice, and a dated
observation is not a grant.

No resources are recorded yet. To add one, delegate a Router-home Worker: it
edits this file with the resource, its source and read date, and how to
obtain it, and its reviewed publication lands here.
'''


class RouterHomeUnavailable(RuntimeError):
    """The router home could not be prepared; the message states the remedy."""


class RouterGitUnavailable(RouterHomeUnavailable):
    """Git or its storage is temporarily unavailable; the ensure may be retried."""


def router_home_root() -> str:
    """Directory that holds every owner's router home."""
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), ROUTER_HOME_DIRECTORY)


def router_home_location(user_id: int) -> str:
    """Canonical (real, normalized) home path of one owner; creates nothing."""
    owner = require_user_id(user_id, context='router home')
    return normalize_project_path(os.path.realpath(os.path.join(router_home_root(), str(owner))))


def is_router_home(user_id: int, path: str) -> bool:
    """Whether ``path`` is exactly this owner's router home identity."""
    if not isinstance(path, str) or not path.strip():
        return False
    return normalize_project_path(path.strip()) == router_home_location(user_id)


def owner_resources(user_id: int, *, content: bool) -> dict:
    """The owner-wide resource registry: ``RESOURCES.md`` at the router home root.

    Returns ``{path, available}`` plus ``bytes`` (and the text with
    ``content``), or ``error`` when a file is there but unreadable. Read
    without following a link out of the home (a link or non-regular file is
    refused); creates nothing.
    """
    from lib.conversations.project_messages import CROSS_PROJECT
    path = os.path.join(router_home_location(user_id), CROSS_PROJECT['resources']['file'])
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    except FileNotFoundError:
        return {'path': path, 'available': False}
    except OSError as error:
        return {'path': path, 'available': False,
                'error': f'{type(error).__name__}: {error.strerror or error} (a link is never followed)'}
    with os.fdopen(descriptor, 'rb') as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            return {'path': path, 'available': False, 'error': 'Not a regular file'}
        view = {'path': path, 'available': True, 'bytes': status.st_size}
        if content:
            view['content'] = stream.read().decode('utf-8', errors='replace')
    return view


RESOURCES_BUNDLE_KIND = 'router_home'


def _resources_seed() -> str:
    """The registry seed: the last bundle's ``router_home/RESOURCES.md``, else the empty core seed.

    Which resources an owner can lend is deployment data, so a release bundle
    supplies it (docs/PUBLIC_CORE.md D2); the core seed names none.
    """
    from tofu_extensions import bundle_directories
    for directory in reversed(bundle_directories(RESOURCES_BUNDLE_KIND)):
        seed = directory / 'RESOURCES.md'
        if seed.is_file():
            return seed.read_text(encoding='utf-8')
    return _RESOURCES


def _hope_queue_policy_seed() -> str:
    """The R16 policy seed: exactly the contract's fail-safe default (the global m17 ban).

    The guards enforce that default when the file is missing, so seeding it
    changes no decision; it makes the owner's source exist where RESOURCES.md
    and the guards name it, ready for the Global Router's reviewed edits.
    """
    import json
    from lib.tools.hope_queue_contract import schema
    return json.dumps(schema()['x-fail-safe-default'], indent=2, ensure_ascii=False) + '\n'


def _seed_owner_files(home: str) -> None:
    """Write-once owner sources a home must hold; owner or reviewed-Worker edits win."""
    from lib.tools.hope_queue_policy import POLICY_FILENAME
    _write_once(os.path.join(home, 'RESOURCES.md'), _resources_seed())
    _write_once(os.path.join(home, POLICY_FILENAME), _hope_queue_policy_seed())


def _write_once(path: str, content: str) -> None:
    if os.path.lexists(path):
        return
    directory = os.path.dirname(path)
    handle, temporary = tempfile.mkstemp(prefix='.tofu_router_', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(content)
        # A concurrent creator writes identical bytes; whichever lands first wins.
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _git(home: str, *args: str) -> subprocess.CompletedProcess:
    """Local metadata commands only; the baseline commit runs in the managed owner."""
    from lib.git_checkpoint_policy import git_policy_environment
    from lib.git_runtime import GIT_STALL_SECONDS, GitExecutableUnavailable, git_executable
    from lib.project_mod.activity_supervised_process import (
        ProcessRetirementUnproven, ProcessStalled, run_activity_supervised)
    try:
        return run_activity_supervised(
            [git_executable(), '-c', 'core.fsmonitor=false', '-C', home, *args],
            cwd=home, env=git_policy_environment(), stall_seconds=GIT_STALL_SECONDS)
    except (GitExecutableUnavailable, OSError, ProcessStalled, ProcessRetirementUnproven) as error:
        logger.warning('[RouterHome] Git command unavailable', exc_info=True)
        raise RouterGitUnavailable(
            'Git or its filesystem is temporarily unavailable. Retry opening the main coordinator.') from error


def _has_commit(home: str) -> bool:
    return _git(home, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}').returncode == 0


def router_home_path(user_id: int) -> str:
    """Create (idempotently) and return the owner's router home repository."""
    home = router_home_location(user_id)
    os.makedirs(home, exist_ok=True)
    if os.path.isdir(os.path.join(home, '.git')) and _has_commit(home):
        # R13: a home created before the registry seed must not point leads at a
        # missing RESOURCES.md. Write-once; the owner or a reviewed Worker may
        # edit it afterwards and nothing here overwrites it. The same holds for the
        # R16 HOPE_QUEUE_POLICY.json, which a home recreated on a new host lacked.
        _seed_owner_files(home)
        return home
    # The same cross-process per-root lock as managed repository preparation:
    # concurrent `git init` runs race on template and config lock files.
    from lib.integration_control import _repo_lock
    with _repo_lock(home):
        _write_once(os.path.join(home, 'README.md'), _README)
        _write_once(os.path.join(home, '.gitignore'), _GITIGNORE)
        _seed_owner_files(home)
        if not os.path.lexists(os.path.join(home, '.git')):
            initialized = _git(home, 'init', '-q')
            if initialized.returncode:
                raise RouterHomeUnavailable(
                    f'Cannot initialize the router home {home}: {initialized.stderr.strip()}')
            _git(home, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    from lib.project_mod.managed_repository import (
        ManagedRepositoryUnavailable, prepare_managed_repository,
    )
    try:
        prepare_managed_repository(home, refresh=False)
    except ManagedRepositoryUnavailable as error:
        if error.code == 'volume_stalled':
            logger.warning('[RouterHome] Git preparation stalled', exc_info=True)
            raise RouterGitUnavailable(
                'The main coordinator workspace filesystem stopped responding. Retry shortly.') from error
        raise RouterHomeUnavailable(f'{error.reason} {error.remedy}') from error
    if not _has_commit(home):
        raise RouterHomeUnavailable(f'The router home {home} has no baseline commit.')
    return home


def router_home_owners() -> list[int]:
    """Owners that have a router home, ascending; creates nothing.

    A deployment-wide fault such as a storage fence concerns every owner with
    a Global Router, and their homes are where those owners are recorded.
    """
    root = router_home_root()
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    return sorted(int(name) for name in names
                  if name.isdigit() and int(name) > 0 and os.path.isdir(os.path.join(root, name)))


__all__ = ['ROUTER_HOME_DIRECTORY', 'RouterGitUnavailable', 'RouterHomeUnavailable', 'is_router_home',
           'owner_resources', 'router_home_location', 'router_home_owners', 'router_home_path',
           'router_home_root']
