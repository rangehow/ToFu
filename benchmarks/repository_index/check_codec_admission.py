"""Changed tag schema before first witnessed caching must never lose fields."""
from dataclasses import dataclass
from pathlib import Path
import tempfile
from unittest.mock import patch

from bootstrap_epoch_task_measure import create_bootstrap_group
from budget import CacheBudgetPlan
from index import outline, capture_entry
from quota import CacheReservationPool

OWNER = 900001


def check():
    @dataclass(frozen=True)
    class ExtendedTag(outline.Tag):
        provenance: str = 'default'
    def changed(path, text):
        return [ExtendedTag(path, 'symbol', 1, 'def', 'def symbol():', 0, 'observed provenance')]
    original = outline.Tag('module.py', 'symbol', 1, 'def', 'def symbol():', 0)
    saved = capture_entry('module.py', b'digest', True, True, [original], [])
    with tempfile.TemporaryDirectory(prefix='tofu-codec-admission-') as folder:
        root = Path(folder); (root/'module.py').write_text('def symbol():\n    return 1\n')
        pool = CacheReservationPool(CacheBudgetPlan(2*1024*1024, 1024*1024, 2, 'fixture'))
        with patch.object(outline, 'Tag', ExtendedTag), patch.object(outline, '_collect_source_tags', changed):
            for operation in (saved.restore,
                              lambda: capture_entry('module.py', b'digest', True, True, changed('module.py', ''), [])):
                try:
                    operation()
                except RuntimeError as error:
                    assert 'schema' in str(error) and 'codec' in str(error)
                else:
                    raise AssertionError('direct codec silently accepted a changed schema')
            group = create_bootstrap_group(pool, OWNER, 'codec-admission-fixture', [root], epoch_source=lambda root: 0)
            try:
                first = group.scan(OWNER, root)
                assert first.tags['module.py'][0].provenance == 'observed provenance'
                assert group.retained_bytes(OWNER) == 0
                try:
                    group.scan(OWNER, root)
                except RuntimeError as error:
                    assert 'codec' in str(error) and 'schema' in str(error)
                else:
                    raise AssertionError('unsupported first observed schema admitted into stale codec')
                assert group.retained_bytes(OWNER) == 0
            finally:
                group.close(OWNER)
        assert pool.snapshot()['reserved_bytes'] == 0
    print('uncached changed-schema output retains field; first byte-cache admission fails closed; quota released')


if __name__ == '__main__':
    check()
