"""Bridge forwards trusted request metadata only on the v2 client."""
from __future__ import annotations

import asyncio

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge

pytestmark = pytest.mark.unit


class _V2Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        class Result:
            isError = False
            content = []
        return Result()


def test_v2_call_tool_passes_meta_without_changing_arguments():
    bridge = MCPBridge()
    handle = mc._MCPServerHandle('hope', {})
    handle.sdk_generation = 2
    handle.session = _V2Session()
    asyncio.run(bridge._async_call_tool(
        handle, 'submit_job', {'workdir': '/tmp'}, None,
        meta={'tofu/project': '/canonical'}))
    name, arguments, kwargs = handle.session.calls[0]
    assert name == 'submit_job'
    assert arguments == {'workdir': '/tmp'}
    assert kwargs['meta'] == {'tofu/project': '/canonical'}
