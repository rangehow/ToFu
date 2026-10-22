"""Reject incomplete/failed measurements and reap a timed-out owned process."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("body,timeout,kind", [
    ("pass", "2", "incomplete_or_invalid_phase"),
    ("raise SystemExit(7)", "2", "process_failure"),
    ("import time; time.sleep(30)", "0.2", "process_failure"),
])
def test_driver_refuses_missing_results_and_reaps_failures(tmp_path, body, timeout, kind):
    fake = tmp_path / "fake-measure"
    fake.write_text("#!/usr/bin/env python3\n" + body + "\n")
    fake.chmod(0o755)
    output = tmp_path / "result"
    driver = Path(__file__).resolve().parents[1] / "measure.py"
    result = subprocess.run([sys.executable, str(driver), "--binary", str(fake),
        "--output", str(output), "--histories", "1000", "--branches", "1",
        "--iterations", "1", "--timeout-seconds", timeout, "--diagnostic"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert not manifest.get("complete")
    assert any(item["kind"] == kind for item in manifest["failures"])
    run = manifest["runs"][0]
    assert (output / run["stdout"]).is_file() and (output / run["stderr"]).is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(run["pid"], 0)
    if "sleep" in body:
        assert run["timeout"] and run["returncode"] < 0
