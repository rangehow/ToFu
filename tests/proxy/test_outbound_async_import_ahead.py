"""An outbound async request never searches the import path on the event loop.

httpcore runs ``import sniffio`` inside every async request
(``httpcore._synchronization.current_async_library``). Production's environment
does not install sniffio (anyio 4 dropped it), so each request's failed import
walked every ``sys.path`` directory, twice through the pinned import finder, on
the shared volume and on the event loop: LoopWatch sampled the loop there for
5-12 s (2026-11-18 13:11:54, 14:30:05, 14:31:23, 15:34:09). Importing the
outbound transport resolves that probe ahead, present or absent.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

_SUBJECT = textwrap.dedent(r'''
    import asyncio, sys

    class Absent:
        """sniffio is not installed, as in production."""
        def find_spec(self, name, path=None, target=None):
            if name == 'sniffio':
                raise ModuleNotFoundError(name)
            return None

    class Recorder:
        searched = []
        def find_spec(self, name, path=None, target=None):
            self.searched.append(name)
            return None

    if sys.argv[1] == 'absent':
        sys.meta_path.insert(0, Absent())
    import lib.http_transport.outbound_async  # noqa: F401  (boot import)
    from httpcore._synchronization import current_async_library
    recorder = Recorder()
    sys.meta_path.insert(0, recorder)

    async def requests():
        return [current_async_library() for _ in range(3)]

    print(asyncio.run(requests()), recorder.searched)
''')


@pytest.mark.parametrize('installed', ['present', 'absent'])
def test_request_time_async_library_probe_searches_no_path(installed):
    result = subprocess.run(
        [sys.executable, '-c', _SUBJECT, installed], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(ROOT)}, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['asyncio', 'asyncio', 'asyncio'] []", result.stdout
