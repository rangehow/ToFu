"""Value-driven tool repair for ``fetch_url`` calls that name a local file.

Models sometimes put ``file:///...`` (or the absolute path of an existing
file) in ``fetch_url.url``. HTTP(S) is the fetch authority; workspace bytes
belong to ``read_files`` with its root/path guards. This module owns ONE
predicate and the dispatch-shape rewrite, so the unified ingest seam and
streaming pre-execution agree without duplicating a URL parser.

Entry points: :func:`local_file_fetch_path`,
:func:`redirect_local_file_fetch`. Dependencies: standard library only.
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

LOCAL_FILE_TOOL_REDIRECT = 'local_file_tool_redirect'
_MAX_NOTE_TARGET = 160


def _printable(value: str) -> bool:
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _local_file_path(value: str) -> str | None:
    raw = value.strip()
    if not raw or not _printable(raw):
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme == 'file':
        # A remote authority is NOT a local file; reading its path here would
        # silently turn a foreign host's URI into a server-side filesystem read.
        if parsed.netloc and parsed.netloc.lower() != 'localhost':
            return None
        if parsed.query or parsed.fragment:
            return None
        path = unquote(parsed.path)
        if not path.startswith('/') or not _printable(path):
            return None
        return path
    if scheme:
        return None
    # A bare absolute path cannot be an HTTP URL. Only an EXISTING regular
    # file is definite enough to redirect; relative paths depend on a cwd this
    # pure boundary does not own, and protocol-relative //host/path is a URL.
    if not raw.startswith('/') or raw.startswith('//'):
        return None
    try:
        if not os.path.isfile(raw):
            return None
    except (OSError, ValueError):
        return None
    return raw


def local_file_fetch_path(fn_args: Mapping[str, Any]) -> str | None:
    """The unambiguous local file a single ``fetch_url`` call names, else None.

    Batch ``urls`` stays untouched: splitting one call into several different
    tools would require a second dispatch occurrence and a second authority.
    """
    if not isinstance(fn_args, Mapping) or fn_args.get('urls'):
        return None
    value = fn_args.get('url')
    if not isinstance(value, str):
        return None
    return _local_file_path(value)


def redirect_local_file_fetch(
    fn_name: str,
    fn_args: Mapping[str, Any],
    *,
    read_files_admitted: bool,
) -> tuple[str, dict[str, Any], str] | None:
    """Rewrite one local-file ``fetch_url`` into a ``read_files`` dispatch.

    Returns ``(tool_name, arguments, model_warning)`` or ``None`` when the
    call must keep its original authority. ``read_files_admitted`` is the
    caller's combined visibility + request-owned contract admission check.
    """
    if fn_name != 'fetch_url' or not read_files_admitted:
        return None
    path = local_file_fetch_path(fn_args)
    if path is None:
        return None
    raw = str(fn_args.get('url') or '').strip()
    shown = raw if len(raw) <= _MAX_NOTE_TARGET else raw[:_MAX_NOTE_TARGET - 3] + '...'
    note = (
        f'fetch_url({shown!r}) names a local file; it was dispatched as '
        f'read_files(path={path!r}). Local files are read from the workspace, '
        f'not fetched over HTTP.'
    )
    if fn_args.get('extract'):
        note += (' `extract` does not apply to a local read; resend '
                 'read_files with a range to narrow the content.')
    return 'read_files', {'path': path}, note
