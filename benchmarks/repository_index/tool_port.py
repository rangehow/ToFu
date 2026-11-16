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


def _rebind(function, namespace):
    bound = FunctionType(function.__code__, namespace, argdefs=function.__defaults__,
                         closure=function.__closure__)
    bound.__kwdefaults__ = function.__kwdefaults__
    return bound


def _bind_finalizer(handler, finalize):
    """The handler, and each same-module helper it delegates settlement to
    (``_execute_repo_map``), resolve ``_finalize_tool_round`` to ``finalize``."""
    module_globals = handler.__globals__
    namespace = dict(module_globals, _finalize_tool_round=finalize)
    for name, value in module_globals.items():
        if (isinstance(value, FunctionType) and value is not handler
                and value.__globals__ is module_globals
                and '_finalize_tool_round' in value.__code__.co_names):
            namespace[name] = _rebind(value, namespace)
    return _rebind(handler, namespace)


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
