"""Recording adapter through the public repository scanner parameter.

The execution caller supplies an explicit authorized root/scope and finalizer.
Only the finalizer is privately bound; handler/API code and scanner selection
stay canonical. The component fixture uses user_id; task ports override that
identity boundary. No production globals, registry or task state are installed.
"""
import os
from types import FunctionType

from index import outline
from lib.tasks_pkg.handlers import project


def _bind_finalizer(handler, finalize):
    bound = FunctionType(handler.__code__, dict(handler.__globals__,
                         _finalize_tool_round=finalize), argdefs=handler.__defaults__)
    bound.__kwdefaults__ = handler.__kwdefaults__
    return bound


def handler_template():
    """Freeze the actual handler for a measurement cohort without rewriting it."""
    return project._handle_project_tool


class RepositoryToolPort:
    def __init__(self, root, scope=None, *, template=None):
        self.root = os.path.abspath(root)
        self.scope = scope
        self.template = handler_template() if template is None else template

    def owner_for_task(self, task):
        """Explicit component-fixture identity; real tasks use the task subclass."""
        return task.get('user_id')

    def handler_for_call(self):
        return self.template

    def invoke(self, task, arguments, finalize, *, project_root=None):
        owner = self.owner_for_task(task)
        def scan(root, **options):
            if os.path.abspath(root) != self.root:
                raise PermissionError('repository index belongs to another project root')
            if self.scope is None:
                return outline.scan(root, **options)
            return self.scope.scan(owner, **options)
        handler = _bind_finalizer(self.handler_for_call(), finalize)
        return handler(task, {}, 'repo_map', 'repository-port-call', arguments,
                       1, {}, None, self.root if project_root is None else project_root, True,
                       repository_scanner=scan)
