"""ChatUI adapter for the agent-core post-settlement port."""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def settle_project_task(task: dict, project_path: str, *, user_id: int) -> None:
    """Delegate a settled task to the project coordination owner.

    A Turn the Global Router started to repair a project (docs/ROUTER.md R9
    ``project_repair``) also tells that Router its outcome, once.
    """
    from lib.conversations.project_settlement import on_project_task_settled
    from lib.router.project_repair import REPAIR_MARKER, deliver_repair_outcome

    on_project_task_settled(task, project_path, user_id=int(user_id))
    marker = ((task or {}).get('config') or {}).get(REPAIR_MARKER)
    if isinstance(marker, dict):
        try:
            deliver_repair_outcome(task, marker, user_id=int(user_id))
        except Exception:
            logger.warning('[ProjectSettlement] repair outcome notice failed conv=%s',
                           (task or {}).get('convId'), exc_info=True)


__all__ = ["settle_project_task"]
