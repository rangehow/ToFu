"""The owner resource registry seed names every queue the Hope patrol scans.

Private with ``scripts/hope_patrol/`` (export_pkg/_lists.py
_OPENSOURCE_EXCLUDE_PATHS): the seed is an inline literal in
``lib/router/home.py`` and never reads the patrol's files at runtime, so only
this cross-check of the two needs them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.router.home import owner_resources, router_home_path
from tests.router.test_router_home import root as root

pytestmark = pytest.mark.unit


def test_resource_registry_seed_names_every_patrol_queue_and_its_sources(root):
    router_home_path(7)
    seed = owner_resources(7, content=True)['content']
    patrol = json.loads(Path('scripts/hope_patrol/queues.json').read_text())
    for queue in patrol['queues']:
        assert f'`{queue}`' in seed
    for source in ('scripts/hope_patrol/queues.json', 'references/queues.md',
                   'references/rsi-queues-2026q4.md'):
        assert source in seed
