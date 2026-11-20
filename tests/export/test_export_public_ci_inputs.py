"""Public developer commands and shipped tests retain their exact inputs."""
from pathlib import Path

import pytest

pytest.importorskip('export', reason='private export tooling')

pytestmark = pytest.mark.unit


def test_public_ci_helpers_ship_without_broadening_private_exclusions():
    from export_pkg._excludes import _should_exclude
    from export_pkg._lists import ROOT, _OPENSOURCE_KEEP_FILES

    for relative in ('scripts/verify.py', 'scripts/cache_usage_window.py',
                     'scripts/install_document_tex_packages.py'):
        assert (ROOT / relative).is_file()
        assert relative in _OPENSOURCE_KEEP_FILES
    for relative in ('export.py', 'export_pkg/_lists.py', 'JOURNAL.md',
                     'docs/PUBLICATION_AUDIT.md', 'scripts/secret_fingerprint_scan.py'):
        assert relative not in _OPENSOURCE_KEEP_FILES
        assert _should_exclude(relative, Path(relative).name, 'opensource')
