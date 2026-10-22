"""Canonical scans across edits, deletion, filters, mutation, read faults and eviction."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from index import Index,outline

def check():
    for invalid in (False,True,0,1.5,'owner'):
        try:Index(invalid,'.',1024)
        except ValueError:pass
        else:raise AssertionError('invalid owner admitted')
    with tempfile.TemporaryDirectory(prefix='tofu-index-check-') as folder:
        root=Path(folder);(root/'tests').mkdir()
        (root/'tests/a.py').write_text('def test_symbol():\n    return original()\n')
        source=root/'a.py';source.write_text('def original():\n    return 1\n')
        cache=Index(900001,str(root),1024*1024)
        def verify(**options):
            actual=cache.scan(900001,**options);expected=outline.scan(str(root),**options)
            assert actual==expected,(actual,expected)
            return actual
        verify();verify();assert cache.hits==1
        original_extractor=outline._collect_source_tags
        def changed_extractor(relative,text):
            return [outline.Tag(relative,'changedBackend',1,'def','def changedBackend():',0)]
        with patch.object(outline,'_collect_source_tags',new=changed_extractor):
            assert cache.scan(900001,extractor_epoch=1)==outline.scan(str(root))
            assert cache.misses==1
            assert cache.scan(900001,extractor_epoch=1)==outline.scan(str(root))
            assert cache.hits==1
        verify();assert cache.misses==1
        retained=cache.retained_bytes
        try:cache.scan(900002,extractor_epoch=2)
        except PermissionError:pass
        else:raise AssertionError('foreign owner authorized epoch change')
        assert cache.retained_bytes==retained
        for invalid in (-1,True,'epoch'):
            try:cache.scan(900001,extractor_epoch=invalid)
            except ValueError:pass
            else:raise AssertionError('invalid extractor epoch accepted')
        before=source.stat()
        source.write_text('def changedx():\n    return 2\n')
        os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns))
        verify();assert cache.misses==1
        returned=verify()
        returned.tags['a.py'][0].__dict__['name']='caller_mutation'
        verify()
        returned.tags.clear();returned.references.clear();verify()
        for options in ({'include_tests':True},{'include_refs':False},{'path':'tests'},{'focus':'a.py'}):verify(**options)
        with patch.object(outline,'_read_code_source',return_value=None):
            verify();assert 'a.py' not in cache.entries
            verify(include_tests=True);assert not cache.entries
        verify();source.unlink();verify();assert 'a.py' not in cache.entries
        source.write_text('def recover():\n    return 3\n');verify()
        script=root/'script.js';script.write_text('function jsSymbol() { return recover(); }\n')
        rust=root/'library.rs';rust.write_text('fn rustSymbol() -> i32 { 3 }\n')
        verify();verify()
        script.rename(root/'renamed.js');verify()
        rust.write_text('fn changedRustSymbol() -> i32 { 4 }\n');verify()
        cache.close()
        with patch.object(outline,'_extract_treesitter_tags',return_value=[]):
            verify();assert 'renamed.js' not in cache.entries and 'library.rs' not in cache.entries
        verify()
        try:cache.scan(900002)
        except PermissionError:pass
        else:raise AssertionError('foreign owner authorized')
        cache.close();assert cache.retained_bytes==0
        entered=Event();release=Event();close_started=Event();close_finished=Event()
        def blocked_extract(relative,text):
            entered.set();assert release.wait(10)
            return original_extractor(relative,text)
        def clear_cache():
            close_started.set();cache.close();close_finished.set()
        with patch.object(outline,'_collect_source_tags',new=blocked_extract), ThreadPoolExecutor(max_workers=2) as pool:
            scan_future=pool.submit(cache.scan,900001)
            assert entered.wait(10)
            clear_future=pool.submit(clear_cache)
            try:
                assert close_started.wait(10)
                assert not close_finished.wait(.05),'clear returned while scan still owns cache'
            finally:release.set()
            scan_future.result(timeout=10);clear_future.result(timeout=10)
        assert cache.retained_bytes==0 and not cache.entries
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:cache.scan(900001),range(32)))
        assert all(result==outline.scan(str(root)) for result in results)
        assert cache.retained_bytes<=cache.maximum_bytes
        cache=Index(900001,str(root),1);verify();assert not cache.entries
    print('content/timestamp edits, filters, read failure, typed ownership, epochs, 32 concurrent scans, blocking clear and bounded teardown passed')

if __name__=='__main__':check()
