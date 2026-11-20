"""Black-box harness target: any Tofu server reachable at one base URL.

``TOFU_BLACKBOX_URL`` names the server under test (for example a future Rust
front door started by ``scripts/blackbox_run.py --server-url URL``); the
harness then never imports server internals. Without it, the session boots the
in-repo Python server through the shared ``live_server`` fixture in
``tests/conftest.py``, which refuses to start against production storage.

Cases in this directory speak only HTTP/WebSocket over that URL with the
standard library and read only committed contract files.
"""

from __future__ import annotations

import os

import pytest

BLACKBOX_URL_ENV = 'TOFU_BLACKBOX_URL'


@pytest.fixture(scope='session')
def blackbox_url(request) -> str:
    explicit = os.environ.get(BLACKBOX_URL_ENV, '').strip()
    if explicit:
        return explicit.rstrip('/')
    return request.getfixturevalue('live_server').rstrip('/')
