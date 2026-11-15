"""Experimental actual tool port using the canonical task ownership boundary.

The component fixture keeps its explicit alias. Real task principal validation
uses manager.task_user_id; no user_id alias or module-global patch is introduced.
"""
from lib.tasks_pkg.manager import task_user_id
from tool_port import RepositoryToolPort


class TaskRepositoryToolPort(RepositoryToolPort):
    def owner_for_task(self, task):
        return task_user_id(task)
