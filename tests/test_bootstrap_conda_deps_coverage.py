#!/usr/bin/env python3
"""Drift guard for bootstrap.py's conda-forge dependency-repair list.

Tofu has historically maintained THREE parallel dependency lists — install.sh,
bootstrap.py's ``_CONDA_PYTHON_DEPS``, and requirements.txt — and they drift.
install.sh has its own drift guard; bootstrap.py's did not, and its conda list
had already fallen behind: it shipped only transitive ``flask`` (never the
actual ``quart``/``hypercorn`` ASGI stack the server runs on), and lacked
``orjson`` (REQUIRED for the SSE snapshot) and ``sqlalchemy`` (imported
unconditionally in the chat hot-path). On a CentOS-7-class host where the conda
repair path is the ONLY one that works (pip manylinux wheels crash on glibc
2.17), that meant the server could never boot after a dep repair.

This test locks two invariants:
  1. Every boot-critical package (``_CRITICAL_BOOT_PACKAGES``) is present in
     ``_CONDA_PYTHON_DEPS`` — so the conda repair path installs what boot needs.
  2. Every boot-critical package is also DECLARED in requirements.txt — so the
     guard tracks the real requirement, not an invented name, and the two lists
     stay coherent.

Pure text/parse over the repo files; no DB, no network, no conda.
"""
import os
import re
import sys
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip('bootstrap', reason='bootstrap.py is the launcher; import-only guard')

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bare(spec: str) -> str:
    """Strip version specifiers / extras → bare lower-cased package name."""
    return re.split(r'[<>=!~\[ ]', spec, 1)[0].strip().lower()


def _requirements_names() -> set[str]:
    names = set()
    with open(os.path.join(_ROOT, 'requirements.txt'), encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            b = _bare(line)
            if b:
                names.add(b)
    return names


class BootstrapCondaDepsCoverageTest(unittest.TestCase):

    def test_conda_list_covers_boot_critical_packages(self):
        import bootstrap
        conda_bare = {_bare(s) for s in bootstrap._CONDA_PYTHON_DEPS}
        for pkg in bootstrap._CRITICAL_BOOT_PACKAGES:
            self.assertIn(
                pkg.lower(), conda_bare,
                f'boot-critical package {pkg!r} missing from _CONDA_PYTHON_DEPS '
                f'— the conda repair path would install a non-bootable env')

    def test_boot_critical_packages_are_declared_in_requirements(self):
        import bootstrap
        req = _requirements_names()
        for pkg in bootstrap._CRITICAL_BOOT_PACKAGES:
            self.assertIn(
                pkg.lower(), req,
                f'boot-critical package {pkg!r} not declared in requirements.txt '
                f'— the drift guard must track a real requirement')

    def test_the_asgi_stack_is_present(self):
        """Explicit regression assertion for the exact drift that was found:
        quart + hypercorn (NOT just transitive flask) must be in the conda list."""
        import bootstrap
        conda_bare = {_bare(s) for s in bootstrap._CONDA_PYTHON_DEPS}
        self.assertIn('quart', conda_bare)
        self.assertIn('hypercorn', conda_bare)


if __name__ == '__main__':
    unittest.main()
