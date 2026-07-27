"""lib/mcp/transport.py — Transport classification + header templating.

Three concerns that were previously implicit (and wrong once a third
transport existed) now have one home:

1. **Classification.** ``is_stdio`` / ``is_remote``. Callers used to write
   ``transport != 'sse'`` to mean "this is stdio" — a two-valued assumption
   that silently mis-classifies every transport added later.

2. **Header templating.** A remote MCP server authenticates with a header
   (``Authorization: Bearer <key>``). The secret itself MUST NOT live in the
   header block: ``headers`` is a *template* holding ``${VAR}`` references
   and the value comes from the server's existing ``env`` block, which is
   already the single redacted credential store (see ``redact_config``).

3. **Redaction.** One function decides what a config is allowed to show an
   API caller, so a new secret-bearing field can never be added to the
   config shape without this module having an opinion about it.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

# ── Transport vocabulary ─────────────────────────────────────────────
STDIO = 'stdio'
SSE = 'sse'
STREAMABLE_HTTP = 'streamable-http'

# Remote transports speak HTTP and are configured by ``url`` (+ optional
# ``headers``); stdio spawns a subprocess and is configured by ``command``.
REMOTE_TRANSPORTS = frozenset({SSE, STREAMABLE_HTTP})
VALID_TRANSPORTS = frozenset({STDIO}) | REMOTE_TRANSPORTS

# Accepted spellings for the streamable-HTTP transport. Upstream clients are
# inconsistent (Claude CLI writes ``http``, Cursor/Codex write
# ``streamable-http``, some docs write ``streamable_http``) and users
# copy-paste those blocks verbatim, so normalize rather than reject.
_TRANSPORT_ALIASES = {
    'http': STREAMABLE_HTTP,
    'https': STREAMABLE_HTTP,
    'streamable_http': STREAMABLE_HTTP,
    'streamablehttp': STREAMABLE_HTTP,
}

_PLACEHOLDER_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def normalize_transport(cfg: dict) -> str:
    """Return the canonical transport name for ``cfg`` (default ``stdio``)."""
    raw = str((cfg or {}).get('transport') or STDIO).strip().lower()
    return _TRANSPORT_ALIASES.get(raw, raw)


def is_stdio(cfg: dict) -> bool:
    """True when this server is launched as a local subprocess.

    Anything not recognised as a remote transport is treated as stdio, which
    matches the config default and keeps an unknown/typo'd transport failing
    with the explicit "stdio transport requires command" error rather than
    silently attempting a URL-less HTTP connection.
    """
    return normalize_transport(cfg) not in REMOTE_TRANSPORTS


def is_remote(cfg: dict) -> bool:
    """True when this server is reached over HTTP (sse / streamable-http)."""
    return not is_stdio(cfg)


def stdio_command(cfg: dict) -> str:
    """The launcher command, or ``''`` for remote servers.

    Every caller that wants "the subprocess launcher, if there is one" should
    use this instead of re-deriving it from a transport comparison.
    """
    return (cfg or {}).get('command', '') if is_stdio(cfg) else ''


def resolve_headers(cfg: dict, *, server_name: str = '') -> dict[str, str]:
    """Expand ``${VAR}`` placeholders in the header template.

    Values resolve from the server's own ``env`` block first (the redacted
    credential store the settings UI already writes), then from the process
    environment. The returned dict carries real secrets and MUST NOT be
    logged or returned to an API caller.

    Raises:
        ValueError: a placeholder has no value. Sending the literal
            ``${VAR}`` upstream would surface as an opaque 401 from the
            vendor; failing here names the missing key instead.
    """
    template = (cfg or {}).get('headers') or {}
    if not isinstance(template, dict):
        raise ValueError(
            f'MCP server {server_name!r}: "headers" must be an object, '
            f'got {type(template).__name__}'
        )

    env = (cfg or {}).get('env') or {}
    resolved: dict[str, str] = {}
    missing: list[str] = []

    for key, raw in template.items():
        def _sub(m: re.Match) -> str:
            var = m.group(1)
            val = env.get(var) or os.environ.get(var) or ''
            if not str(val).strip():
                missing.append(var)
                return ''
            return str(val)

        resolved[str(key)] = _PLACEHOLDER_RE.sub(_sub, str(raw))

    if missing:
        raise ValueError(
            f'MCP server {server_name!r}: header credential(s) not set: '
            f'{", ".join(sorted(set(missing)))}. Add the value in '
            f'Settings → MCP so it is stored in the server\'s env block.'
        )

    if resolved:
        logger.debug('[MCP:Transport] %s resolved %d auth header(s): %s',
                     server_name or '?', len(resolved), sorted(resolved))
    return resolved


def redact_config(cfg: dict) -> dict:
    """Strip every secret-bearing field before a config reaches an API caller.

    ``env`` holds raw credentials. ``headers`` holds only ``${VAR}``
    templates by contract, but a hand-edited ``mcp_servers.json`` can inline
    a literal token, so the template is rewritten to expose placeholder
    references and mask anything else — the shape stays visible (so the UI
    can show *that* a server authenticates) without the value ever leaving
    the process.
    """
    out = {k: v for k, v in (cfg or {}).items() if k not in ('env', 'headers')}
    template = (cfg or {}).get('headers') or {}
    if isinstance(template, dict) and template:
        out['headers'] = {
            str(k): _PLACEHOLDER_RE.sub(lambda m: '${%s}' % m.group(1),
                                        _mask_literal(str(v)))
            for k, v in template.items()
        }
    return out


def _mask_literal(value: str) -> str:
    """Replace any non-placeholder text in a header value with a mask."""
    parts = _PLACEHOLDER_RE.split(value)
    # re.split with one group yields [text, var, text, var, ..., text]
    rebuilt = []
    for i, chunk in enumerate(parts):
        if i % 2 == 1:               # captured variable name
            rebuilt.append('${%s}' % chunk)
        elif chunk:
            # Keep a short structural hint (e.g. "Bearer ") but never a secret.
            rebuilt.append(chunk if len(chunk) <= 8 and ' ' in chunk else '***')
    return ''.join(rebuilt)


def header_env_keys(cfg: dict) -> list[str]:
    """Env var names referenced by the header template (for UI prompting)."""
    template = (cfg or {}).get('headers') or {}
    if not isinstance(template, dict):
        return []
    keys: set[str] = set()
    for raw in template.values():
        keys.update(_PLACEHOLDER_RE.findall(str(raw)))
    return sorted(keys)


__all__ = [
    'STDIO', 'SSE', 'STREAMABLE_HTTP', 'REMOTE_TRANSPORTS', 'VALID_TRANSPORTS',
    'normalize_transport', 'is_stdio', 'is_remote', 'stdio_command',
    'resolve_headers', 'redact_config', 'header_env_keys',
]
