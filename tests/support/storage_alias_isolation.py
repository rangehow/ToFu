"""Product modules first imported under a patched storage factory keep the real one.

Responsibility: dozens of fixtures replace ``lib.storage.get_storage_client``
with a test authority's factory through ``monkeypatch``. A product module that
is first imported while that patch is active (``from lib.storage import
get_storage_client`` in a lazily imported owner such as
``lib.acceptance.turns``) binds the test's factory, and ``monkeypatch`` cannot
undo an alias it never made. Every later test in that process then reads a
stopped authority ("Storage sidecar is unavailable") and waits until
pytest-timeout: which test leaks depends on the xdist split, so the failure
appeared only in large parallel runs.

Entry points: :func:`imported_module_names` before a test's setup and
:func:`restore_storage_client_aliases` after its teardown. Only modules
imported during that test are examined, and only a ``get_storage_client``
defined by a test module is replaced: a product module never binds one.
"""

from __future__ import annotations

import sys
from types import ModuleType

_ALIAS = 'get_storage_client'


def imported_module_names() -> frozenset[str]:
    return frozenset(sys.modules)


def _is_test_module_name(name: str) -> bool:
    return name == 'conftest' or name.startswith(('tests.', 'conftest.')) or name == 'tests'


def restore_storage_client_aliases(before: frozenset[str]) -> list[str]:
    """Rebind leaked aliases in modules imported since ``before``; return their names."""
    from lib.storage import service

    canonical = service.get_storage_client
    restored = []
    for name, module in list(sys.modules.items()):
        if name in before or not isinstance(module, ModuleType) or _is_test_module_name(name):
            continue
        value = module.__dict__.get(_ALIAS)
        if value is None or value is canonical:
            continue
        if _is_test_module_name(getattr(value, '__module__', None) or ''):
            setattr(module, _ALIAS, canonical)
            restored.append(name)
    return restored
