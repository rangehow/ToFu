"""Content-addressed test discovery for immutable verification snapshots.

Inputs are a Git revision and its loaded selector. Git enumerates the complete
test inventory; the selector remains the sole import/reference policy. Only
requested, changed blobs are read, in one cat-file transaction. One atomically
replaced cache retains entries for the current inventory, shared across private
checkouts. Cache loss/concurrent replacement costs work, never coverage.

Entry points: build_index, select_paths, measure. Dependencies: standard library, Git,
``lib.tools.process_environment`` for Git's environment.
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import statistics
import subprocess
import sys
import tempfile
import time


# The repository's selector applies the same rule to the working tree
# (scripts/test_select.py build_index; TEST_DIRECTORY in scripts/test_execution.py),
# and test_verification_index.py pins their agreement.
TEST_DIRECTORY = 'tests'


class UnsupportedIndex(ValueError):
    """A selector/filesystem feature needs the selector's ordinary discovery."""


@dataclasses.dataclass
class IndexResult:
    index: dict
    parsed: int
    reused: int
    seconds: float
    cache_error: str = ''


def select_paths(inventory, paths=None) -> list[str]:
    """Resolve complete file/directory scopes; a typo never yields a partial run."""
    wanted = []
    for value in paths or ():
        path = str(PurePosixPath(value))
        if path.startswith('/') or '..' in PurePosixPath(path).parts or '::' in path:
            raise ValueError(f'invalid test path: {value}')
        wanted.append(path)
    selected = sorted(test for test in inventory if not wanted or any(
        path == '.' or test == path or test.startswith(path + '/') for path in wanted))
    missing = [path for path in wanted if not any(
        path == '.' or test == path or test.startswith(path + '/') for test in selected)]
    if missing or not selected:
        raise ValueError('no test files matched requested paths: ' + ', '.join(missing or wanted))
    return selected


def _git(root, *args, input=None):
    from lib.tools.process_environment import inherited_child_environment
    env = {key: value for key, value in inherited_child_environment().items()
           if not key.startswith('GIT_')}
    env.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0')
    return subprocess.run(['git', *args], cwd=root, env=env, input=input,
                          capture_output=True, check=True).stdout


def is_test_root_path(path) -> bool:
    """Whether PATH lies inside a test root: a directory named ``tests``, at any depth."""
    return TEST_DIRECTORY in PurePosixPath(path).parts[:-1]


def test_inventory(root, revision):
    """Every ``test_*.py`` inside a test root, anywhere in the tree.

    The repository suite (``tests/``) and package suites (``packages/*/tests``,
    ``bundles/*/tests`` ...) form one inventory; the repository's execution
    policy runs each from its package directory (``scripts/test_execution.py``).
    """
    entries = {}
    for entry in _git(root, 'ls-tree', '-r', '-z', '--full-tree', revision).split(b'\0'):
        if not entry:
            continue
        meta, raw_path = entry.split(b'\t', 1)
        path = os.fsdecode(raw_path)
        if not (is_test_root_path(path) and PurePosixPath(path).name.startswith('test_')
                and path.endswith('.py')):
            continue
        mode, kind, blob = meta.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise UnsupportedIndex(f'test path requires ordinary discovery: {path}')
        entries[path] = blob.decode('ascii')
    if not entries:
        raise ValueError('test census is empty; check the revision and tests directory')
    return entries


def _sources(root, blobs):
    if not blobs:
        return {}
    output = _git(root, 'cat-file', '--batch', input=('\n'.join(blobs) + '\n').encode('ascii'))
    sources, offset = {}, 0
    for blob in blobs:
        end = output.index(b'\n', offset)
        oid, kind, size = output[offset:end].split()
        start, stop = end + 1, end + 1 + int(size)
        if oid.decode('ascii') != blob or kind != b'blob' or output[stop:stop + 1] != b'\n':
            raise ValueError(f'incomplete Git blob response: {blob}')
        sources[blob] = output[start:stop].decode('utf-8')
        offset = stop + 1
    if offset != len(output):
        raise ValueError('unexpected trailing Git blob response')
    return sources


