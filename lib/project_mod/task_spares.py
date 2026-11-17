"""Spare task checkouts: prepared local checkouts that make a task workspace instant.

Responsibility
    Creating a task workspace never checks out a whole tree on the task's
    critical path. Each canonical repository keeps prepared checkouts
    ("spares") on the placed local workspace root, beside its task checkouts:
    ``<workspace root>/<repository key>/.spare-<creation time>-<id>``. A new
    task workspace takes a spare with an atomic rename (one already at its
    input revision when there is one) and brings it to that revision, which
    writes only the files that differ. Retired
    task checkouts (delivered or discarded, with everything they hold saved in
    Git) are recycled into spares instead of being deleted, so the pool follows
    demand: after tasks ran side by side, about as many spares wait for the next
    such burst, and their space is reused rather than grown. When the volume
    nears its placement reserve, the oldest spares are removed first and a
    retired checkout is removed instead of recycled. Spares an earlier
    placement left in another local root are removed by the repository's next
    maintenance pass, since no claim or eviction reaches them there.

    Spares follow the repository's HEAD. Each claim, each delegation and each
    delivery commit starts one background maintenance pass per repository: it
    prepares a spare at HEAD when none is there, then refreshes the stale ones
    in place, always while another spare stays claimable, so a task never
    finds the pool empty because of maintenance. A spare is complete before it
    gets its ``.spare-`` name. One pass at a time per repository holds the
    persistent ``.preparation.lock`` and works under a ``.preparing-`` name: it
    checks the spare out (or updates it), refreshes its index so no entry is
    racily clean (a claim then compares stat data instead of rehashing the
    tree), and renames it. Any ``.preparing-`` entry the lock holder finds was
    left by a pass that died, and is removed.

    Each pool names its repository in ``.repository``. Spares live on the local
    volume across restarts, so a new process refreshes every named pool in the
    background (``warm``) before its first task asks.

Entry points
    ``available``, ``take``, ``ensure``, ``prepare``, ``recycle``, ``make_room``,
    ``warm``.

Dependencies: ``lib.integration_control`` (Git runner, checkout
materialization, controlled removal, placement), ``lib.project_mod.lanes``
(atomic checkout rename), ``serverctl_pkg.local_placement`` (free space).
Contract: docs/modules/project_integration.md, "Task checkouts come from
prepared spares".
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from lib.git_runtime import GIT_STALL_SECONDS
from lib.log import get_logger

logger = get_logger(__name__)

SPARE_PREFIX = '.spare-'
PREPARING_PREFIX = '.preparing-'
_PREPARATION_LOCK = '.preparation.lock'
_REPOSITORY_MARKER = '.repository'
# Git keeps these in a checkout's administrative directory while an operation
# is unfinished; such a checkout is never handed to another task.
_UNFINISHED_OPERATIONS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG',
                          'rebase-merge', 'rebase-apply')

_PREPARING: set[str] = set()
_PREPARING_GUARD = threading.Lock()


def pool(root: Path) -> Path | None:
    """ROOT's directory of task checkouts and spares, or None when checkouts use the data root."""
    from lib import integration_control as ic
    local = ic._placed_local_root()
    return None if local is None else local / ic._repository_key(root)


def scratch_root() -> Path:
    """The directory holding every pooled task checkout's command scratch."""
    from serverctl_pkg.local_placement import temporary_root
    return Path(temporary_root()) / f'tofu-{getattr(os, "getuid", lambda: 0)()}-tmp'


def _scratch_path(checkout: Path) -> Path | None:
    checkout = Path(checkout)
    owner = checkout.parent
    # The layout test runs first: only a pooled checkout needs the server's
    # placement, which the storage-free agent wheel does not ship.
    if not owner.name.startswith('u') or not owner.name[1:].isdigit():
        return None
    from lib import integration_control as ic
    local = ic._placed_local_root()
    if local is None or os.path.realpath(owner.parent.parent) != os.path.realpath(local):
        return None
    identity = os.path.join(os.path.realpath(owner), checkout.name)
    return scratch_root() / hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]


def checkout_scratch(checkout: Path) -> str | None:
    """The temporary directory of commands run in a pooled task CHECKOUT, created on demand.

    Workers' scratch (``mktemp``, ``tempfile``, build and test temporaries)
    otherwise landed in the shared ``/tmp`` or beside the checkouts with no
    owner. It lives outside the tree Git captures, in the private
    :func:`scratch_root` under a digest of the checkout's path, and goes when
    the checkout retires (:func:`retire_scratch`), i.e. when its assignment is
    delivered or discarded. None for any other directory.

    The path stays short because programs bind AF_UNIX sockets under
    ``$TMPDIR`` and ``sun_path`` holds 108 bytes: Chromium aborts with
    "Socket path too long" at ``$TMPDIR/org.chromium.Chromium.XXXXXX/
    SingletonSocket`` once ``$TMPDIR`` exceeds about 60 characters, which a
    directory beside the checkout always did.
    """
    scratch = _scratch_path(checkout)
    if scratch is None:
        return None
    from serverctl_pkg.local_placement import ensure_private_directory
    try:
        ensure_private_directory(scratch.parent)
        scratch.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return None
    return str(scratch)


