"""Experimental loaded-extractor/config/package witness to a monotonic epoch.

Observes local facts, never invents package versions. No production registry is
installed. Opaque grammar state, mutable closures and concurrent reload still
require an authoritative producer/admission fence before adoption.
"""
from dataclasses import fields
import hashlib
import importlib.metadata
import json
import marshal
from threading import RLock

from index import outline

FUNCTIONS=('_is_generated','_extract_python_tags','_extract_treesitter_tags',
    '_collect_tree_sitter_tags','_code_extensions','_is_code_path','_read_code_source',
    '_collect_source_tags','collect_tags','extract_identifiers','list_files','_walk_files','scan','_is_test_path')
SETS=('SKIP_EXTENSIONS','STOP_WORDS','_TEST_DIR_NAMES')
PACKAGES=('grep-ast','tree-sitter','tree-sitter-language-pack')


def loaded_identity():
    functions={}
    for name in FUNCTIONS:
        function=getattr(outline,name)
        if not hasattr(function,'__code__'):
            raise TypeError('extractor witness requires a Python function: '+name)
        functions[name]={'object_identity':id(function),
                         # v3/v4 reference flags depend on transient refcounts;
                         # v2 encodes code consistently before/after execution.
                         'code_sha256':hashlib.sha256(marshal.dumps(function.__code__,2)).hexdigest()}
    versions={}
    for package in PACKAGES:
        try:versions[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:versions[package]=None
    return {'functions':functions,'packages':versions,
        'sets':{name:sorted(getattr(outline,name)) for name in SETS},
        'limits':{name:getattr(outline,name) for name in ('_MAX_SOURCE_BYTES','_MAX_REF_BYTES')},
        'generated_markers':list(outline._GENERATED_MARKERS),
        'regexes':{name:{'pattern':getattr(outline,name).pattern,'flags':getattr(outline,name).flags}
                   for name in ('_WORD_RE','_IDENT_RE')},
        'active_extensions':sorted(outline._code_extensions()),
        'tag_schema':[field.name for field in fields(outline.Tag)]}


class LoadedExtractionEpoch:
    def __init__(self,*,identity_source=loaded_identity):
        if not callable(identity_source):raise TypeError('explicit identity source required')
        self._identity_source=identity_source
        self._lock=RLock();self._digest=None;self._epoch=0;self._tag_schema=None

    def __call__(self):
        with self._lock:
            identity=self._identity_source()
            schema=tuple(identity['tag_schema'])
            if self._tag_schema is not None and schema!=self._tag_schema:
                raise RuntimeError('tag schema changed; rebuild cache codec/owner before reuse')
            digest=hashlib.sha256(json.dumps(identity,sort_keys=True,allow_nan=False).encode()).hexdigest()
            if self._digest is not None and digest!=self._digest:self._epoch+=1
            self._tag_schema=schema;self._digest=digest
            return self._epoch
