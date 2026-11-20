#!/usr/bin/env python3
"""Run the black-box harness (tests/blackbox) against any Tofu server URL.

Usage: ``python3 scripts/blackbox_run.py --server-url URL [--reference-url REF] [pytest args...]``.
Sets ``TOFU_BLACKBOX_URL`` so the harness talks to that server only (it never
boots or imports the in-repo server) and runs pytest with the project
interpreter (``.venv/bin/python3`` when present). Without ``--server-url`` run
``pytest tests/blackbox`` directly to exercise the in-repo Python server.
``--reference-url`` sets ``TOFU_BLACKBOX_REFERENCE_URL`` for the differential
cases (``test_blackbox_push_differential.py``), which replay one script against
the reference (Python) server and the target and compare the bytes.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HARNESS = 'tests/blackbox'


def project_python() -> str:
    candidate = ROOT / '.venv' / 'bin' / 'python3'
    return str(candidate) if candidate.exists() else sys.executable


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--server-url', required=True, help='base URL, e.g. http://127.0.0.1:18080')
    parser.add_argument('--reference-url', help='reference server for the differential cases')
    args, pytest_args = parser.parse_known_args(argv)
    for flag, url in (('--server-url', args.server_url), ('--reference-url', args.reference_url)):
        target = urlsplit(url or 'http://unset')
        if url is not None and (target.scheme not in ('http', 'https') or not target.hostname):
            parser.error(f'{flag} must be an http(s) URL with a host: {url!r}')
    environment = dict(os.environ, TOFU_BLACKBOX_URL=args.server_url.rstrip('/'))
    if args.reference_url:
        environment['TOFU_BLACKBOX_REFERENCE_URL'] = args.reference_url.rstrip('/')
    # ``parse_known_args`` keeps the ``--`` separator, and pytest would take the
    # first option after it for a path ("file or directory not found: -q").
    if pytest_args[:1] == ['--']:
        pytest_args = pytest_args[1:]
    command = [project_python(), '-m', 'pytest', HARNESS, *pytest_args]
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


if __name__ == '__main__':
    sys.exit(main())
