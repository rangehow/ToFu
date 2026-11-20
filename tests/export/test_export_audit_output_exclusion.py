#!/usr/bin/env python3
"""The internal figure/table audit is exported nowhere, and only at the root.

Its render PDFs embed the compiling host's absolute path and username in
binary content streams; the sanitizer rewrites text files, not those bytes, so
the post-export secret scan rejects the artifact. The audit is private evidence,
not product source, so the export strips it as a whole. These assertions pin the
anchoring too: a nested directory or file with the same name is product source
and must still ship (the lib/paper drop-bug class).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


def _should_exclude(rel, mode):
    from export import _should_exclude
    return _should_exclude(rel, rel.rsplit('/', 1)[-1], mode)


def test_audit_directory_is_excluded_from_opensource_only():
    for rel in (
        'figure-table-audit/corrected-caption.pdf',
        'figure-table-audit/README.md',
    ):
        assert _should_exclude(rel, 'opensource') is not None, rel
        assert _should_exclude(rel, 'internal') is None, rel
        assert _should_exclude(rel, 'personal') is None, rel


def test_audit_root_bundle_members_are_excluded_from_opensource_only():
    for rel in (
        'figure-table-audit-evidence.zip',
        'figure_table_audit_probe.py',
    ):
        assert _should_exclude(rel, 'opensource') is not None, rel
        assert _should_exclude(rel, 'internal') is None, rel
        assert _should_exclude(rel, 'personal') is None, rel


def test_same_named_paths_elsewhere_are_not_excluded():
    # Same basename, different directory: product source, not audit output.
    for rel in (
        'lib/figure-table-audit/corrected-caption.pdf',
        'docs/figure-table-audit/README.md',
        'tests/fixtures/figure_table_audit_probe.py',
    ):
        assert _should_exclude(rel, 'opensource') is None, rel

    # A nested archive with the same basename is not the root audit bundle.
    assert _should_exclude(
        'lib/artifacts/figure-table-audit-evidence.zip', 'opensource') is None