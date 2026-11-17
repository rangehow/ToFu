"""The owner's registered projects: the one answer to "is this a registered project root?".

Responsibility: a registered project is a root of one of the owner's project
folders (lib/conversations/folders.py; primary or not, router_project lists
them) or a project with a coordination binding (lib/conversations/
project_coordination.py; get_router_projects lists them). The router home and
the Watchdog are not projects. The recent-project list
(lib/project_mod/config.py, /api/v1/project/recent) is navigation history, never a
registration: every surface that admits a project (channel attachments, the
Feishu bridge) asks here, and scripts/check_architecture.py rejects any other
module reading recent projects to decide one (incident 2026-11-17: channel
attachments read the recent list and refused a project the owner registered
in three folders).

Entry points: registered_project_roots(owner), registered_project(owner, requested).
Dependencies: conversations.folders, conversations.project_coordination,
router.projects (binding pages), router.home, router.watchdog.binding.
"""
from __future__ import annotations

import os

__all__ = ['registered_project', 'registered_project_roots']


def _not_a_project(owner: int, path: str) -> bool:
    from lib.router.home import is_router_home
    from lib.router.watchdog.binding import is_watchdog_location
    return is_router_home(owner, path) or is_watchdog_location(owner, path)


def _folder_roots(owner: int) -> list[str]:
    from lib.conversations.folders import owner_folders
    return [os.path.normpath(path) for folder in owner_folders().list(owner)
            for path in folder['projectPaths'] if isinstance(path, str) and os.path.isabs(path)]


def registered_project_roots(owner: int, *, client=None) -> list[str]:
    """Every registered project root of ``owner``: folder roots in folder order, then bound projects."""
    from lib.router.projects import binding_page
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    roots = _folder_roots(owner)
    cursor = None
    while True:
        _, paths, cursor = binding_page(owner, cursor, client)
        roots.extend(os.path.normpath(path) for path in paths if os.path.isabs(path))
        if cursor is None:
            break
    return [path for path in dict.fromkeys(roots) if not _not_a_project(owner, path)]


def registered_project(owner: int, requested: str) -> str | None:
    """The registered root ``requested`` names exactly (its path, or the same directory), or None.

    An exact folder root or bound project wins over a folder root that is
    only the same directory through a link.
    """
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    wanted = os.path.normpath(os.path.expanduser(requested))
    # '/' has no project identity (normalize_project_path strips it to '').
    if not os.path.isabs(wanted) or wanted == os.sep or _not_a_project(owner, wanted):
        return None
    roots = _folder_roots(owner)
    if wanted in roots or ProjectCoordinationRepository(owner).is_bound(wanted):
        return wanted
    real = os.path.realpath(wanted)
    return next((root for root in roots if os.path.realpath(root) == real), None)