def retire_scratch(checkout: Path) -> None:
    """Remove a retired CHECKOUT's command scratch; called once the checkout is gone."""
    scratch = _scratch_path(checkout)
    if scratch is not None and not Path(checkout).exists():
        shutil.rmtree(scratch, ignore_errors=True)


def _spares(directory: Path) -> list[Path]:
    """DIRECTORY's spares, oldest first: a spare's name begins with its creation time."""
    try:
        return sorted(entry for entry in directory.iterdir()
                      if entry.name.startswith(SPARE_PREFIX))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _name() -> str:
    return f'{time.time_ns():020d}-{uuid.uuid4().hex[:8]}'


def _head(spare: Path) -> str:
    """The commit SPARE's detached HEAD names, read from its administrative directory."""
    admin = _administrative_directory(spare)
    try:
        value = (admin / 'HEAD').read_text(encoding='ascii').strip() if admin else ''
    except (OSError, UnicodeError):
        return ''
    return value if all(char in '0123456789abcdef' for char in value) else ''


def _update(checkout: Path, revision: str, *, cancel=None, progress=None) -> None:
    """Bring a settled checkout to REVISION; Git rewrites only the files that differ."""
    from lib import integration_control as ic
    ic._git(checkout, ['checkout', '--force', '--detach', '-q', revision], check=True,
            stall_seconds=GIT_STALL_SECONDS, cancel=cancel, progress=progress)


def available(root: Path, destination: Path | None = None) -> bool:
    """Whether a spare of ROOT is ready (one that can move to DESTINATION, when given)."""
    directory = pool(root)
    if directory is None or (destination is not None
                             and not destination.is_relative_to(directory)):
        return False
    return bool(_spares(directory))


def _claim(spare: Path, destination: Path) -> bool:
    from lib.project_mod.lanes import LaneError, move_checkout
    try:
        return move_checkout(spare, destination)
    except (FileNotFoundError, NotADirectoryError):
        return False  # another task took it first
    except (LaneError, OSError) as error:
        logger.warning('[TaskSpares] spare %s not taken: %s', spare, error)
        return False


def take(root: Path, destination: Path, revision: str, *, cancel=None,
         progress=None) -> bool:
    """Move a spare of ROOT to DESTINATION at REVISION; False when none was taken.

    A spare already at REVISION is taken first, else the newest. The rename is
    the claim, so concurrent takers never share a spare. A taken spare that
    cannot reach REVISION is removed before the error propagates. ``progress()``
    runs at every observed advance of that update, as for a checkout.
    """
    from lib import integration_control as ic
    from lib.observability import measure_task_startup
    directory = pool(root)
    if directory is None or not destination.is_relative_to(directory):
        return False
    with measure_task_startup('workspace_spare'):
        # With the parent in place, a vanished source is the only lost rename.
        destination.parent.mkdir(parents=True, exist_ok=True)
        for spare in sorted(_spares(directory), reverse=True,
                            key=lambda spare: (_head(spare) == revision, spare.name)):
            if not _claim(spare, destination):
                continue
            try:
                if ic._rev(destination, 'HEAD') != revision:
                    _update(destination, revision, cancel=cancel, progress=progress)
            except BaseException:
                ic._remove_controlled_worktree(root, destination, lifecycle_owned=True)
                raise
            return True
    return False


def ensure(root: Path) -> bool:
    """Start a background pass bringing ROOT's spares to its HEAD, unless one runs here."""
    directory = pool(root)
    if directory is None:
        return False
    key = str(directory)
    with _PREPARING_GUARD:
        if key in _PREPARING:
            return False
        _PREPARING.add(key)

    def run():
        try:
            logger.info('[TaskSpares] spares of %s: %s', root, prepare(root))
        except Exception as error:
            logger.warning('[TaskSpares] spare maintenance for %s failed: %s', root, error)
        finally:
            with _PREPARING_GUARD:
                _PREPARING.discard(key)

    _start(run, f'tofu-task-spare-{directory.name}')
    return True


