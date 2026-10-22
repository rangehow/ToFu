"""Experimental owner/root-scoped content-witness cache for canonical extraction.

Reads every selected source each scan. Full normalized text SHA-256, admission
and reference mode control reuse; mtime/size alone never authorize a hit.
"""
from collections import OrderedDict
from operator import attrgetter
from dataclasses import fields, dataclass
import hashlib
import json
import os
import sys
from threading import RLock
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from lib.repo_map import outline
TAG_FIELDS=tuple(field.name for field in fields(outline.Tag))
TAG_VALUES=attrgetter(*TAG_FIELDS)


def verify_tag_codec_schema():
    """A fresh loaded witness cannot expand this already-built byte codec."""
    if tuple(field.name for field in fields(outline.Tag)) != TAG_FIELDS:
        raise RuntimeError('tag schema changed; rebuild byte codec/owner before cache admission or restore')

@dataclass(frozen=True, slots=True)
class Entry:
    digest: bytes
    admitted: bool
    include_refs: bool
    payload: bytes
    charge: int

    def restore(self):
        verify_tag_codec_schema()
        tags, identifiers = json.loads(self.payload)
        return [outline.Tag(*values) for values in tags], frozenset(identifiers)


def capture_entry(relative, digest, admitted, include_refs, tags, identifiers):
    verify_tag_codec_schema()
    # ASCII escaping preserves even filesystem surrogate escapes. Builtin JSON
    # retains arbitrary-size integer fields, unlike fixed-width native codecs.
    payload = json.dumps((tuple(TAG_VALUES(tag) for tag in tags), tuple(identifiers)),
                         ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    entry = Entry(digest, admitted, include_refs, payload, 0)
    charge = (256 + sys.getsizeof(relative) + sys.getsizeof(digest)
              + sys.getsizeof(payload) + sys.getsizeof(entry))
    object.__setattr__(entry, 'charge', charge)
    return entry

class Index:
    def __init__(self,owner_id,root,maximum_bytes):
        if type(owner_id) is not int or owner_id<1 or type(maximum_bytes) is not int or maximum_bytes<1:
            raise ValueError('explicit integer owner and positive integer cache budget required')
        self.owner_id=owner_id;self.root=os.path.abspath(root);self.maximum_bytes=maximum_bytes
        self._lock=RLock()
        self.close()
    def close(self):
        """Wait for active scans and clear reusable experimental cache state."""
        with self._lock:
            self.entries=OrderedDict();self.retained_bytes=0;self.hits=0;self.misses=0
            self._extractor_epoch=None
    def remove(self,path):
        with self._lock:
            previous=self.entries.pop(path,None)
            if previous:self.retained_bytes-=previous.charge
    def scan(self,owner_id,path='',focus='',include_refs=True,include_tests=False,*,extractor_epoch=0):
        if type(owner_id) is not int or owner_id!=self.owner_id:raise PermissionError('foreign repository index owner')
        if type(extractor_epoch) is not int or extractor_epoch<0:
            raise ValueError('extractor epoch must be an explicit nonnegative integer')
        with self._lock:
            if self._extractor_epoch!=extractor_epoch:
                self.close()
                self._extractor_epoch=extractor_epoch
            return self._scan(path,focus,include_refs,include_tests)
    def _scan(self,path,focus,include_refs,include_tests):
        verify_tag_codec_schema()
        all_files=outline.list_files(self.root)
        present=set(all_files)
        for old in tuple(self.entries):
            if old not in present:self.remove(old)
        files=all_files
        if path:
            prefix=path.strip('/').rstrip('/')+'/'
            files=[item for item in files if item.startswith(prefix) or item==prefix.rstrip('/')]
        if focus:files=[item for item in files if focus.lower() in item.lower()]
        result=outline.RepoMap(root=self.root,files=files)
        self.hits=0;self.misses=0
        for relative in files:
            if not outline._is_code_path(relative) or outline._is_generated(relative):continue
            if not include_tests and outline._is_test_path(relative):continue
            source=outline._read_code_source(self.root,relative)
            if source is None:self.remove(relative);continue
            text,admitted=source
            digest=hashlib.sha256(text.encode('utf-8')).digest()
            previous=self.entries.get(relative)
            if previous and (previous.digest,previous.admitted,previous.include_refs)==(digest,admitted,include_refs):
                tags, identifiers=previous.restore()
                self.entries.move_to_end(relative);self.hits+=1
            else:
                tags=outline._collect_source_tags(relative,text) if admitted else []
                identifiers=frozenset(outline.extract_identifiers(text[:outline._MAX_REF_BYTES])) if include_refs else frozenset()
                entry=capture_entry(relative,digest,admitted,include_refs,tags,identifiers)
                charge=entry.charge
                self.remove(relative);self.misses+=1
                # Empty non-Python extraction may be a transient grammar failure.
                cacheable=relative.endswith('.py') or bool(tags) or not admitted
                if cacheable and charge<=self.maximum_bytes:
                    while self.entries and self.retained_bytes+charge>self.maximum_bytes:
                        self.remove(next(iter(self.entries)))
                    self.entries[relative]=entry
                    self.retained_bytes+=charge
            for tag in tags:result.add_tag(tag)
            if include_refs:result.add_references(relative,identifiers)
        return result
