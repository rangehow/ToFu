"""Offline selector authentication is independent of backup-store access."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

from serverctl_pkg.tofudb_restore_selectors import restore_selectors

pytestmark = pytest.mark.unit
CLI = [sys.executable, '-m', 'serverctl_pkg.tofudb_restore_selectors']


@pytest.fixture(params=[185, 218], ids=['legacy-framing', 'current-framing'])
def preserved(tmp_path, request):
    # Header-only fixture: native restore, not this extractor, validates footers/graph.
    encoded = bytearray(request.param)
    encoded[:12] = b'TDBBKP01\x01\x00\x00\x00'
    encoded[28:36] = (8).to_bytes(8, 'little')
    manifest = tmp_path / 'preserved.manifest'
    manifest.write_bytes(encoded)
    return manifest, hashlib.sha256(encoded).hexdigest()


def test_authenticated_bytes_supply_missing_generation_without_duplicate_provenance(preserved):
    manifest, digest = preserved
    expected = ('--manifest-sha256', digest, '--source-control-generation', '8')
    assert restore_selectors(manifest, digest.upper()) == expected
    assert restore_selectors(manifest, digest, 8) == expected


def test_conflicting_or_missing_evidence_refuses(preserved):
    manifest, digest = preserved
    for bad_digest in ('', 'bad', '0' * 64):
        with pytest.raises(ValueError):
            restore_selectors(manifest, bad_digest)
    for bad_generation in (0, 9):
        with pytest.raises(ValueError, match='conflicts'):
            restore_selectors(manifest, digest, bad_generation)
    original = manifest.read_bytes()
    manifest.write_bytes(original[:-1] + b'\x01')
    with pytest.raises(ValueError, match='independent receipt'):
        restore_selectors(manifest, digest)
    manifest.unlink()
    with pytest.raises(FileNotFoundError):
        restore_selectors(manifest, digest)


def test_bad_digest_is_refused_before_header_interpretation(tmp_path):
    manifest = tmp_path / 'invalid.manifest'
    manifest.write_bytes(b'not a manifest')
    with pytest.raises(ValueError, match='independent receipt'):
        restore_selectors(manifest, '0' * 64)
    with pytest.raises(ValueError, match='framing'):
        restore_selectors(manifest, hashlib.sha256(manifest.read_bytes()).hexdigest())


def test_cli_and_fail_closed_shell_procedure(preserved):
    manifest, digest = preserved
    env = {'PATH': os.defpath}
    result = subprocess.run([*CLI, '--manifest', manifest,
                             '--manifest-sha256', digest], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == list(restore_selectors(manifest, digest))
    for evidence in ([], ['--manifest-sha256', '0' * 64],
                     ['--manifest-sha256', digest, '--source-control-generation', '9']):
        result = subprocess.run(
            ['sh', '-c', 'selectors=$("$@") || exit "$?"; printf "restore-called %s" "$selectors"',
             'selector-procedure', *CLI, '--manifest', str(manifest),
             *evidence], env=env, capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert not result.stdout


def test_authenticated_manifest_without_generation_refuses(tmp_path):
    manifest = tmp_path / 'missing-generation.manifest'
    encoded = bytearray(218)
    encoded[:12] = b'TDBBKP01\x01\x00\x00\x00'
    manifest.write_bytes(encoded)
    with pytest.raises(ValueError, match='no source CONTROL generation'):
        restore_selectors(manifest, hashlib.sha256(encoded).hexdigest())
