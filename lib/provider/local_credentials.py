"""Declared local CLI credentials: one-click import of a key a vendor CLI wrote.

Some providers issue the credential only through their own CLI, which writes
it to a fixed per-user config file after an interactive login. An installed
bundle declares that source in ``local_credentials/<template_key>.json``
(contract ``tofu.local-credential/v1``); this module reads and validates the
key server-side and can start the CLI's login to surface its SSO link.

Only bundles declare sources (``tofu_extensions.bundle_directories``), never
the user drop-in tier or a request: a caller-controlled path or command would
turn the import into a file-read oracle or a command launcher. The plaintext
key never leaves the server; only a presence verdict and the sealed envelope
do. With no bundle declaring a source, nothing is available.

Document shape::

    {"contract": "tofu.local-credential/v1", "template_key": "acme",
     "config_path": "~/.config/acme-cli/config.yaml", "format": "yaml",
     "field": "AUTHORIZATION", "token_pattern": "^[A-Za-z0-9]{32}$",
     "cli": {"command": ["acme", "login"],
             "login_url_pattern": "https?://\\\\S*?/login\\\\?uuid=\\\\S+"}}

Entry points: ``local_credential_source``, ``LocalCredentialError``,
``detect_local_key``, ``read_local_key``, ``secret_envelope``,
``cli_installed``, ``kick_cli_login``. Dependencies: ``tofu_extensions``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import threading
import time

from lib.log import get_logger
from tofu_extensions import bundle_directories

logger = get_logger(__name__)

LOCAL_CREDENTIAL_CONTRACT = 'tofu.local-credential/v1'
LOCAL_CREDENTIAL_KIND = 'local_credentials'
_FORMATS = frozenset({'yaml', 'json'})
#: How long a login kick waits for the CLI to print its SSO link before
#: returning without one; the CLI keeps running to receive the callback.
LOGIN_LINK_WAIT_S = 8.0


class LocalCredentialError(Exception):
    """No usable local credential. ``reason`` is a stable machine code without secret text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LocalCredentialSource:
    template_key: str
    config_path: str
    format: str
    field: str
    token_pattern: re.Pattern
    cli_command: tuple[str, ...]
    login_url_pattern: re.Pattern | None


def _parse(raw, source: str) -> LocalCredentialSource:
    if not isinstance(raw, dict) or raw.get('contract') != LOCAL_CREDENTIAL_CONTRACT:
        raise ValueError(f'{source}: not a {LOCAL_CREDENTIAL_CONTRACT} document')
    strings = {name: raw.get(name) for name in ('template_key', 'config_path', 'format', 'field',
                                                'token_pattern')}
    if not all(isinstance(value, str) and value for value in strings.values()):
        raise ValueError(f'{source}: {sorted(strings)} must be non-empty strings')
    if strings['format'] not in _FORMATS:
        raise ValueError(f'{source}: format must be one of {sorted(_FORMATS)}')
    cli = raw.get('cli') or {}
    command = cli.get('command') or []
    if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
        raise ValueError(f'{source}: cli.command must be a list of non-empty strings')
    link = cli.get('login_url_pattern')
    return LocalCredentialSource(
        template_key=strings['template_key'], config_path=strings['config_path'],
        format=strings['format'], field=strings['field'],
        token_pattern=re.compile(strings['token_pattern']), cli_command=tuple(command),
        login_url_pattern=re.compile(link, re.IGNORECASE) if isinstance(link, str) and link else None)


def local_credential_source(template_key: str) -> LocalCredentialSource | None:
    """The first installed bundle's declaration for ``template_key``, else None."""
    for directory in bundle_directories(LOCAL_CREDENTIAL_KIND):
        for path in sorted(directory.glob('*.json')):
            try:
                source = _parse(json.loads(path.read_text(encoding='utf-8')), str(path))
            except (OSError, ValueError, re.error) as exc:
                logger.warning('[LocalCredentials] skipping %s: %s', path, exc)
                continue
            if source.template_key == template_key:
                return source
    return None


def read_local_key(source: LocalCredentialSource | None) -> str:
    """Read and validate the key; raise ``LocalCredentialError`` with a stable reason."""
    if source is None:
        raise LocalCredentialError('not_declared')
    path = os.path.expanduser(source.config_path)
    if not os.path.isfile(path):
        raise LocalCredentialError('cli_config_missing')
    try:
        with open(path, encoding='utf-8') as handle:
            if source.format == 'yaml':
                import yaml
                data = yaml.safe_load(handle)
            else:
                data = json.load(handle)
    except Exception:
        # Never surface the parse error text: it can embed file contents.
        logger.warning('local credential import: unreadable CLI config at %s', path)
        raise LocalCredentialError('cli_config_unreadable') from None
    token = str(data.get(source.field) or '').strip() if isinstance(data, dict) else ''
    if not source.token_pattern.fullmatch(token):
        raise LocalCredentialError('authorization_invalid')
    return token


def detect_local_key(source: LocalCredentialSource | None) -> dict:
    """``{available: bool}`` without returning the secret."""
    try:
        read_local_key(source)
    except LocalCredentialError:
        return {'available': False}
    return {'available': True}


def secret_envelope(api_key: str) -> str:
    """Wrap the key in the canonical credential-secret envelope JSON."""
    return json.dumps({'format': 'tofu.credential-secret/v1', 'api_key': api_key,
                       'oauth': '', 'extra_headers': {}})


def cli_installed(source: LocalCredentialSource | None) -> bool:
    """True when the declared CLI's executable resolves on PATH."""
    return bool(source and source.cli_command and shutil.which(source.cli_command[0]))


def kick_cli_login(source: LocalCredentialSource | None, timeout_s: float = LOGIN_LINK_WAIT_S) -> dict:
    """Start the declared CLI login briefly to surface its SSO link.

    The CLI prints a login URL then waits for the user to finish SSO in a
    browser; it is left running detached so it can write the config on
    success. Returns ``{started, installed, login_url}`` and never raises.
    """
    if not cli_installed(source):
        return {'started': False, 'installed': False, 'login_url': ''}
    captured: list[str] = []

    def _run() -> None:
        try:
            proc = subprocess.Popen(
                list(source.cli_command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True,
                start_new_session=True)  # detach: survives this route's lifetime
        except Exception:
            logger.warning('local credential login kick: failed to spawn %s',
                           source.cli_command[0], exc_info=True)
            return
        deadline = time.monotonic() + timeout_s
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                match = source.login_url_pattern.search(line or '') if source.login_url_pattern else None
                if match:
                    captured.append(match.group(0).rstrip())
                if time.monotonic() > deadline or captured:
                    break
        except Exception:
            logger.warning('local credential login kick: read failed', exc_info=True)
        # Do NOT kill the process: it must stay alive to receive the SSO
        # callback and write the config once the user completes login.

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_s + 2.0)
    return {'started': True, 'installed': True, 'login_url': captured[0] if captured else ''}


__all__ = [
    'LOCAL_CREDENTIAL_CONTRACT', 'LOCAL_CREDENTIAL_KIND', 'LocalCredentialError',
    'LocalCredentialSource', 'cli_installed', 'detect_local_key', 'kick_cli_login',
    'local_credential_source', 'read_local_key', 'secret_envelope',
]
