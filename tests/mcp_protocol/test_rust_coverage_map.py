"""Every tests/mcp_protocol oracle file has a Rust-backed equivalent.

The Python MCP client (``lib/mcp``) is the oracle for the Rust port
(``packages/tofu-mcp``, docs/RUST_REWRITE_EVIDENCE.md "Phase 5 progress").
Each ``test_rust_*.py`` suite in this directory declares a literal ``COVERS``
dict: an oracle file name maps to the tests that run that file's behaviour
against the Rust client. An entry is either a test function of the same
suite, or ``cargo:<path under packages/tofu-mcp>::<test fn>`` for a Rust test
that needs no Python. This meta test proves that no oracle file is
Python-only, that every named test exists, and that every covering suite
drives the Rust client.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent
CRATE = HERE.parents[1] / 'packages' / 'tofu-mcp'
RUST_TEST = re.compile(r'#\[(?:tokio::)?test(?:\([^)]*\))?\]\s*(?:#\[[^\]]*\]\s*)*(?:async\s+)?fn\s+(\w+)')


def _oracle_files() -> set[str]:
    return {path.name for path in HERE.glob('test_*.py')
            if not path.name.startswith('test_rust_') and path.name != Path(__file__).name}


def _suites() -> dict[str, tuple[ast.Module, dict[str, list[str]]]]:
    suites = {}
    for path in sorted(HERE.glob('test_rust_*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        covers = None
        for node in tree.body:
            target = node.target if isinstance(node, ast.AnnAssign) else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id == 'COVERS' and node.value is not None:
                covers = ast.literal_eval(node.value)
        if covers is not None:
            suites[path.name] = (tree, covers)
    return suites


def _functions(tree: ast.Module) -> set[str]:
    names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return {name for name in names if name.startswith('test_')}


def _rust_tests(relative: str) -> set[str]:
    path = CRATE / relative
    return set(RUST_TEST.findall(path.read_text(encoding='utf-8'))) if path.is_file() else set()


def test_every_oracle_file_is_covered_by_a_rust_backed_test():
    covered: dict[str, list[str]] = {}
    for suite, (_, covers) in _suites().items():
        for oracle, tests in covers.items():
            covered.setdefault(oracle, []).extend(f'{suite}::{test}' for test in tests)
    missing = sorted(name for name in _oracle_files() if not covered.get(name))
    assert not missing, f'Python-only oracle files (no Rust-backed test in any COVERS): {missing}'
    unknown = sorted(set(covered) - _oracle_files())
    assert not unknown, f'COVERS names files that are not oracle files here: {unknown}'


def test_every_covering_test_exists():
    broken = []
    for suite, (tree, covers) in _suites().items():
        functions = _functions(tree)
        for oracle, tests in covers.items():
            for test in tests:
                if test.startswith('cargo:'):
                    relative, _, name = test[len('cargo:'):].partition('::')
                    if name not in _rust_tests(relative):
                        broken.append(f'{suite}: {oracle} -> {test}')
                elif test not in functions:
                    broken.append(f'{suite}: {oracle} -> {test}')
    assert not broken, f'COVERS entries naming no test: {broken}'


def test_every_covering_suite_drives_the_rust_client():
    for suite, (tree, covers) in _suites().items():
        if not covers or all(test.startswith('cargo:') for tests in covers.values() for test in tests):
            continue
        source = ast.unparse(tree)
        assert 'rust_host' in source or 'rust_parity_support' in source, (
            f'{suite} names Python tests in COVERS but never reaches the Rust client')
