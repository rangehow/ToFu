"""A product module first imported under a patched storage factory is restored after the test."""
import sys
from types import ModuleType

import pytest

from tests.support.storage_alias_isolation import (
    imported_module_names, restore_storage_client_aliases,
)

pytestmark = pytest.mark.unit


def _product_module(name, factory):
    module = ModuleType(name)
    module.get_storage_client = factory
    sys.modules[name] = module
    return module


def test_an_alias_bound_to_a_test_factory_during_the_test_is_restored(monkeypatch):
    from lib.storage import service

    def test_authority_factory(*, write=False):
        raise AssertionError('stopped test authority')

    before = imported_module_names()
    monkeypatch.delitem(sys.modules, 'lib._alias_probe_leaked', raising=False)
    leaked = _product_module('lib._alias_probe_leaked', test_authority_factory)

    assert restore_storage_client_aliases(before) == ['lib._alias_probe_leaked']
    assert leaked.get_storage_client is service.get_storage_client


def test_preexisting_and_product_defined_aliases_are_left_alone(monkeypatch):
    def test_authority_factory(*, write=False):
        raise AssertionError('stopped test authority')

    def product_factory(*, write=False):
        return None
    product_factory.__module__ = 'lib._alias_probe_owner'

    monkeypatch.delitem(sys.modules, 'lib._alias_probe_old', raising=False)
    monkeypatch.delitem(sys.modules, 'lib._alias_probe_owner', raising=False)
    old = _product_module('lib._alias_probe_old', test_authority_factory)
    before = imported_module_names()
    owner = _product_module('lib._alias_probe_owner', product_factory)

    assert restore_storage_client_aliases(before) == []
    assert old.get_storage_client is test_authority_factory
    assert owner.get_storage_client is product_factory