def _read_cache(path, extractor):
    try:
        saved = json.loads(Path(path).read_text(encoding='utf-8'))
        if (isinstance(saved, dict) and saved.get('version') == 1
                and saved.get('extractor') == extractor and isinstance(saved.get('entries'), dict)
                and saved.get('checksum') == _checksum(saved['entries'])):
            return {name: entry for name, entry in saved['entries'].items()
                    if isinstance(entry, dict) and isinstance(entry.get('blob'), str)
                    and isinstance(entry.get('refs'), list)
                    and all(isinstance(ref, str) for ref in entry['refs'])}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _checksum(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _publish(path, payload):
    temporary = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(payload, stream, separators=(',', ':'))
        os.replace(temporary, path)
        return ''
    except OSError as error:
        return str(error)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def build_index(root, revision, selector, cache_path, paths=None) -> IndexResult:
    """Index exactly PATHS at REVISION, with byte-identical sources parsed once."""
    started = time.perf_counter()
    if not callable(getattr(selector, 'refs_of_test_file', None)):
        raise UnsupportedIndex('selector has no pure reference extractor')
    inventory = test_inventory(root, revision)
    selected = select_paths(inventory, paths)
    extractor = hashlib.sha256(Path(selector.__file__).read_bytes()
                               + sys.implementation.cache_tag.encode()).hexdigest()
    saved = _read_cache(cache_path, extractor) if cache_path else {}
    by_blob = {entry['blob']: entry['refs'] for entry in saved.values()}
    missing = sorted({inventory[path] for path in selected} - by_blob.keys())
    for blob, source in _sources(root, missing).items():
        by_blob[blob] = sorted(selector.refs_of_test_file(source))
    entries = {path: {'blob': blob, 'refs': by_blob[blob]}
               for path, blob in inventory.items() if blob in by_blob}
    error = ''
    if cache_path and entries != saved:
        error = _publish(cache_path, {'version': 1, 'extractor': extractor, 'entries': entries,
                                      'checksum': _checksum(entries)})
    # The selector resolves a source's references at the test's path (a
    # package suite's imports and configuration): never cached by blob.
    resolve = getattr(selector, 'resolve_references', None) or (lambda path, refs: refs)
    return IndexResult({path: {'refs': resolve(path, entries[path]['refs'])} for path in selected},
                       len(missing), len(selected) - len(missing),
                       time.perf_counter() - started, error)


def measure(root, revision='HEAD', repetitions=3):
    """Measure full-reference parity, warm reuse and exact one-file scope on a revision."""
    from lib.project_mod import landing_verification as landing

    head = landing._git(root, 'rev-parse', '--verify', f'{revision}^{{commit}}')
    if head.returncode:
        raise ValueError(head.stderr.strip())
    head = head.stdout.strip()
    timings = {name: [] for name in ('ordinary_cold', 'content_cold', 'content_warm', 'scoped_cold')}
    with landing.merge_checkout(root, head) as checkout, tempfile.TemporaryDirectory() as temporary:
        selector = landing._load_selector(checkout)
        expected = None
        parsed = []
        for repeat in range(repetitions):
            cache = Path(temporary) / f'index-{repeat}.json'
            order = ('ordinary_cold', 'content_cold') if repeat % 2 == 0 else ('content_cold', 'ordinary_cold')
            for kind in order:
                started = time.perf_counter()
                if kind == 'ordinary_cold':
                    index = selector.build_index(str(checkout / 'tests'), None)
                else:
                    result = build_index(root, head, selector, cache)
                    index = result.index
                    parsed.append(result.parsed)
                timings[kind].append(time.perf_counter() - started)
                refs = {path: row['refs'] for path, row in index.items()}
                if expected is None:
                    expected = refs
                if refs != expected:
                    raise AssertionError('index reference sets differ')
            started = time.perf_counter()
            warm = build_index(root, head, selector, cache)
            timings['content_warm'].append(time.perf_counter() - started)
            assert warm.parsed == 0 and {p: row['refs'] for p, row in warm.index.items()} == expected
            first = sorted(expected)[0]
            started = time.perf_counter()
            scoped = build_index(root, head, selector,
                Path(temporary) / f'scoped-{repeat}.json', [first])
            timings['scoped_cold'].append(time.perf_counter() - started)
            assert scoped.index == {first: {'refs': expected[first]}} and scoped.parsed == 1
        return {'revision': head, 'python': sys.version, 'files': len(expected),
                'equivalent_reference_sets': True, 'cold_blobs_parsed': parsed,
                'cache_bytes': cache.stat().st_size,
                'samples_seconds': timings,
                'median_seconds': {name: statistics.median(values) for name, values in timings.items()},
                'scope': 'Index preparation only; scoped mode selects one test file.'}
