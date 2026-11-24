"""Value-driven tool repair for ``fetch_url`` calls that name local files.

Models sometimes put ``file:///...`` (or an existing absolute/``~`` path) in
``fetch_url.url`` or ``fetch_url.urls``. HTTP(S) is the fetch authority;
workspace bytes belong to ``read_files`` with its root/path guards. This module
owns ONE predicate and the dispatch-shape rewrite, so the unified ingest seam
and streaming pre-execution agree without duplicating a URL parser.

Entry points: :func:`local_file_fetch_paths`,
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


def _existing_file(candidate: str) -> bool:
    try:
        return os.path.isfile(candidate)
    except (OSError, ValueError):
        return False


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
    # A bare path cannot be an HTTP URL. Only an EXISTING regular file is
    # definite enough to redirect; relative paths depend on a cwd this pure
    # boundary does not own, and protocol-relative //host/path is a URL.
    if raw.startswith('//'):
        return None
    if raw.startswith('~/'):
        return raw if _existing_file(os.path.expanduser(raw)) else None
    if not raw.startswith('/'):
        return None
    return raw if _existing_file(raw) else None


def local_file_fetch_paths(fn_args: Mapping[str, Any]) -> list[str] | None:
    """Every definite local path one ``fetch_url`` call names, else ``None``.

    A rewrite must never lose a target: it fires only when EVERY single-url or
    batch target is an unambiguous local file. A mixed batch keeps fetch_url's
    authority and its own per-entry refusal.
    """
    if not isinstance(fn_args, Mapping):
        return None
    urls = fn_args.get('urls')
    if urls:
        if not isinstance(urls, list) or not urls:
            return None
        targets: list[str] = []
        for spec in urls:
            target = spec.get('url') if isinstance(spec, dict) else spec
            if not isinstance(target, str):
                return None
            targets.append(target)
    else:
        value = fn_args.get('url')
        if not isinstance(value, str):
            return None
        targets = [value]
    paths = [_local_file_path(target) for target in targets]
    if not paths or any(path is None for path in paths):
        return None
    return paths


def redirect_local_file_fetch(
    fn_name: str,
    fn_args: Mapping[str, Any],
    *,
    read_files_admitted: bool,
) -> tuple[str, dict[str, Any], str] | None:
    """Rewrite one all-local ``fetch_url`` into a ``read_files`` dispatch.

    Returns ``(tool_name, arguments, model_warning)`` or ``None`` when the
    call must keep its original authority. ``read_files_admitted`` is the
    caller's combined visibility + request-owned contract admission check.
    """
    if fn_name != 'fetch_url' or not read_files_admitted:
        return None
    paths = local_file_fetch_paths(fn_args)
    if paths is None:
        return None
    count = len(paths)
    if count == 1:
        args: dict[str, Any] = {'path': paths[0]}
        dispatched = f'read_files(path={paths[0]!r})'
    else:
        args = {'reads': [{'path': path} for path in paths]}
        dispatched = f'read_files(reads=[{count} files])'
    urls = fn_args.get('urls')
    first = (urls[0] if isinstance(urls, list) and urls else None)
    if isinstance(first, dict):
        first = first.get('url')
    shown = str(first or fn_args.get('url') or '')
    if len(shown) > _MAX_NOTE_TARGET:
        shown = shown[:_MAX_NOTE_TARGET - 3] + '...'
    note = (
        f'fetch_url with {count} local-file target{"s" if count > 1 else ""} '
        f'(first: {shown!r}) was dispatched as {dispatched}. '
        f'Local files are read from the workspace, not fetched over HTTP.'
    )
    if fn_args.get('extract'):
        note += (' `extract` does not apply to a local read; resend '
                 'read_files with a range to narrow the content.')
    return 'read_files', args, note
