"""The ingress evidence scanner does not mistake JSON-escaped text for an nmap command (FHE-2, 2f17edc2)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCANNER = Path("/path/to/your/project")
spec = importlib.util.spec_from_file_location("evidence_scan", SCANNER)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

pytestmark = pytest.mark.unit


def test_json_escaped_newline_map_is_not_a_port_scan():
    text = r'{"output":"first line\nmap second line"}'
    assert scanner.re.search(scanner.PATTERNS["port_scan"], text) is None


def test_an_actual_nmap_command_is_still_detected():
    assert scanner.re.search(scanner.PATTERNS["port_scan"], "nmap -sV 10.251.1.2")
    assert scanner.re.search(scanner.PATTERNS["port_scan"], "sudo nmap -Pn target")


# 2026-11-14 (EVAL-MATRIX-3): restored attempts keep their records beside the attempt as <name>.ingress-refused-<ns>
# (runner set_aside, also .held-/.reset-). They hold runner error text with sandbox node addresses but no agent ran, so
# the 07:49 scan reported 15 false "suspicious" node_address hits in them. Set-aside records are not agent evidence.
def test_set_aside_runner_records_are_not_scanned_but_agent_files_still_are(tmp_path):
    import json
    import shutil
    import subprocess
    import sys

    lane = tmp_path / "lane"
    (lane / "tools").mkdir(parents=True)
    (lane / "analysis" / "security-scan").mkdir(parents=True)
    shutil.copy(SCANNER, lane / "tools" / "evidence_scan.py")
    attempt = lane / "runs" / "glm-5.2" / "a2" / "trials" / "tofu" / "git-leak-recovery" / "attempt-1"
    (attempt / "trial.ingress-refused-1794609916915920065" / "agent").mkdir(parents=True)
    (attempt / "state.ingress-refused-1794609916915920065.json").write_text('{"error": "peer 10.156.95.24:50583"}')
    (attempt / "result.ingress-refused-1794609916915920065.json").write_text('{"error": "10.241.1.2:8080"}')
    (attempt / "trial.ingress-refused-1794609916915920065" / "agent" / "log.txt").write_text("10.251.9.9:1\n")
    (attempt / "trial" / "agent").mkdir(parents=True)
    (attempt / "trial" / "agent" / "tofu.jsonl").write_text('{"cmd": "curl http://10.156.1.2:9000/"}\n')
    (attempt / "result.json").write_text("{}")
    done = subprocess.run([sys.executable, str(lane / "tools" / "evidence_scan.py")], cwd=lane,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    report = json.loads(next((lane / "analysis" / "security-scan").glob("evidence-*.json")).read_text())
    assert [hit["file"] for hit in report["suspicious"]] == ["trial/agent/tofu.jsonl"]
