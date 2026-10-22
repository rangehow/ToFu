"""Actual public handler/API injection; only publication remains a fixture port.

No extractor globals, API clones or inline-import source rewrites. The recording
finalizer is privately bound in the handler namespace, so this is not durable
publication or a registered application cache. Caller owns scope authorization.
"""
import os
from types import FunctionType

from index import outline
from lib.tasks_pkg.handlers import project
from lib.tasks_pkg.manager import task_user_id


class PublicTaskRepositoryToolPort:
    def __init__(self, root, scope=None, *, template=None):
        if template is not None:
            raise ValueError('public handler port does not accept rewritten templates')
        self.root = os.path.abspath(root)
        self.scope = scope

    def invoke(self, task, arguments, finalize, *, project_root=None):
        owner = task_user_id(task)
        def scanner(root, **options):
            if os.path.abspath(root) != self.root:
                raise PermissionError('repository index belongs to another project root')
            if self.scope is None:
                return outline.scan(root, **options)
            return self.scope.scan(owner, **options)
        original = project._handle_project_tool
        handler = FunctionType(original.__code__, dict(original.__globals__,
                               _finalize_tool_round=finalize), argdefs=original.__defaults__)
        handler.__kwdefaults__ = original.__kwdefaults__
        return handler(task, {}, 'repo_map', 'repository-port-call', arguments,
                       1, {}, None, self.root if project_root is None else project_root, True,
                       repository_scanner=scanner)
