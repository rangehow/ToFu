"""Start a scratch Tofu server that shares nothing with the live one: the tests/AGENTS.md recipe as code.

Usage, from a checkout: ``python3 -m tests.support.scratch_server <scratch root> [--port N]``.
The root is the instance's own directory (the workspace or ``~/.cache/...``); a free loopback port
is chosen when ``--port`` is omitted.

A Tofu shell exports the live server's identity: ``TOFU_PROJECT_PATH`` (the production project
root), ``PORT``, ``TOFU_PROCESS_ROLE=execution``, the storage connection, and more over time. So
the environment is built from an allowlist of what a process needs (``PATH``, ``HOME``, locale,
proxies, certificates), never by removing names. The instance runs with ``TOFU_RUN_SERVER=1``, binds
127.0.0.1, and gets its own data root, project path, heartbeat directory and ``TMPDIR`` under the
root. Its own ``TMPDIR`` keeps its admitted engine copy (lib/storage/admitted_engine.py, under
``tempfile.gettempdir()``) out of production's ``/tmp/tofu-admitted-engines-<uid>``, so a check
that identifies production's engine by its executable path never mistakes the scratch engine for it.
Rule and rationale: docs/TESTING_STRATEGY.md, "Scratch servers share nothing with the live one".
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys
from typing import Mapping

CHECKOUT = Path(__file__).resolve().parents[2]
_KEPT = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'TZ', 'LANG', 'LANGUAGE',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
})


def prepare_scratch_environment(root: Path, port: int, checkout: Path,
                                inherited: Mapping[str, str]) -> dict[str, str]:
    """Create the instance's directories under ``root`` and return its whole environment.

    Nothing else from ``inherited`` reaches the instance. The directories must exist:
    ``tempfile.gettempdir()`` silently falls back to ``/tmp`` when ``TMPDIR`` is missing.
    """
    root = root.resolve()
    directories = {name: root / name for name in ('data', 'project', 'heartbeat', 'tmp')}
    for directory in directories.values():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {name: value for name, value in inherited.items() if name in _KEPT or name.startswith('LC_')}
    environment.update({
        'TOFU_RUN_SERVER': '1', 'BIND_HOST': '127.0.0.1', 'PORT': str(port), 'PYTHONPATH': str(checkout),
        'TOFU_DATA_DIR': str(directories['data']), 'TOFU_PROJECT_PATH': str(directories['project']),
        'TOFU_HEARTBEAT_DIR': str(directories['heartbeat']), 'TMPDIR': str(directories['tmp']),
    })
    return environment


def free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('root', type=Path, help="the instance's own directory (workspace or ~/.cache)")
    parser.add_argument('--port', type=int, help='loopback port (default: a free one)')
    args = parser.parse_args(argv)
    port = args.port or free_loopback_port()
    environment = prepare_scratch_environment(args.root, port, CHECKOUT, os.environ)
    print(f'scratch Tofu server: http://127.0.0.1:{port}/ (root {args.root.resolve()})', flush=True)
    os.chdir(CHECKOUT)
    os.execve(sys.executable, [sys.executable, 'server.py', '--host', '127.0.0.1', '--port', str(port)],
              environment)
    return 1


if __name__ == '__main__':
    sys.exit(main())
