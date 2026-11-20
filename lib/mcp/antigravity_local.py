"""Opt-in personal stdio connector: MCP client → official Antigravity ACP CLI.

Google owns login, token storage, model selection, and the agent. This connector
owns only the lifetime of one text prompt, never a provider route or OAuth token.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal

import anyio
from mcp.types import CallToolResult, TextContent

from lib.auth_mode import MODE_PRIVATE, get_state
from lib.identity import PERSONAL_USER_ID
from lib.oauth.antigravity import AntigravityError, prompt_antigravity as _prompt


def _require_private_owner() -> None:
    if (get_state(refresh=True).mode != MODE_PRIVATE
            or os.environ.get('TOFU_MCP_OWNER_ID') != f'tofu:{PERSONAL_USER_ID}'):
        raise AntigravityError('Antigravity requires a private single-user Tofu instance and its owner.')


def build_server(agy: Path):
    from mcp.server import MCPServer

    mcp = MCPServer('tofu-antigravity')

    @mcp.tool(
        name='ask_antigravity',
        description=('Ask the locally signed-in Google Antigravity agent a one-off text question. '
                     'Only send content the user wants passed to Google. This can consume the signed-in '
                     'account’s quota and run under that account’s Antigravity agent policies.'),
        annotations={'readOnlyHint': False, 'idempotentHint': False, 'openWorldHint': True},
    )
    async def ask_antigravity(prompt: str) -> CallToolResult:
        try:
            _require_private_owner()
            text = await _prompt(agy, prompt)
        except AntigravityError as exc:
            return CallToolResult(is_error=True, content=[TextContent(type='text', text=str(exc))])
        return CallToolResult(content=[TextContent(type='text', text=text)])

    return mcp


async def _serve(agy: Path) -> None:
    server = build_server(agy)
    # The MCP stdio owner can send TERM while a prompt is in flight. Retire
    # the separate ACP group before exiting rather than abandoning it.
    with anyio.open_signal_receiver(signal.SIGTERM) as signals:
        async with anyio.create_task_group() as scope:
            async def stop_on_term():
                await anext(signals)
                scope.cancel_scope.cancel()

            scope.start_soon(stop_on_term)
            await server.run_stdio_async()
            scope.cancel_scope.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description='Use the locally signed-in Antigravity CLI from private Tofu.')
    parser.add_argument('--agy', type=Path, required=True, help='Absolute path to the official agy executable')
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('The Antigravity connector currently requires a POSIX host.')
    if not args.agy.is_absolute() or not args.agy.is_file() or not os.access(args.agy, os.X_OK):
        parser.error('--agy must name an installed executable at an absolute path.')
    try:
        _require_private_owner()
    except AntigravityError as exc:
        parser.error(str(exc))
    anyio.run(_serve, args.agy)


if __name__ == '__main__':
    main()
