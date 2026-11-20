"""The Rust MCP host's calls back into its binding (packages/tofu-mcp/src/callback.rs).

The host asks the process that drives it for facts other owners keep (the
shared network route plan, credential stores, browser devices). A handler's
answer and its failure category reach the Rust caller; a method with no
handler is ``unavailable``; a slow handler never blocks other replies.
"""
from __future__ import annotations

import os
import threading

import pytest

from tests.mcp_protocol.rust_parity_support import mcp_host_binary  # noqa: F401  (fixture)
__all__ = ['mcp_host_binary']

pytestmark = pytest.mark.unit

COVERS: dict[str, list[str]] = {}


def _host(binary, callbacks):
    from lib.mcp.rust_host import RustMCPHost
    return RustMCPHost(binary, env=dict(os.environ), callbacks=callbacks)


def test_a_handler_answer_and_its_failure_category_reach_the_host(mcp_host_binary):
    from lib.mcp.rust_host import RustHostError

    class Refused(Exception):
        kind = 'invalid'

    def refuse(params):
        raise Refused('no such entry')

    host = _host(mcp_host_binary, {'echo': lambda params: {'got': params}, 'refuse': refuse})
    try:
        assert host.call('ask_binding', {'method': 'echo', 'params': {'a': [1, 'ü']}}, timeout=30) == {
            'got': {'a': [1, 'ü']}}
        with pytest.raises(RustHostError) as refused:
            host.call('ask_binding', {'method': 'refuse'}, timeout=30)
        assert refused.value.kind == 'invalid'
        assert 'no such entry' in str(refused.value)
        with pytest.raises(RustHostError) as missing:
            host.call('ask_binding', {'method': 'nobody'}, timeout=30)
        assert missing.value.kind == 'error'
        assert 'no handler for nobody' in str(missing.value)
    finally:
        host.close()


def test_a_slow_handler_does_not_hold_other_replies(mcp_host_binary):
    release = threading.Event()
    host = _host(mcp_host_binary, {'slow': lambda params: release.wait(30) and 'late',
                                   'fast': lambda params: 'now'})
    try:
        _, slow = host.submit('ask_binding', {'method': 'slow'})
        assert host.call('ask_binding', {'method': 'fast'}, timeout=30) == 'now'
        assert not slow.done()
        release.set()
        assert slow.result(30) == 'late'
    finally:
        host.close()
