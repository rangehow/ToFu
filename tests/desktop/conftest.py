"""Shared isolation for desktop/node specs.

- Every test gets its own node config path, so the node call journal
  (``lib/desktop_agent/_journal.py``) and config writes never touch the
  developer's ``~/.tofu`` and never leak a recorded command id into a later test.
- The Home device policy (``lib/desktop/device_policy.py``) reads an in-memory
  record store instead of the storage authority.
- Specs in this directory predate Home ceilings and specify the device-side
  switch, so by default the Home ceiling allows every tier here. A test marked
  ``home_ceiling`` gets the real default-deny ceiling (new devices read-only);
  ``tests/desktop/test_device_routing.py`` specifies that boundary.
"""
from __future__ import annotations

import pytest

from tests.channels.conftest import RecordClient


def pytest_configure(config):
    config.addinivalue_line('markers', 'home_ceiling: use the real default-deny Home ceiling')


@pytest.fixture(autouse=True)
def _isolated_node_config(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'node-config' / 'desktop_agent.json'))


@pytest.fixture
def policy_records():
    return RecordClient()


@pytest.fixture(autouse=True)
def _device_policy_store(request, policy_records, monkeypatch):
    from lib.desktop import device_policy
    device_policy.invalidate()
    monkeypatch.setattr(device_policy, '_default_client', policy_records)
    if request.node.get_closest_marker('home_ceiling') is None:
        monkeypatch.setattr(device_policy, 'ceiling_tiers',
                            lambda record, node_id: {tier: True for tier in
                                                     ('read', *device_policy.CEILING_TIERS)})
    yield
    device_policy.invalidate()
