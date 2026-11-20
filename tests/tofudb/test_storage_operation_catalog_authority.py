"""contracts/storage_operations_v1.json is authored; the sidecar registry cannot drift from it."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import gen_storage_operation_catalog as catalog_gen

pytestmark = pytest.mark.unit
CATALOG = Path(catalog_gen.CATALOG)


@pytest.fixture(scope='module')
def catalog():
    return json.loads(CATALOG.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def registry():
    return catalog_gen._document()


def test_sidecar_registry_implements_the_authored_catalog(catalog, registry):
    assert catalog_gen._catalog_problems(catalog, registry) == []


def test_operation_on_one_side_only_fails(catalog, registry):
    trimmed = copy.deepcopy(catalog)
    removed = trimmed['operations'].pop(0)['name']
    problems = catalog_gen._catalog_problems(trimmed, registry)
    assert f'{removed}: registered by the sidecar but missing from the catalog' in problems
    assert 'operationsSha256 does not describe the catalog operations' in problems

    extended = copy.deepcopy(registry)
    extended['operations'] = [*registry['operations'][1:]]
    problems = catalog_gen._catalog_problems(catalog, extended)
    assert f'{removed}: in the catalog but not registered by the sidecar' in problems


def test_attribute_drift_names_the_operation(catalog, registry):
    changed = copy.deepcopy(catalog)
    operation = changed['operations'][0]
    operation['receiptRequired'] = not operation['receiptRequired']
    problems = catalog_gen._catalog_problems(changed, registry)
    assert any(problem.startswith(f"{operation['name']}: catalog ") for problem in problems)


def test_checker_never_writes_the_catalog(monkeypatch, tmp_path, catalog):
    written = []
    monkeypatch.setattr(Path, 'write_text', lambda self, *a, **k: written.append(self))
    assert catalog_gen.main([]) == 0
    assert CATALOG not in written
    assert json.loads(CATALOG.read_text(encoding='utf-8')) == catalog
