"""The opt-in ``live_server`` retargeting seam (``TOFU_BLACKBOX_URL``).

Setting the selector must change where HTTP suites point, not what they
exercise: ``live_server`` yields the target while the local app is never
constructed. These specs pin that boundary structurally — in a subprocess for
the boot path, so a prior test in the worker cannot make ``server`` appear
already imported.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from tests import conftest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SELECTOR = 'TOFU_BLACKBOX_URL'
TARGET = 'http://127.0.0.1:9'


class _Item:
    def __init__(self, *fixturenames):
        self.fixturenames = fixturenames


def _raw(fixture):
    return inspect.unwrap(fixture)


def _first_yield(fixture, **kwargs):
    return next(_raw(fixture)(**kwargs))


def test_the_seam_reuses_the_blackbox_harness_selector():
    from tests.blackbox import conftest as blackbox_conftest

    assert conftest._EXTERNAL_TARGET_ENV == blackbox_conftest.BLACKBOX_URL_ENV == SELECTOR


def test_live_server_yields_the_normalized_target(monkeypatch):
    monkeypatch.setenv(SELECTOR, TARGET + '/')
    assert _first_yield(conftest.live_server, request=None) == TARGET


def test_live_server_resolves_flask_app_only_on_the_local_path():
    """A static fixture argument would construct flask_app before the seam runs."""
    assert list(inspect.signature(_raw(conftest.live_server)).parameters) == ['request']


def test_flask_app_is_a_stub_that_refuses_attribute_access(monkeypatch):
    monkeypatch.setenv(SELECTOR, TARGET)
    app = _raw(conftest.flask_app)(None)
    assert TARGET in repr(app)
    with pytest.raises(RuntimeError, match=SELECTOR):
        _ = app.config


def test_unset_or_blank_selector_still_selects_the_local_boot(monkeypatch):
    monkeypatch.delenv(SELECTOR, raising=False)
    assert conftest._external_target_url() is None
    monkeypatch.setenv(SELECTOR, '   ')
    assert conftest._external_target_url() is None


_BOOT_PATH_PROBE = textwrap.dedent(f'''
    import builtins, inspect, json, sys, types

    import tests.conftest as conftest

    storage_calls = []
    fake_storage = types.ModuleType('lib.storage')
    fake_storage.start_storage = lambda *a, **k: storage_calls.append('start_storage')
    fake_storage.stop_storage = lambda *a, **k: storage_calls.append('stop_storage')
    sys.modules['lib.storage'] = fake_storage

    imported = []
    real_import = builtins.__import__

    def spy(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = spy
    resolved = next(inspect.unwrap(conftest.live_server)(request=None))
    stub = inspect.unwrap(conftest.flask_app)(None)
    try:
        stub.config
    except RuntimeError as error:
        refused = {SELECTOR!r} in str(error)
    else:
        refused = False
    print('SEAM-REPORT ' + json.dumps({{
        'resolved': resolved,
        'server_modules': [name for name in sys.modules
                           if name == 'server' or name.startswith('server.')],
        'server_imports': [name for name in imported
                           if name == 'server' or name.startswith('server.')],
        'storage_calls': storage_calls,
        'stub_refused': refused,
    }}))
''')


def test_external_target_boot_path_constructs_nothing_local():
    env = dict(os.environ, PYTHONPATH=str(ROOT), **{SELECTOR: TARGET})
    completed = subprocess.run(
        [sys.executable, '-c', _BOOT_PATH_PROBE],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    reports = [line for line in completed.stdout.splitlines()
               if line.startswith('SEAM-REPORT ')]
    assert reports, completed.stdout
    report = json.loads(reports[-1].removeprefix('SEAM-REPORT '))
    assert report == {
        'resolved': TARGET,
        'server_modules': [],
        'server_imports': [],
        'storage_calls': [],
        'stub_refused': True,
    }, report


def test_external_target_does_not_require_the_local_frontend_graph(monkeypatch):
    from tests import _jsdom

    monkeypatch.setenv(SELECTOR, TARGET)
    monkeypatch.setattr(_jsdom, 'shipped_graph_available', lambda: False)
    monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
    hook = conftest.pytest_runtest_setup(item=_Item('browser', 'live_server'))
    next(hook)  # the local-graph skip must not fire for an external target
    with pytest.raises(StopIteration):
        hook.send(None)


def test_page_fixture_skips_a_non_loopback_target(monkeypatch):
    monkeypatch.setenv(SELECTOR, 'https://tofu.example.com:8443')
    with pytest.raises(pytest.skip.Exception, match='non-loopback'):
        _first_yield(conftest.page, browser=object(),
                     live_server='https://tofu.example.com:8443')


@pytest.mark.parametrize('origin', [
    'http://127.0.0.1:5555',
    'http://127.1.2.3:5555',
    'http://[::1]:5555',
    'http://localhost:5555',
])
def test_conversation_cleanup_is_allowed_on_loopback(origin):
    conftest._require_loopback_cleanup_target(origin)


@pytest.mark.parametrize('origin', [
    'https://tofu.example.com',
    'http://10.0.0.5:8000',
    'http://0.0.0.0:8000',
])
def test_conversation_cleanup_refuses_a_non_loopback_target(origin):
    with pytest.raises(RuntimeError, match='not loopback'):
        conftest._require_loopback_cleanup_target(origin)
