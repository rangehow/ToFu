"""Browser preparation uses the bounded, progress-reporting child owner."""
import sys

import pytest
import serverctl
from serverctl_pkg import native_engine

pytestmark = pytest.mark.unit


def test_browser_preparation_relays_progress_and_owns_descendants(tmp_path, monkeypatch, capsys):
    script = tmp_path / 'chromium_env.py'
    script.write_text('import sys; print("browser download progress", file=sys.stderr)')
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    assert serverctl._ensure_canary_browser({'TOFU_PYTHON': sys.executable}) == ''
    assert 'browser download progress' in capsys.readouterr().err


@pytest.mark.parametrize('failure', [RuntimeError('browser download failed'), OSError('cannot launch browser checker')])
def test_browser_preparation_failure_is_actionable(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        raise failure
    monkeypatch.setattr(native_engine, '_run', run)
    error = serverctl._ensure_canary_browser({'TOFU_PYTHON': sys.executable})
    assert str(failure) in error
    assert 'headless Chromium' in error
    assert calls[0][1]['progress']
    assert calls[0][1]['timeout'] == serverctl._FRONTEND_REPAIR_TIMEOUT_SECONDS


def test_status_retains_preparation_failure_beside_foreign_lock_problem(capsys):
    serverctl._print_status({
        'running': False, 'observed': 'conflict', 'ready': False,
        'lastError': 'instance lock names previous host',
        'lastWorkerExitKind': 'planned', 'lastWorkerExitReason': 'manual',
        'lastFailureReason': 'resident service preparation failed: cannot read source',
    })
    output = capsys.readouterr().out
    assert 'instance lock names previous host' in output
    assert 'resident service preparation failed: cannot read source' in output
