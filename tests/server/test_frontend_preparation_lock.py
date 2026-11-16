"""Concurrent frontend preparation stays bounded and reuses publication."""
import fcntl
import sys
import threading
import time

import pytest

import serverctl

pytestmark = pytest.mark.unit


def test_busy_frontend_lock_refuses_before_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: ['unused'])
    monkeypatch.setattr(serverctl, '_FRONTEND_BUILD_LOCK_TIMEOUT_SECONDS', 0.05)
    def stale():
        raise ValueError('stale frontend')
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', stale)
    monkeypatch.setattr(serverctl.subprocess, 'run', lambda *_a, **_k: pytest.fail('busy lock must not start a build'))
    lock = tmp_path / 'data/.frontend-build.lock'
    lock.parent.mkdir()
    with lock.open('a+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        error = serverctl.prepare_source_frontend_artifact('test restart')
        assert time.monotonic() - started < 1
    assert 'another frontend build is still running' in error
    assert 'current service remains online' in error


def test_waiter_reuses_graph_published_by_lock_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: ['unused'])
    monkeypatch.setattr(serverctl, '_FRONTEND_BUILD_LOCK_TIMEOUT_SECONDS', 2)
    published = threading.Event()
    validation_started = threading.Event()
    def validate():
        if not published.is_set():
            validation_started.set()
            raise ValueError('stale frontend')
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', validate)
    monkeypatch.setattr(serverctl.subprocess, 'run', lambda *_a, **_k: pytest.fail('published graph must be reused'))
    lock = tmp_path / 'data/.frontend-build.lock'
    lock.parent.mkdir()
    with lock.open('a+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        results = []
        waiter = threading.Thread(target=lambda: results.append(serverctl.prepare_source_frontend_artifact('test restart')))
        waiter.start()
        assert validation_started.wait(timeout=1)
        published.set()
        fcntl.flock(held, fcntl.LOCK_UN)
        waiter.join(timeout=3)
        assert not waiter.is_alive()
    assert results == ['']


def _stale_source_checkout(tmp_path, monkeypatch, browser_cli):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact',
                        lambda: (_ for _ in ()).throw(ValueError('stale frontend')))
    (tmp_path / 'chromium_env.py').write_text(browser_cli)
    built = tmp_path / 'built'
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: [
        sys.executable, '-c', f'open({str(built)!r}, "w").close()'])
    return built


def test_fresh_host_without_a_browser_refuses_start_with_one_actionable_line(tmp_path, monkeypatch):
    built = _stale_source_checkout(tmp_path, monkeypatch, (
        'import sys\n'
        'print("installer progress\\nError: getaddrinfo ENOTFOUND", file=sys.stderr)\n'
        'print("headless Chromium is not installed in /r and installing it failed. Install it '
        'with: py -m playwright install --only-shell chromium; after a host move, '
        'scripts/migrate_host.py --start restores ~/.cache/ms-playwright.", file=sys.stderr)\n'
        'sys.exit(69)\n'))
    error = serverctl.prepare_source_frontend_artifact('Tofu startup', capture_output=True)
    assert '\n' not in error
    assert error.startswith('the frontend bundle canary needs a headless Chromium: ')
    assert 'playwright install --only-shell chromium' in error
    assert 'scripts/migrate_host.py --start' in error
    assert not built.exists(), 'no build (and so no canary) runs without its browser'


def _rebuild_harness(tmp_path, monkeypatch, *, builds):
    """Stale graph; each build runs ``builds[n]`` and moves to its sources."""
    import serverctl_pkg.native_engine as native_engine
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: ['build'])
    monkeypatch.setattr(serverctl, '_ensure_canary_browser', lambda _env: '')
    state = {'sources': 'v0', 'published': None, 'runs': 0}

    def run(*_a, **_k):
        step = builds[state['runs']]
        state['runs'] += 1
        state['published'] = state['sources']
        state['sources'] = step.get('sources_after', state['sources'])
        if step.get('fails'):
            raise RuntimeError(step['fails'])

    def validate():
        if state['published'] != state['sources']:
            raise ValueError('Vite authoring inputs are stale')
        if state['published'] == 'broken':
            raise ValueError('broken graph')

    monkeypatch.setattr(native_engine, '_run', run)
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', validate)
    monkeypatch.setattr(serverctl, '_frontend_source_fingerprint', lambda: state['sources'])
    return state


def test_landing_during_the_rebuild_builds_the_new_sources(tmp_path, monkeypatch):
    state = _rebuild_harness(tmp_path, monkeypatch, builds=[
        {'sources_after': 'v1'}, {'sources_after': 'v2'}, {}])
    assert serverctl.prepare_source_frontend_artifact('test restart') == ''
    assert state['runs'] == 3 and state['published'] == 'v2'


def test_build_refusing_its_changed_inputs_is_rebuilt(tmp_path, monkeypatch):
    state = _rebuild_harness(tmp_path, monkeypatch, builds=[
        {'sources_after': 'v1', 'fails': 'frontend authoring inputs changed during the frontend build'},
        {}])
    assert serverctl.prepare_source_frontend_artifact('test restart') == ''
    assert state['runs'] == 2


def test_failure_on_settled_sources_refuses_after_one_build(tmp_path, monkeypatch):
    state = _rebuild_harness(tmp_path, monkeypatch, builds=[{'fails': 'vite exploded'}])
    assert serverctl.prepare_source_frontend_artifact(
        'test restart', capture_output=True) == 'vite exploded'
    assert state['runs'] == 1
    state = _rebuild_harness(tmp_path, monkeypatch, builds=[{}])
    state['sources'] = 'broken'
    error = serverctl.prepare_source_frontend_artifact('test restart')
    assert error.startswith('frontend artifact remained invalid after rebuild: broken graph')
    assert state['runs'] == 1


def test_source_fingerprint_matches_the_validator_digests(monkeypatch):
    from lib import vite_assets
    monkeypatch.setattr(serverctl, 'PROJECT', vite_assets.BASE_DIR)
    assert serverctl._frontend_source_fingerprint() == (
        f'{vite_assets._source_i18n_catalog_digest()}:'
        f'{vite_assets._source_vite_authoring_digest()}')


def test_browser_authority_runs_in_the_canary_environment_before_the_build(tmp_path, monkeypatch):
    seen = tmp_path / 'seen'
    built = _stale_source_checkout(tmp_path, monkeypatch, (
        'import os, sys\n'
        f'open({str(seen)!r}, "w").write(" ".join(sys.argv[1:]) + "|" + os.environ["TOFU_PYTHON"])\n'))
    monkeypatch.delenv('TOFU_PYTHON', raising=False)
    monkeypatch.delenv('PYTHON', raising=False)
    assert serverctl.prepare_source_frontend_artifact('Tofu startup', capture_output=True) \
        .startswith('frontend artifact remained invalid after rebuild')
    assert seen.read_text() == f'--ensure-headless-shell|{sys.executable}'
    assert built.exists()