def _start(target, name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def _name_repository(directory: Path, root: Path) -> None:
    """Record ROOT in its pool DIRECTORY, so a new process can warm the pool."""
    marker = directory / _REPOSITORY_MARKER
    try:
        if marker.read_text(encoding='utf-8') == str(root):
            return
    except OSError:
        pass
    staged = directory / f'{_REPOSITORY_MARKER}.{uuid.uuid4().hex[:8]}'
    staged.write_text(str(root), encoding='utf-8')
    os.replace(staged, marker)


def warm() -> None:
    """In the background, bring every named pool's spares to its repository's HEAD."""
    _start(_warm, 'tofu-task-spare-warmup')


def _warm() -> None:
    from lib import integration_control as ic
    local = ic._placed_local_root()
    if local is None:
        return
    try:
        directories = [entry for entry in local.iterdir() if entry.is_dir()]
    except FileNotFoundError:
        return
    for directory in directories:
        try:
            root = Path((directory / _REPOSITORY_MARKER).read_text(encoding='utf-8'))
        except OSError:
            continue
        if root.is_dir() and ic._repository_key(root) == directory.name:
            ensure(root)


def _settle(checkout: Path) -> None:
    """Record fresh stat data for every index entry.

    Entries Git wrote in the same instant as the index are racily clean, and
    every later read would rehash those files; refreshing once here lets a
    claim compare stat data only.
    """
    from lib import integration_control as ic
    ic._git(checkout, ['update-index', '-q', '--refresh'], stall_seconds=GIT_STALL_SECONDS)


def prepare(root: Path) -> str:
    """Bring ROOT's spares to its current HEAD.

    A spare at HEAD is created when none is there and the only spare is stale
    or missing (a stale one stays claimable meanwhile); every other stale spare
    is updated in place while another stays claimable. Returns ``ready`` (a
    spare was created), ``refreshed``, ``present`` (all spares at HEAD),
    ``underway`` (another pass holds the lock), ``no-space`` (the placement
    reserve would be crossed even after removing older spares), ``unborn`` (ROOT
    has no commit) or ``unplaced`` (task checkouts use the data root).
    """
    from lib import integration_control as ic
    directory = pool(root)
    if directory is None:
        return 'unplaced'
    _retire_stranded(root, directory)
    directory.mkdir(parents=True, exist_ok=True)
    _name_repository(directory, root)
    # A persistent inode: unlinking a lock file a waiter may hold races it.
    with open(directory / _PREPARATION_LOCK, 'a+b') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'underway'
        for abandoned in sorted(directory.glob(f'{PREPARING_PREFIX}*')):
            ic._remove_controlled_worktree(root, abandoned, lifecycle_owned=True)
        head = ic._rev(root, 'HEAD')
        if not head:
            return 'unborn'
        spares = _spares(directory)
        stale = [spare for spare in spares if _head(spare) != head]
        outcome = 'present'
        if len(stale) == len(spares) <= 1:
            outcome = _create(root, directory, head)
            if outcome != 'ready':
                return outcome
        for spare in stale:
            if _refresh(root, directory, spare, head):
                outcome = 'ready' if outcome == 'ready' else 'refreshed'
    return outcome


def _retire_stranded(root: Path, directory: Path) -> None:
    """Remove ROOT's spares that an earlier placement left outside DIRECTORY.

    Only the placed pool hands out spares, and ``make_room`` reclaims only the
    placed root, so a spare in another local root's pool would hold its volume
    for good (the root overlay, once task checkouts moved to the local temporary
    disk on 2026-11-12). Spares hold no task's work. A pool whose preparation
    lock is held is left to a later pass; a spare being claimed or removed
    keeps its destination lock and is left too.
    """
    from lib import integration_control as ic
    current = Path(os.path.realpath(directory))
    for local in ic._local_workspace_roots():
        stranded = local / ic._repository_key(root)
        if Path(os.path.realpath(stranded)) == current or not stranded.is_dir():
            continue
        try:
            lock = open(stranded / _PREPARATION_LOCK, 'a+b')
        except OSError:
            continue
        with lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            for abandoned in sorted(stranded.glob(f'{PREPARING_PREFIX}*')):
                ic._remove_controlled_worktree(root, abandoned, lifecycle_owned=True)
            for spare in _spares(stranded):
                ic._remove_controlled_worktree(root, spare)
                logger.info('[TaskSpares] removed spare %s an earlier placement left', spare)


def _create(root: Path, directory: Path, head: str) -> str:
    from lib import integration_control as ic
    from lib.project_mod.lanes import move_checkout
    from serverctl_pkg.local_placement import has_free_space
    size, inodes = ic._checkout_estimate(root, head)
    if (not has_free_space(directory, size, required_inodes=inodes)
            and not make_room(size, inodes)):
        return 'no-space'
    name = _name()
    preparing = directory / f'{PREPARING_PREFIX}{name}'
    try:
        ic._materialize_worktree(root, preparing, head)
        _settle(preparing)
        if not move_checkout(preparing, directory / f'{SPARE_PREFIX}{name}'):
            return 'lost'
    except BaseException:
        ic._remove_controlled_worktree(root, preparing, lifecycle_owned=True)
        raise
    return 'ready'


def _refresh(root: Path, directory: Path, spare: Path, head: str) -> bool:
    """Update one stale spare to HEAD out of claimers' sight; False when a task took it."""
    from lib import integration_control as ic
    from lib.project_mod.lanes import move_checkout
    name = _name()
    refreshing = directory / f'{PREPARING_PREFIX}{name}'
    if not _claim(spare, refreshing):
        return False
    try:
        _update(refreshing, head)
        _settle(refreshing)
        return move_checkout(refreshing, directory / f'{SPARE_PREFIX}{name}')
    except BaseException:
        ic._remove_controlled_worktree(root, refreshing, lifecycle_owned=True)
        raise


def _administrative_directory(checkout: Path) -> Path | None:
    try:
        marker = (checkout / '.git').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None
    if not marker.startswith('gitdir:'):
        return None
    admin = Path(marker[len('gitdir:'):].strip())
    admin = admin if admin.is_absolute() else (checkout / admin).resolve()
    return admin if admin.is_dir() else None


def recycle(root: Path, checkout: Path, *, lifecycle_owned: bool = False) -> bool:
    """Turn a retired task checkout of ROOT into a spare; False leaves it to the caller.

    The caller has proven that Git holds everything CHECKOUT contains. The
    checkout returns to ROOT's HEAD with nothing untracked or ignored left:
    dependency bridges, caches, shared-data links and its FileHistory store go
    (owner decision 2026-11-12), so the next task starts clean. Only a checkout
    in ROOT's pool, with no unfinished Git operation, is recycled, and only
    while the volume stays above its reserve once older spares made room.
    ``lifecycle_owned`` means the caller already holds its destination
    admission; otherwise a busy checkout is left alone, as removal would.
    """
    from lib import integration_control as ic
    from lib.project_mod.lanes import move_checkout
    from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_claims_lease
    from serverctl_pkg.local_placement import has_free_space
    directory = pool(root)
    if (directory is None or not checkout.is_dir()
            or Path(os.path.realpath(checkout)).parent.parent
            != Path(os.path.realpath(directory))):
        return False
    admin = _administrative_directory(checkout)
    if admin is None or any((admin / marker).exists() for marker in _UNFINISHED_OPERATIONS):
        return False
    if not has_free_space(directory) and not make_room():
        return False
    admission = (contextlib.nullcontext() if lifecycle_owned
                 else ic._destination_lock(checkout, wait=False))
    try:
        with admission, workspace_claims_lease([('tree', checkout)], timeout=0.0):
            ic._populate_checkout(checkout, ic._rev(root, 'HEAD'))
            ic._git(checkout, ['clean', '-ffdxq'], check=True, stall_seconds=GIT_STALL_SECONDS)
            _settle(checkout)
            _name_repository(directory, root)
            return move_checkout(checkout, directory / f'{SPARE_PREFIX}{_name()}')
    except (BlockingIOError, WorkspaceBusyError):
        return False


def _canonical_root(spare: Path) -> Path | None:
    admin = _administrative_directory(spare)
    if admin is None or admin.parent.name != 'worktrees' or admin.parent.parent.name != '.git':
        return None
    return admin.parent.parent.parent


def make_room(required_bytes: int = 0, required_inodes: int = 0) -> bool:
    """Remove spares, oldest first across repositories, until the placed root has room.

    Spares are reconstructible and hold no task's work, so they are the first
    space given back when the volume nears its placement reserve.
    """
    from lib import integration_control as ic
    from serverctl_pkg.local_placement import has_free_space
    local = ic._placed_local_root()
    if local is None:
        return False

    def fits() -> bool:
        return has_free_space(local, required_bytes, required_inodes=required_inodes)

    try:
        repositories = [entry for entry in local.iterdir() if entry.is_dir()]
    except FileNotFoundError:
        return fits()
    spares = sorted((spare for repository in repositories for spare in _spares(repository)),
                    key=lambda spare: spare.name)
    for spare in spares:
        if fits():
            return True
        canonical = _canonical_root(spare)
        if canonical is None:
            shutil.rmtree(spare, ignore_errors=True)  # Git no longer knows it.
        else:
            ic._remove_controlled_worktree(canonical, spare)
        logger.info('[TaskSpares] removed spare %s to keep the placement reserve', spare)
    return fits()
