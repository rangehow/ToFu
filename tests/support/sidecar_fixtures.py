"""Scope helpers for opt-in real-Sidecar pytest plugins.

Pytest registers a module named by ``pytest_plugins`` as a process-wide
plugin. An ``autouse`` fixture from that plugin is therefore considered for
every subsequently collected test module, not only the module that declared
it. Real-Sidecar fixtures must check the current module's declaration before
starting a subprocess or creating a database.
"""

from __future__ import annotations

from contextlib import contextmanager


def module_declares_plugin(request, plugin_name: str) -> bool:
    """Return whether the current test module explicitly opted into a plugin."""
    declared = getattr(request.module, 'pytest_plugins', ())
    if isinstance(declared, str):
        declared = (declared,)
    try:
        return plugin_name in declared
    except TypeError:
        return False


@contextmanager
def installed_storage_runtime(runtime):
    """Make ``runtime`` the process authority, then give the authority back.

    ``install_runtime_for_test`` stops the runtime it replaces. A session
    ``live_server`` keeps serving through the process authority across
    modules, so a module that installed its own Sidecar and left ``None``
    behind fenced every later browser test with "Storage sidecar is
    unavailable". On exit this stops ``runtime`` and reinstalls the previous
    owner, restarting it when it was ready before.
    """
    from lib.storage.service import (
        install_runtime_for_test, started_storage_runtime)

    previous = started_storage_runtime()
    resume = previous is not None and previous.ready
    install_runtime_for_test(runtime)
    try:
        yield runtime
    finally:
        install_runtime_for_test(previous)
        if resume:
            previous.start()


__all__ = ['installed_storage_runtime', 'module_declares_plugin']
