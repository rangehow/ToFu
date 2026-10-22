"""Packed cache fidelity, detached restores and conservative byte accounting."""
from pathlib import Path
import sys
import tempfile

from index import Index, capture_entry, outline, TAG_VALUES


def main():
    tags = [outline.Tag('odd\udcff.py', '中文', 2**130, 'def', 'def 中文():', -2),
            outline.Tag('normal.py', 'empty', 1, 'var', '', 0)]
    identifiers = frozenset(('repeat', '中文', 'odd\udcff'))
    entry = capture_entry('odd\udcff.py', b'digest', True, True, tags, identifiers)
    restored, names = entry.restore()
    assert restored == tags and names == identifiers
    assert entry.charge >= sum(sys.getsizeof(value) for value in (entry, entry.payload, entry.digest))
    tags[0].__dict__['name'] = 'producer mutation'
    restored[0].__dict__['signature'] = 'caller mutation'
    again, _ = entry.restore()
    assert again[0].name == '中文' and again[0].signature == 'def 中文():'
    assert entry.restore()[0][0] is not again[0]
    with tempfile.TemporaryDirectory(prefix='tofu-packed-index-') as directory:
        root = Path(directory)
        (root/'odd\udcff.py').write_text('def unicode_name():\n    return 1\n')
        (root/'normal.py').write_text('def admitted_name():\n    return 1\n')
        index = Index(900001, directory, 1024*1024)
        try:
            assert index.scan(900001) == outline.scan(directory)
            assert index.scan(900001) == outline.scan(directory)
            assert index.hits == 1
            assert index.retained_bytes == sum(value.charge for value in index.entries.values())
            assert index.retained_bytes <= index.maximum_bytes
        finally:
            index.close()
        assert index.retained_bytes == 0
    # Repeated identifier/string occurrences in the tuple representation are
    # charged per occurrence; the byte representation has explicit residency.
    values = [outline.Tag('module.py', f'name{i}', i+1, 'def', f'def name{i}():', 0)
              for i in range(100)]
    frozen = tuple(TAG_VALUES(tag) for tag in values)
    old_charge = (256+sys.getsizeof('module.py')+sys.getsizeof(b'digest')
                  +sys.getsizeof(frozen)+sys.getsizeof(identifiers)
                  +sum(sys.getsizeof(row)+sum(map(sys.getsizeof,row)) for row in frozen)
                  +sum(map(sys.getsizeof,identifiers)))
    packed = capture_entry('module.py', b'digest', True, True, values, identifiers)
    assert packed.restore() == (values, identifiers)
    assert packed.charge < old_charge
    print('packed exact fields/large integers/Unicode/surrogate paths/detached restores/bounds pass;'
          f' fixture charged-byte ratio={packed.charge/old_charge:.3f}; not process RSS or performance admission')


if __name__ == '__main__':
    main()
