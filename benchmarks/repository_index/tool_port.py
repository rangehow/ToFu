"""Private index port through actual repository API and tool handler.

The execution caller supplies an explicit authorized root/scope and finalizer.
No production module globals, tool registry or task state are installed here.
"""
import inspect
import os
from types import FunctionType

from index import outline
from lib.repo_map import api
from lib.tasks_pkg.handlers import project


def clone(function, namespace):
    result = FunctionType(function.__code__, namespace, argdefs=function.__defaults__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


def handler_template():
    source = inspect.getsource(project._handle_project_tool)
    boundary = '            from lib.repo_map.api import generate_repo_map'
    assert source.count(boundary) == 1, 'repository handler import seam changed'
    source = source.replace(boundary, '            generate_repo_map = _bound_generate_repo_map')
    namespace = dict(project._handle_project_tool.__globals__)
    exec(compile(source, '<repository-tool-index-port>', 'exec'), namespace)
    return namespace['_handle_project_tool']


class RepositoryToolPort:
    def __init__(self, root, scope=None, *, template=None):
        self.root = os.path.abspath(root)
        self.scope = scope
        self.template = handler_template() if template is None else template

    def invoke(self, task, arguments, finalize, *, project_root=None):
        owner = task.get('user_id')
        def scan(root, **options):
            if os.path.abspath(root) != self.root:
                raise PermissionError('repository index belongs to another project root')
            if self.scope is None:
                return outline.scan(root, **options)
            return self.scope.scan(owner, **options)
        generate = clone(api.generate_repo_map, dict(api.generate_repo_map.__globals__, scan=scan))
        handler = clone(self.template, dict(self.template.__globals__,
                       _bound_generate_repo_map=generate, _finalize_tool_round=finalize))
        return handler(task, {}, 'repo_map', 'repository-port-call', arguments,
                       1, {}, None, self.root if project_root is None else project_root, True)
