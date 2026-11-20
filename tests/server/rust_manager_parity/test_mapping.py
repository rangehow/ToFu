"""Phase 2 test-to-implementation mapping stays complete and names real tests.

``mapping.json`` classifies every tracked ``tests/server`` and
``tests/lifecycle_approval`` test file against the Rust lifecycle manager
(``packages/tofu-manager``): which Rust unit tests or parity-harness tests
re-specify it, or which Python owner it still tests. There is no ``gap``
category: every manager behaviour has a parity or Rust equivalent before the
Python manager can be retired. ``parity-harness`` names files that specify the
harness itself rather than a manager behaviour, so they need no parity or Rust
case of their own.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
MAPPING = Path(__file__).with_name('mapping.json')
CATEGORIES = {
    'parity-black-box', 'rust-unit', 'parity-and-rust-unit', 'python-owner-retained',
    'client-unchanged', 'application-owner', 'parity-harness',
}
NEEDS_RUST = {'rust-unit', 'parity-and-rust-unit'}
NEEDS_PARITY = {'parity-black-box', 'parity-and-rust-unit'}
PARITY_DIR = 'tests/server/rust_manager_parity/'


def _mapping() -> dict:
    return json.loads(MAPPING.read_text(encoding='utf-8'))


def _tracked_test_files() -> set[str]:
    listed = subprocess.run(
        ['git', 'ls-files', 'tests/server/test_*.py', 'tests/lifecycle_approval/test_*.py'],
        cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    return set(listed)


def _rust_tests(path: Path) -> set[str]:
    text = path.read_text(encoding='utf-8')
    module = re.search(r'^\s*mod tests\s*\{', text, re.MULTILINE)
    if module is None:
        return set()
    return set(re.findall(r'#\[test\]\s*(?:#\[[^\]]*\]\s*)*fn\s+(\w+)', text[module.end():]))


def _pytest_tests(path: Path) -> set[str]:
    text = path.read_text(encoding='utf-8')
    return set(re.findall(r'^\s*(?:async\s+)?def\s+(test_\w+)', text, re.MULTILINE))


def test_mapping_declares_exactly_the_known_categories():
    mapping = _mapping()
    assert mapping['version'] == 1
    assert set(mapping['categories']) == CATEGORIES


def test_every_tracked_server_and_lifecycle_test_file_is_classified():
    files = _mapping()['files']
    tracked = _tracked_test_files()
    assert tracked, 'git ls-files found no test files'
    assert sorted(tracked - set(files)) == [], 'unclassified test files'
    assert sorted(set(files) - tracked) == [], 'mapping names files that are not tracked'
    for name, entry in files.items():
        assert entry['category'] in CATEGORIES, name
        assert isinstance(entry['rust'], list), name
        assert entry['note'].strip(), name


def test_rust_and_parity_categories_name_their_coverage():
    for name, entry in _mapping()['files'].items():
        targets = entry['rust']
        if entry['category'] in NEEDS_RUST:
            assert any(t.startswith('packages/') for t in targets), name
        if entry['category'] in NEEDS_PARITY:
            assert any(t.startswith(PARITY_DIR) for t in targets), name


def test_every_listed_identifier_exists():
    rust_cache: dict[str, set[str]] = {}
    pytest_cache: dict[str, set[str]] = {}
    missing = []
    for name, entry in _mapping()['files'].items():
        for target in entry['rust']:
            path, _, rest = target.partition('::')
            source = REPO / path
            if path.endswith('.rs'):
                module, _, function = rest.partition('::')
                if module != 'tests' or not source.is_file():
                    missing.append((name, target))
                    continue
                found = rust_cache.setdefault(path, _rust_tests(source))
            elif path.endswith('.py'):
                function = rest
                if not source.is_file():
                    missing.append((name, target))
                    continue
                found = pytest_cache.setdefault(path, _pytest_tests(source))
            else:
                missing.append((name, target))
                continue
            if function not in found:
                missing.append((name, target))
    assert missing == []
