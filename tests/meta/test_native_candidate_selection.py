"""Explicit native candidates and attempted builds cannot become silent skips."""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _fail_without_skip(call, pattern):
    with pytest.raises(pytest.fail.Exception, match=pattern):
        try:
            call()
        except pytest.skip.Exception as exc:
            raise AssertionError(f'explicit verification was silently skipped: {exc}') from exc


def test_explicit_supervisor_candidate_failure_is_not_a_skip(monkeypatch):
    from tests.storage import test_storage_supervisor_tofudb as suite
    monkeypatch.setenv('TOFUDB_BIN', '/nonexistent/explicit-candidate')
    _fail_without_skip(suite._tofudb_binary, 'TOFUDB_BIN')


def test_explicit_differential_candidate_failure_is_not_a_skip(monkeypatch):
    from tests.tofudb import test_tofudb_differential as suite
    from tests.support import tofudb_binary as binary_support
    monkeypatch.setenv('TOFUDB_DIFFERENTIAL_BIN', '/nonexistent/explicit-candidate')
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    monkeypatch.setattr(binary_support, '_FAILURE', None)
    _fail_without_skip(suite._resolve_tofudb_binary, 'TOFUDB_DIFFERENTIAL_BIN')


def _cargo_stand_in(tmp_path, body):
    cargo = tmp_path / 'cargo'
    cargo.write_text('#!/bin/sh\n' + body)
    cargo.chmod(0o755)
    return cargo


def test_failed_source_build_cannot_fall_back_to_stale_binary(tmp_path, monkeypatch):
    from tests.tofudb import test_tofudb_differential as suite
    from tests.support import tofudb_binary as binary_support
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    cargo = _cargo_stand_in(tmp_path, 'echo synthetic compile failure >&2\nexit 1\n')
    stale = tmp_path / 'packages/tofu-db/target/debug/tofu-db'
    stale.parent.mkdir(parents=True); stale.write_text('stale executable'); stale.chmod(0o755)
    monkeypatch.setattr(suite, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(binary_support, '_discover_cargo', lambda root: (cargo, dict(os.environ)))
    # The resolver caches per process; clear it so this scenario's candidates
    # are actually consulted.
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    monkeypatch.setattr(binary_support, '_FAILURE', None)
    _fail_without_skip(suite._resolve_tofudb_binary, 'build failed.*synthetic compile failure')


def test_source_built_test_engine_compiles_in_the_placed_cargo_target(tmp_path, monkeypatch):
    """Not in packages/tofu-db/target-differential: 16 lane checkouts held 66 GB of
    those on the root overlay (2026-11-12). The suite's hermetic placement pins
    (tests/conftest.py) do not decide where this real build goes."""
    from serverctl_pkg import native_engine
    from tests.support import tofudb_binary as binary_support
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(tmp_path / 'suite-placement'))
    monkeypatch.setenv('TOFU_RUNTIME_REPLICA', 'off')
    placed = tmp_path / 'scratch' / 'target-differential'
    requests = []

    def cargo_target_directory(root, name, environment=None):
        requests.append((Path(root), name, dict(environment or {})))
        placed.mkdir(parents=True, exist_ok=True)
        return placed

    monkeypatch.setattr(native_engine, 'cargo_target_directory', cargo_target_directory)
    cargo = _cargo_stand_in(tmp_path, 'mkdir -p "$CARGO_TARGET_DIR/debug"\n'
                            'printf "#!/bin/sh\\n" > "$CARGO_TARGET_DIR/debug/tofu-db"\n'
                            'chmod +x "$CARGO_TARGET_DIR/debug/tofu-db"\n')
    checkout = tmp_path / 'checkout'
    (checkout / 'packages/tofu-db').mkdir(parents=True)
    monkeypatch.setattr(binary_support, '_discover_cargo', lambda root: (cargo, dict(os.environ)))
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    monkeypatch.setattr(binary_support, '_FAILURE', None)

    built = binary_support.ensure_engine_binary(checkout)

    assert built == placed / 'stable' / 'tofu-db' and os.access(built, os.X_OK)
    assert not (checkout / 'packages/tofu-db/target-differential').exists()
    assert [(root, name) for root, name, _ in requests] == [(checkout, 'target-differential')]
    assert not {'TOFU_LOCAL_DIR', 'TOFU_RUNTIME_REPLICA'} & set(requests[0][2])


def test_missing_implicit_supervisor_installation_remains_optional(monkeypatch):
    from tests.storage import test_storage_supervisor_tofudb as suite
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    def absent():
        raise RuntimeError('no native engine installed')
    monkeypatch.setattr(suite, '_resolve_tofudb_binary', absent)
    with pytest.raises(pytest.skip.Exception, match='no native engine'):
        suite._tofudb_binary()


@pytest.mark.parametrize('result', ['timeout', 'missing_output'])
def test_attempted_source_build_must_produce_verified_candidate(tmp_path, monkeypatch, result):
    from tests.tofudb import test_tofudb_differential as suite
    from tests.support import tofudb_binary as binary_support
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    cargo = _cargo_stand_in(tmp_path, 'exec sleep 600\n' if result == 'timeout' else 'exit 0\n')
    monkeypatch.setattr(suite, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(binary_support, '_discover_cargo', lambda root: (cargo, dict(os.environ)))
    monkeypatch.setattr(binary_support, '_CARGO_BUILD_TIMEOUT_SECONDS', 0.5)
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    monkeypatch.setattr(binary_support, '_FAILURE', None)
    _fail_without_skip(suite._resolve_tofudb_binary,
                       'timed out' if result == 'timeout' else 'no executable candidate')


def test_explicit_content_pin_failure_cannot_be_skipped(monkeypatch):
    from tests.storage import test_storage_supervisor_tofudb as suite
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', '0' * 64)
    def mismatch():
        raise RuntimeError('tofu-db SHA-256 mismatch')
    monkeypatch.setattr(suite, '_resolve_tofudb_binary', mismatch)
    _fail_without_skip(suite._tofudb_binary, 'SHA-256 mismatch')
