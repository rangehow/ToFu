"""Pytest plugin: a package suite's process yields like the repository suite's.

Package suites (``packages/*/tests``, ``bundles/*/tests`` ...) do not load
``tests/conftest.py``, which lowers every repository pytest process before its
own module-level work. Their sessions load this module with ``-p``
(``scripts/test_execution.py``), so it lowers the process as pytest starts,
before their conftests import anything (docs/TESTING_STRATEGY.md, "Test
processes yield to the live server").
"""
from tests.support.process_priority import lower_own_priority

lower_own_priority()
