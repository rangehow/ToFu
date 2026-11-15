"""The conversation-sync repository resolves its storage client when it reads.

A default that bound ``lib.storage.get_storage_client`` at import kept whatever
object that name held at the first import: a test fake installed then (the
channel suite's in-memory record client) answered every later snapshot read in
the same process, so unrelated conversation tests failed with the fake's
``KeyError: 'namespace'``. Production code must read the facade's current
client on each call, as ``lib.automations.repository._storage`` does.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]

_PROBE = '''
import lib.storage as storage
storage.get_storage_client = lambda write=False: ('installed-at-import', write)
from lib.conversation_sync.repository import SidecarConversationSyncRepository
repository = SidecarConversationSyncRepository()
storage.get_storage_client = lambda write=False: ('current', write)
print(repository._client())
'''


def test_default_client_is_the_facade_client_at_read_time_not_at_import():
    # A fresh interpreter makes this the module's first import, as in a test
    # worker whose first conversation-sync import happens under a fixture.
    result = subprocess.run([sys.executable, '-c', _PROBE], cwd=ROOT, capture_output=True,
                            text=True, check=True, env={**os.environ, 'PYTHONPATH': str(ROOT)})
    assert result.stdout.strip().splitlines()[-1] == "('current', False)"


def test_an_explicit_factory_is_still_used_as_given():
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    calls = []
    repository = SidecarConversationSyncRepository(
        client_factory=lambda write=False: calls.append(write) or 'given')
    assert repository._client() == 'given' and calls == [False]
