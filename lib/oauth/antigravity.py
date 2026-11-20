"""Antigravity agent sessions over the official ACP stdio transport.

The installed Google CLI owns OAuth, credential storage and upstream usage.
Tofu checks readiness, requests CLI-managed sign-in, or sends an explicit text turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Literal, NotRequired, TypedDict

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from lib.auth_mode import MODE_OPEN, MODE_PRIVATE, get_state
from lib.identity import PERSONAL_USER_ID, PrincipalContext
from lib.project_mod.process_group import escaped_descendant_groups, retire_process_group
from lib.tools.process_environment import inherited_child_environment
from lib.version import __version__

_ACP_VERSION = 1
# Preserve the existing local connector handshake and protocol framing bounds.
_ACP_CONNECT_TIMEOUT = 30
# A Settings request must eventually reap its child even behind a buffering proxy.
_SETTINGS_PROMPT_DEADLINE_SECONDS = 300
_SETTINGS_LOGIN_DEADLINE_SECONDS = 300
_PERSONAL_LOGIN_METHOD = 'oauth-personal'
_DESKTOP_ENV = frozenset({'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY'})
_ACP_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_AUTH_REQUIRED = -32000
_MISSING_METHOD = -32601
_STOP_REASONS = frozenset({'end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'})
_CHILD_ENV = frozenset({
    'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE',
    'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'SSL_CERT_FILE', 'SSL_CERT_DIR',
})


class AntigravityError(RuntimeError):
    """Only locally constructed messages; never includes raw ACP diagnostics."""


class _AuthenticationRequired(AntigravityError):
    """ACP explicitly declined access before or during the session."""

    browser_login_available = False


class AntigravityResult(TypedDict):
    state: Literal['unsupported', 'missing_cli', 'login_required', 'ready', 'unavailable']
    text: NotRequired[str]
    browser_login_available: NotRequired[bool]


def _child_environment(*, login: bool) -> dict[str, str]:
    allowed = _CHILD_ENV | _DESKTOP_ENV if login else _CHILD_ENV
    return {name: value for name, value in inherited_child_environment().items()
            if name.upper() in allowed}


def _text_delta(frame: dict, session_id: str | None) -> str:
    params = frame.get('params')
    if (session_id is None or frame.get('method') != 'session/update'
            or not isinstance(params, dict) or params.get('sessionId') != session_id):
        return ''
    update = params.get('update')
    if not isinstance(update, dict) or update.get('sessionUpdate') != 'agent_message_chunk':
        return ''
    content = update.get('content')
    if isinstance(content, dict) and content.get('type') == 'text':
        text = content.get('text')
        if isinstance(text, str):
            return text
    return ''


class _ACP:
    def __init__(self, process: anyio.abc.Process):
        assert process.stdin is not None and process.stdout is not None
        self._stdin = process.stdin
        self._stdout = BufferedByteReceiveStream(process.stdout)
        self._id = 0

    async def _send(self, **fields) -> None:
        line = json.dumps({'jsonrpc': '2.0', **fields}, ensure_ascii=True).encode('ascii')
        if len(line) > _ACP_MAX_DOCUMENT_BYTES:
            raise AntigravityError('Antigravity request exceeds the ACP document limit.')
        await self._stdin.send(line + b'\n')

    async def _receive(self) -> dict:
        line = await self._stdout.receive_until(b'\n', _ACP_MAX_DOCUMENT_BYTES)
        msg = json.loads(line)
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
            raise AntigravityError('Antigravity sent an invalid ACP frame.')
        return msg

    async def _server_request(self, frame: dict) -> None:
        request_id = frame['id']
        if type(request_id) not in (int, str):
            raise AntigravityError('Antigravity sent an invalid ACP request ID.')
        # No host tools, filesystem access or affirmative tool approvals.
        if frame.get('method') != 'session/request_permission':
            await self._send(id=request_id, error={'code': _MISSING_METHOD, 'message': 'Method not supported'})
            return
        params = frame.get('params')
        options = params.get('options') if isinstance(params, dict) else None
        outcome = {'outcome': 'cancelled'}
        for option in options if isinstance(options, list) else ():
            if (isinstance(option, dict) and option.get('kind') == 'reject_once'
                    and isinstance(option.get('optionId'), str)):
                outcome = {'outcome': 'selected', 'optionId': option['optionId']}
                break
        await self._send(id=request_id, result={'outcome': outcome})

    async def request(self, method: str, params: dict,
                      *, session_id: str | None = None) -> tuple[dict, str]:
        self._id += 1
        await self._send(id=self._id, method=method, params=params)
        chunks = []
        size = 0
        while True:
            frame = await self._receive()
            if 'method' in frame:
                if 'id' in frame:
                    await self._server_request(frame)
                    continue
                text = _text_delta(frame, session_id)
                if text:
                    size += len(text.encode('utf-8'))
                    if size > _ACP_MAX_DOCUMENT_BYTES:
                        raise AntigravityError('Antigravity response exceeds the ACP document limit.')
                    chunks.append(text)
                continue
            if type(frame.get('id')) is not int or frame['id'] != self._id:
                raise AntigravityError('Antigravity sent an unexpected ACP response.')
            error = frame.get('error')
            if isinstance(error, dict) and error.get('code') == _AUTH_REQUIRED:
                raise _AuthenticationRequired(
                    'Authentication required. On the Tofu host, run `agy acp --login`, then retry.')
            if error is not None or not isinstance(frame.get('result'), dict):
                raise AntigravityError('Antigravity rejected the ACP request. Check the local CLI and its account.')
            return frame['result'], ''.join(chunks)


async def _retire(process: anyio.abc.Process) -> None:
    # The call owns the child and its private working directory even on Stop.
    # Run the shared POSIX group retirement before release.
    with anyio.CancelScope(shield=True):
        try:
            def retire():
                pid = process.pid
                retire_process_group(
                    pid, leader_alive=lambda: process.returncode is None,
                    escaped_groups=escaped_descendant_groups(pid, pid),
                )

            await anyio.to_thread.run_sync(retire)
        finally:
            await process.aclose()


def _offers_personal_login(initialized: dict) -> bool:
    methods = initialized.get('authMethods')
    return isinstance(methods, list) and any(
        isinstance(method, dict) and method.get('id') == _PERSONAL_LOGIN_METHOD
        and method.get('type', 'agent') == 'agent' for method in methods
    )


async def _open_verified_session(acp: _ACP, cwd: str, *, login: bool) -> str:
    with anyio.fail_after(_ACP_CONNECT_TIMEOUT):
        initialized, _ = await acp.request('initialize', {
            'protocolVersion': _ACP_VERSION, 'clientCapabilities': {},
            'clientInfo': {'name': 'tofu', 'title': 'Tofu', 'version': __version__},
        })
        version = initialized.get('protocolVersion')
        if type(version) is not int or version != _ACP_VERSION:
            raise AntigravityError('Antigravity returned an unsupported ACP version.')
    available = _offers_personal_login(initialized)
    try:
        return await _new_session(acp, cwd)
    except _AuthenticationRequired as exc:
        exc.browser_login_available = available
        if not login or not available:
            raise
    await acp.request('authenticate', {'methodId': _PERSONAL_LOGIN_METHOD})
    try:
        # OAuth's acknowledgement alone is not evidence of a usable session.
        return await _new_session(acp, cwd)
    except _AuthenticationRequired as exc:
        exc.browser_login_available = available
        raise


async def _new_session(acp: _ACP, cwd: str) -> str:
    with anyio.fail_after(_ACP_CONNECT_TIMEOUT):
        session, _ = await acp.request('session/new', {'cwd': cwd, 'mcpServers': []})
    session_id = session.get('sessionId')
    if not isinstance(session_id, str) or not session_id:
        raise AntigravityError('Antigravity did not create an ACP session.')
    return session_id


async def _run_session(agy: Path, prompt: str | None, *, login: bool = False) -> str | None:
    with TemporaryDirectory(prefix='tofu-antigravity-') as cwd:
        try:
            # Shield only process creation so cancellation cannot lose the PID.
            with anyio.CancelScope(shield=True):
                process = await anyio.open_process(
                    [str(agy), 'acp', '--sandbox'], cwd=cwd,
                    env=_child_environment(login=login), stderr=subprocess.DEVNULL, start_new_session=True,
                )
        except OSError:
            raise AntigravityError('Could not start agy. Check the configured Antigravity CLI installation.') from None
        try:
            acp = _ACP(process)
            if login:
                session_id = await _open_verified_session(acp, cwd, login=True)
            else:
                with anyio.fail_after(_ACP_CONNECT_TIMEOUT):
                    session_id = await _open_verified_session(acp, cwd, login=False)
            if prompt is None:
                # Opening is the auth check. No model prompt is sent.
                return None
            return await _complete_turn(acp, session_id, prompt)
        except TimeoutError:
            raise AntigravityError('Antigravity did not open an ACP session before the handshake deadline.') from None
        except (anyio.EndOfStream, anyio.IncompleteRead, anyio.ClosedResourceError,
                anyio.BrokenResourceError, OSError):
            raise AntigravityError('The Antigravity ACP connection closed before the request completed.') from None
        except (ValueError, RecursionError, anyio.DelimiterNotFound):
            raise AntigravityError('Antigravity sent an invalid or oversized ACP frame.') from None
        finally:
            await _retire(process)


async def _complete_turn(acp: _ACP, session_id: str, prompt: str) -> str:
    response, text = await acp.request('session/prompt', {
        'sessionId': session_id, 'prompt': [{'type': 'text', 'text': prompt}],
    }, session_id=session_id)
    stop = response.get('stopReason')
    if stop != 'end_turn' or not text:
        reason = stop if isinstance(stop, str) and stop in _STOP_REASONS else 'unknown'
        raise AntigravityError(f'Antigravity did not return a complete text reply ({reason}).')
    return text


async def prompt_antigravity(agy: Path, prompt: str) -> str:
    """Send one text prompt using an operator-configured executable."""
    if not prompt.strip():
        raise AntigravityError('Provide a non-empty prompt for Antigravity.')
    text = await _run_session(agy, prompt)
    assert text is not None
    return text


def antigravity_owner_supported(principal: PrincipalContext) -> bool:
    return (principal.kind == 'user' and principal.owner_user_id == PERSONAL_USER_ID
            and principal.tenant_id is None and principal.has_scope('admin')
            and os.name == 'posix' and get_state(refresh=True).mode in (MODE_OPEN, MODE_PRIVATE))


def _find_cli() -> Path | None:
    # Only the service PATH and the operator's default per-user install path.
    # Neither HTTP input nor a project/workspace can select an executable.
    search = [part for part in os.get_exec_path() if os.path.isabs(part)]
    search.append(str(Path.home() / '.local' / 'bin'))
    found = shutil.which('agy', path=os.pathsep.join(search))
    return Path(found) if found else None


async def antigravity_for_owner(principal: PrincipalContext,
                                *, prompt: str | None = None, login: bool = False) -> AntigravityResult:
    """Run one owner-scoped status, sign-in, or explicit prompt operation."""
    if not antigravity_owner_supported(principal):
        return {'state': 'unsupported'}
    agy = _find_cli()
    if agy is None:
        return {'state': 'missing_cli'}
    deadline = (_SETTINGS_LOGIN_DEADLINE_SECONDS if login else
                _SETTINGS_PROMPT_DEADLINE_SECONDS if prompt is not None else None)
    try:
        with anyio.fail_after(deadline):
            text = await _run_session(agy, prompt, login=login)
    except _AuthenticationRequired as exc:
        content: AntigravityResult = {'state': 'login_required'}
        if exc.browser_login_available:
            content['browser_login_available'] = True
        return content
    except (AntigravityError, TimeoutError):
        return {'state': 'unavailable'}
    return {'state': 'ready'} if text is None else {'state': 'ready', 'text': text}
