"""Reference/cursor extraction through real transient tool result publication.

Only the baseline extraction/scanner function namespace is privately compiled.
Actual registry handler/API/finalizer remain unchanged. No cache/index admission
or hidden import rewriting. Captured ancestor function and package facts witness
the reference; durable/gateway/full-agent boundaries remain excluded.
"""
from types import FunctionType
from index import outline
from reference_tags import (
    extract_reference,
    REFERENCE_SHA256 as REFERENCE_SHA256,
    REVISION as REVISION,
)
from publication_port import PublishedRepositoryToolPort


def reference_scanner():
    namespace=dict(vars(outline),_extract_treesitter_tags=extract_reference)
    for name in ('_collect_source_tags','collect_tags','scan'):
        original=getattr(outline,name)
        function=FunctionType(original.__code__,namespace,argdefs=original.__defaults__)
        function.__kwdefaults__=original.__kwdefaults__
        namespace[name]=function
    return namespace['scan']


class ExtractionScope:
    def __init__(self,root,scanner):self.root,self.scanner=root,scanner
    def scan(self,owner,**options):
        if type(owner) is not int or owner<=0:raise ValueError('positive extraction caller required')
        return self.scanner(self.root,**options)


class ReferenceExtractionPort(PublishedRepositoryToolPort):
    def __init__(self,root,scope=None,*,template=None):
        if scope is not None:raise ValueError('extraction comparison must not admit a cache')
        super().__init__(root,template=template)
        self.scope=ExtractionScope(self.root,reference_scanner())


class CursorExtractionPort(PublishedRepositoryToolPort):
    def __init__(self,root,scope=None,*,template=None):
        if scope is not None:raise ValueError('extraction comparison must not admit a cache')
        super().__init__(root,template=template)
