"""Relay ingress-auth adoption never strands an attached unkeyed runner (EVAL-MATRIX-3, coordinator steer 0dd843ac).

The 2026-11-13 22:26 sequence: tools/watch_relay_auth.py saw a relay at a zero-inflight boundary and switched it to
keyed ingress (adopt_relay_auth.py relaunches it on the SAME port with --ingress-key-file) while a runner without
--relay-key-file still sent its VMs there. From then on the relay refused every one of that runner's requests (401 at
the install-time artifact download), so every attempt ended infra_error without a model call: 2,704 attempts on five
...4861 relays by 06:05 on 11-14.

Contract: while a live runner whose run sends VMs to the relay's port carries no relay key, neither the watcher nor the
one-shot adopter switches that relay; both name the attached runner(s). A keyed runner does not block the switch.
The tools live in the lane directory; TB_RELAY_AUTH_TOOLS points the spec at another copy (e.g. the pre-fix tools).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

TOOLS = Path(os.environ.get("TB_RELAY_AUTH_TOOLS", "/path/to/your/data"
                                                   "your-username/tofu/.eval-runs/eval-matrix-20261112/tools"))
RELAY = "gemini-3.5-flash"

pytestmark = pytest.mark.unit


class Stats(http.server.BaseHTTPRequestHandler):
    """A relay at a zero-inflight boundary: /stats answers 200 with nothing in flight."""

    def do_GET(self):
        body = json.dumps({"inflight": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def relay_port():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stats)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def lane(tmp_path, relay_port):
    """A lane directory as on 11-13 22:26: one unadopted relay, its run, the relay-auth tools under test."""
    root = tmp_path / "lane"
    (root / "tools").mkdir(parents=True)
    for name in ("watch_relay_auth.py", "adopt_relay_auth.py"):
        shutil.copy(TOOLS / name, root / "tools" / name)
    (root / "code-0250ee0b9a").mkdir()
    (root / "code-0250ee0b9a" / "COMMIT").write_text("0250ee0b9a\n")
    relay = root / "relays" / RELAY
    relay.mkdir(parents=True)
    (relay / "watchdog.sh").write_text(f"#!/bin/sh\npython -m evaluations.terminal_bench.relay --port {relay_port} "
                                       "--key-suffixes 4861 --max-rpm 5\n")
    work_dir = root / "runs" / RELAY / "a2"
    work_dir.mkdir(parents=True)
    (work_dir / "run.json").write_text(json.dumps({"relay_url": f"http://10.114.80.41:{relay_port}",
                                                   "arms": ["tofu"], "max_attempts": 3}))
    return root, work_dir


@pytest.fixture
def runner_process(lane, relay_port):
    """A live runner attached to the relay: its command line is the matrix runner's, its run.json names the relay."""
    _, work_dir = lane
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "-m",
                                "evaluations.terminal_bench", "run", "--work-dir", str(work_dir),
                                "--relay-url", f"http://10.114.80.41:{relay_port}"])
    time.sleep(0.3)
    yield process
    process.kill()
    process.wait()


def watch_for(root: Path, seconds: float) -> str:
    """Run the watcher for a few of its polls and return what it printed."""
    watcher = subprocess.Popen([sys.executable, str(root / "tools" / "watch_relay_auth.py"), "--interval", "1"],
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(seconds)
    watcher.kill()
    output, _ = watcher.communicate()
    return output


def test_the_watcher_does_not_switch_a_relay_an_unkeyed_runner_is_attached_to(lane, runner_process):
    root, work_dir = lane
    output = watch_for(root, 4)
    assert "adopting auth" not in output, output
    assert str(work_dir) in output and f"pid {runner_process.pid}" in output, output
    assert not (root / "relays" / RELAY / "AUTH-ADOPTED.json").exists()
    assert not (root / "tools" / "A10-PAUSE").exists()


def test_the_adopter_refuses_and_names_the_attached_unkeyed_runner(lane, runner_process):
    root, work_dir = lane
    done = subprocess.run([sys.executable, str(root / "tools" / "adopt_relay_auth.py"), RELAY, "--timeout", "1"],
                          cwd=root, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    assert "NOT adopted" in done.stdout and str(work_dir) in done.stdout, done.stdout + done.stderr
    assert not (root / "relays" / RELAY / "relay.key").exists()
    assert not (root / "tools" / "A10-PAUSE").exists()


def test_a_keyed_runner_does_not_block_the_switch(lane, runner_process):
    root, work_dir = lane
    config = json.loads((work_dir / "run.json").read_text())
    (work_dir / "run.json").write_text(json.dumps(dict(config, relay_key_file=str(root / "relays" / RELAY /
                                                                                  "relay.key"))))
    output = watch_for(root, 3)
    assert "adopting auth" in output, output


def test_a_runner_on_another_relay_does_not_block_the_switch(lane, runner_process, relay_port):
    root, work_dir = lane
    config = json.loads((work_dir / "run.json").read_text())
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        elsewhere = other.getsockname()[1]
    (work_dir / "run.json").write_text(json.dumps(dict(config, relay_url=f"http://10.114.80.41:{elsewhere}")))
    output = watch_for(root, 3)
    assert "adopting auth" in output, output
