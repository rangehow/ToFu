"""Answer worktree-registry questions from Git's registry files, not a full listing.

Entry points: ``main_worktree``, ``registered_worktrees``, ``is_registered`` and
``worktree_with_branch``. Dependencies: the standard library only.

``git worktree list --porcelain`` also resolves the HEAD, lock and prune state
of every registered checkout. On the shared repository (509 registrations on
2026-11-11) one listing made 5,455 stat and 2,247 open calls and took 1.5-2.7 s
on DolphinFS (65 s under load), and each retired-checkout check and landing
paid for one or more listings to learn one path.
These functions read only the registry files that answer the question, by
Git's own rules (``worktree.c`` ``get_main_worktree`` and ``get_linked_worktree``
in Git 2.34; layout in gitrepository-layout(5), files ref backend):

- the main worktree is the real path of the common directory without a
  trailing ``/.git``. Without that suffix (bare, a separate Git directory or a
  submodule's ``.git/modules/<name>``) Git reports the common directory itself,
  which is no checkout: ``has_main_checkout`` tells the two apart;
- a linked checkout is the content of ``worktrees/<id>/gitdir``, right-trimmed
  and without a trailing ``/.git``. A relative content (``worktree.
  useRelativePaths``) is relative to that ``worktrees/<id>`` directory; an
  empty or unreadable file is no registration, as in Git;
- a checkout is on branch REF when its ``HEAD`` file is the symbolic ref
  ``ref: REF``. Git would also follow a symbolic branch ref to its target;
  Tofu creates none, and such a chain is reported under the name HEAD names.

Nothing is cached across calls: every answer reads the registry as it is now.
"""
from __future__ import annotations

import os
from pathlib import Path

_DOT_GIT = '/.git'


def main_worktree(common_dir: str | os.PathLike) -> Path:
    """The main worktree path ``git worktree list`` reports first."""
    common = os.path.realpath(common_dir)
    return Path(common[:-len(_DOT_GIT)] if common.endswith(_DOT_GIT) else common)


def has_main_checkout(common_dir: str | os.PathLike) -> bool:
    """Whether the main worktree is a checkout whose ``.git`` is COMMON_DIR."""
    return os.path.realpath(common_dir).endswith(_DOT_GIT)


def _admin_dirs(common_dir: str | os.PathLike) -> list[Path]:
    # Only a missing registry is empty; other read errors surface.
    try:
        with os.scandir(os.path.join(common_dir, 'worktrees')) as entries:
            return [Path(entry.path) for entry in entries]
    except (FileNotFoundError, NotADirectoryError):
        return []


def _recorded_checkout(admin: Path) -> Path | None:
    try:
        content = (admin / 'gitdir').read_bytes()
    except OSError:
        return None
    recorded = os.fsdecode(content).rstrip()
    if recorded.endswith(_DOT_GIT):
        recorded = recorded[:-len(_DOT_GIT)]
    if not recorded:
        return None
    if os.path.isabs(recorded):
        return Path(recorded)
    return Path(os.path.realpath(os.path.join(admin, recorded)))


def _git_path_order(path: Path) -> bytes:
    # `git worktree list` sorts linked worktrees by path (pathsort, strcmp).
    return os.fsencode(path)


def registered_worktrees(common_dir: str | os.PathLike) -> list[Path]:
    """Every worktree ``git worktree list`` reports, in its order and as it reports it."""
    linked = (_recorded_checkout(admin) for admin in _admin_dirs(common_dir))
    return [main_worktree(common_dir),
            *sorted((path for path in linked if path is not None), key=_git_path_order)]


def is_registered(common_dir: str | os.PathLike, path: str | os.PathLike) -> bool:
    """Whether a registered worktree resolves to PATH (``Path.resolve`` equality).

    Git records real paths, but a component may become a symlink later (a moved
    data root). Each distinct parent directory is resolved and listed once, so
    the cost follows the number of distinct parents, not of registrations.
    """
    target = os.path.realpath(path)
    parents: dict[str, tuple[str, set[str]]] = {}
    for registered in registered_worktrees(common_dir):
        recorded = str(registered)
        parent, name = os.path.split(recorded)
        if not parent or name in ('', '.', '..'):
            resolved = os.path.realpath(recorded)
        else:
            if parent not in parents:
                parents[parent] = (os.path.realpath(parent), _symlink_names(parent))
            real_parent, links = parents[parent]
            resolved = (os.path.realpath(recorded) if name in links
                        else os.path.join(real_parent, name))
        if resolved == target:
            return True
    return False


def _symlink_names(directory: str) -> set[str]:
    try:
        with os.scandir(directory) as entries:
            return {entry.name for entry in entries if entry.is_symlink()}
    except (FileNotFoundError, NotADirectoryError):
        return set()


def _symbolic_ref(head: Path) -> str | None:
    try:
        text = os.fsdecode(head.read_bytes())
    except OSError:
        return None
    return text[len('ref:'):].strip() if text.startswith('ref:') else None


def worktree_with_branch(common_dir: str | os.PathLike, ref: str) -> Path | None:
    """The checkout whose HEAD is on branch REF, as ``git worktree list`` reports it.

    A main worktree without a checkout (see ``has_main_checkout``) is never
    reported: Git prints no branch for a bare one, and a separate Git
    directory holds no files to land into or recycle.
    """
    if has_main_checkout(common_dir) and _symbolic_ref(Path(common_dir, 'HEAD')) == ref:
        return main_worktree(common_dir)
    # Only a forced checkout puts one branch in two worktrees; then the first
    # in Git's listing order wins, as it did for a scan of the listing.
    matches = (_recorded_checkout(admin) for admin in _admin_dirs(common_dir)
               if _symbolic_ref(admin / 'HEAD') == ref)
    return min((path for path in matches if path is not None), key=_git_path_order,
               default=None)
