"""Actual public handler/API injection; only publication remains a fixture port.

No extractor globals, API clones or inline-import source rewrites. The recording
finalizer is privately bound in the handler namespace, so this is not durable
publication or a registered application cache. Caller owns scope authorization.
"""
from task_port import TaskRepositoryToolPort
from tool_port import handler_template


class PublicTaskRepositoryToolPort(TaskRepositoryToolPort):
    def __init__(self, root, scope=None, *, template=None):
        if template is not None:
            raise ValueError('public handler port does not accept rewritten templates')
        super().__init__(root, scope)

    def handler_for_call(self):
        return handler_template()
