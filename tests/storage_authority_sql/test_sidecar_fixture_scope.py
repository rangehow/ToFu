"""Resource contract for opt-in real-Sidecar pytest plugins."""

from types import SimpleNamespace

import pytest

from tests.support.sidecar_fixtures import (
    installed_storage_runtime, module_declares_plugin)


pytestmark = pytest.mark.unit


def _request(declared=()):
    return SimpleNamespace(module=SimpleNamespace(pytest_plugins=declared))


def test_sidecar_plugin_runs_only_for_the_module_that_declared_it():
    plugin = 'tests._artifact_sidecar'

    assert module_declares_plugin(_request((plugin,)), plugin) is True
    assert module_declares_plugin(_request(plugin), plugin) is True
    assert module_declares_plugin(_request(('tests._other_plugin',)), plugin) is False
    assert module_declares_plugin(_request(), plugin) is False


def test_malformed_module_plugin_declaration_fails_closed():
    request = _request(declared=None)

    assert module_declares_plugin(request, 'tests._artifact_sidecar') is False


class _Runtime:
    def __init__(self, ready):
        self.ready = ready
        self.events = []

    def start(self):
        self.ready = True
        self.events.append('start')

    def stop(self, timeout=10.0):
        self.ready = False
        self.events.append('stop')


@pytest.mark.parametrize('was_ready', [True, False])
def test_module_sidecar_hands_the_process_authority_back(monkeypatch, was_ready):
    """A session live_server keeps serving after a module's own Sidecar."""
    from lib.storage import service

    session, module = _Runtime(ready=was_ready), _Runtime(ready=False)
    monkeypatch.setattr(service, '_runtime', session)
    with installed_storage_runtime(module):
        module.start()
        assert service.started_storage_runtime() is module
    assert module.events == ['start', 'stop']
    assert service.started_storage_runtime() is session
    assert session.events == (['stop', 'start'] if was_ready else ['stop'])
    assert session.ready is was_ready
