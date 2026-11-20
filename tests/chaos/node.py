"""Real node processes for the chaos suite (one node program: lib.desktop_agent, or
its Rust implementation ``tofu node`` with ``program='rust'``).

Responsibility: start, fault and inspect the same headless node a container
or GPU server runs (``python -m lib.desktop_agent --join <link>``), each with
its own HOME / config / TMPDIR under the test root, an explicit simulated host
identity (machine id, hostname, container id: what lib/desktop_agent/
host_identity.py reads), and the instance's leak-scan tag so a process the node
leaks is found by its environment. Twins share one config file on purpose.

Entry: ``ChaosNode(root, name, tag=..., proxy=...)``; ``join(link)``;
``serve()``; ``kill()``; ``config()``; ``attachment()``; ``node_id``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from tests.chaos.instance import CHECKOUT, INSTANCE_TAG, _KEPT

NODE_TAG = 'TOFU_CHAOS_NODE'


class ChaosNode:
    def __init__(self, root: Path, name: str, *, tag: str, proxy: str, config_path: Path | None = None,
                 machine: str = 'chaos-image-machine-id', hostname: str | None = None,
                 container: str | None = None, python: str = sys.executable, program: str = 'python'):
        self.root = Path(root) / f'node-{name}'
        self.name = name
        self.tag = tag
        self.proxy = proxy
        self.config_path = Path(config_path) if config_path else self.root / 'home' / '.tofu' / 'desktop_agent.json'
        self.machine = machine
        self.hostname = hostname or f'host-{name}'
        self.container = container if container is not None else hashlib.sha256(name.encode()).hexdigest()
        self.python = python
        # 'python' = python -m lib.desktop_agent; 'rust' = tofu node (packages/tofu-node).
        self.program = program
        self.process: subprocess.Popen | None = None
        self.generation = 0

    def environment(self) -> dict[str, str]:
        for sub in ('home', 'tmp'):
            (self.root / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k in _KEPT or k.startswith('LC_')}
        env.update({
            INSTANCE_TAG: self.tag, NODE_TAG: self.name,
            'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
            'TOFU_DESKTOP_CONFIG': str(self.config_path),
            'PYTHONPATH': str(CHECKOUT), 'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1',
            # The Home is reachable only through the edge, like a CodeLab Home.
            'HTTP_PROXY': self.proxy, 'http_proxy': self.proxy, 'NO_PROXY': '', 'no_proxy': '',
        })
        return env

    def start(self, *args: str) -> 'ChaosNode':
        assert self.process is None or self.process.poll() is not None, 'node already running'
        self.generation += 1
        env = self.environment()
        log = (self.root / f'node-{self.generation}.log').open('ab')
        self.process = subprocess.Popen(
            [*self._entry(), *args, '--allow-exec'],
            cwd=str(CHECKOUT), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        return self

    def join(self, link: str) -> 'ChaosNode':
        """Join a Home with its pairing link, then keep serving (the one-line headless command)."""
        return self.start('--join', link)

    def serve(self) -> 'ChaosNode':
        return self.start()

    def log_text(self) -> str:
        path = self.root / f'node-{self.generation}.log'
        return path.read_text(errors='replace') if path.exists() else ''

    def wait_log(self, needle: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while needle not in self.log_text():
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f'node {self.name} exited {self.process.returncode}:\n{self.log_text()[-3000:]}')
            if time.monotonic() > deadline:
                raise TimeoutError(f'node {self.name} never logged {needle!r}:\n{self.log_text()[-3000:]}')
            time.sleep(0.1)
        return self.log_text()

    def kill(self, sig: int = signal.SIGKILL) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        os.kill(self.process.pid, sig)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.kill(self.process.pid, signal.SIGKILL)
            self.process.wait()

    def config(self) -> dict:
        return json.loads(self.config_path.read_text()) if self.config_path.exists() else {}

    def _host(self) -> list[str]:
        return [self.machine, self.hostname, self.container]

    def _entry(self) -> list[str]:
        """The node program on this node's simulated host identity."""
        if self.program == 'rust':
            from tests.support.tofu_node import node_command
            return node_command('--simulate-host', *self._host())
        return [self.python, '-m', 'tests.chaos.node_main', *self._host()]

    def host_key(self) -> str:
        """This node's slot key, computed exactly as the node does."""
        return subprocess.run([*self._entry(), '--host-key'],
                              env=self.environment(), cwd=str(CHECKOUT), capture_output=True, text=True,
                              check=True).stdout.strip().splitlines()[-1]

    @property
    def node_id(self) -> str:
        return self.config().get('agent_ids', {}).get(self.host_key(), '')

    def attachment(self) -> dict:
        cfg = self.config()
        key = self.host_key()
        rows = (cfg.get('attachments') if cfg.get('agent_ids', {}).get(key) == cfg.get('agent_id')
                else (cfg.get('host_attachments') or {}).get(key))
        return (rows or [{}])[0]

    def pids(self) -> list[int]:
        """Every live process this node started (by its environment tag)."""
        needle = f'{NODE_TAG}={self.name}'.encode()
        found = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if needle in (entry / 'environ').read_bytes().split(b'\0'):
                    state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                    if state != 'Z':
                        found.append(int(entry.name))
            except OSError:
                continue
        return found
