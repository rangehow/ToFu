"""Experimental actual tool port using the canonical task ownership boundary.

Keep the running component benchmark unchanged. Real task principal validation
uses manager.task_user_id; no user_id alias or module-global patch is introduced.
"""
import inspect
from types import MethodType
import textwrap

from lib.tasks_pkg.manager import task_user_id
from tool_port import RepositoryToolPort


class TaskRepositoryToolPort(RepositoryToolPort):
    def __init__(self, root, scope=None, *, template=None):
        super().__init__(root, scope, template=template)
        source = inspect.getsource(RepositoryToolPort.invoke)
        boundary = "        owner = task.get('user_id')"
        assert source.count(boundary) == 1, 'component ownership seam changed'
        source = source.replace(boundary, '        owner = task_user_id(task)')
        namespace = dict(RepositoryToolPort.invoke.__globals__, task_user_id=task_user_id)
        exec(compile(textwrap.dedent(source), '<canonical-task-index-port>', 'exec'), namespace)
        self.invoke = MethodType(namespace['invoke'], self)
