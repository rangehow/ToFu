"""The Rust node program (``tofu node``, packages/tofu-node) for interop tests.

Responsibility: build the binary once with cargo (incremental; the target
directory is packages/tofu-node/target unless ``CARGO_TARGET_DIR`` names
another) and return its path, or skip the calling test when cargo is not
installed. Entry: :func:`node_binary`, :func:`node_command`.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path
import shutil
import subprocess

import pytest

CHECKOUT = Path(__file__).resolve().parents[2]
MANIFEST = CHECKOUT / 'packages' / 'tofu-node' / 'Cargo.toml'


@functools.lru_cache(maxsize=1)
def _build() -> str:
    cargo = shutil.which('cargo')
    if cargo is None:
        return ''
    completed = subprocess.run(
        [cargo, 'build', '--quiet', '--manifest-path', str(MANIFEST), '--bin', 'tofu',
         '--message-format', 'json-render-diagnostics'],
        cwd=str(CHECKOUT), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError('tofu-node build failed:\n' + completed.stderr[-4000:])
    import json
    for line in completed.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if (message.get('reason') == 'compiler-artifact' and message.get('executable')
                and message.get('target', {}).get('name') == 'tofu'):
            return message['executable']
    raise RuntimeError('tofu-node build produced no tofu executable')


def node_binary() -> str:
    """Path of the built ``tofu`` binary; skips the test without cargo."""
    path = _build()
    if not path:
        pytest.skip('cargo is not installed; the Rust node cannot be built')
    assert os.access(path, os.X_OK), path
    return path


def node_command(*args: str) -> list[str]:
    """``tofu node <args>``: the Rust node with the Python node's arguments."""
    return [node_binary(), 'node', *args]
