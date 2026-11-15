"""Small offline checks for the native workspace benchmark collector."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'benchmarks/native_workspace_compare.py'


def load_collector():
    spec = importlib.util.spec_from_file_location('native_workspace_compare_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_digest_is_path_ordered_and_content_sensitive(tmp_path):
    collector = load_collector()
    (tmp_path / 'z').mkdir()
    (tmp_path / 'z' / 'a').write_bytes(b'z')
    (tmp_path / 'a').write_bytes(b'a')
    first = collector.digest(tmp_path)
    assert first == collector.digest(tmp_path)
    (tmp_path / 'a').write_bytes(b'b')
    assert first != collector.digest(tmp_path)
    assert collector.entries(tmp_path) == {
        'a': hashlib.sha256(b'b').hexdigest(),
        'z/a': hashlib.sha256(b'z').hexdigest(),
    }


def test_missing_native_workspace_fails_loudly(tmp_path):
    collector = load_collector()
    with pytest.raises(RuntimeError, match='native worker failed'):
        collector.run_native(tmp_path / 'project', 1, '/bin/false')


def test_report_requires_equal_content(tmp_path):
    output = tmp_path / 'report.json'
    binary = tmp_path / 'worker'
    binary.write_text('#!/bin/sh\necho "not-json"\n')
    binary.chmod(0o755)
    result = subprocess.run([
        sys.executable, str(SCRIPT), '--repository', str(ROOT), '--binary', str(binary),
        '--files', '1', '--samples', '1', '--output', str(output),
    ], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()
