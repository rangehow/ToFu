"""lib/overleaf_preview.py — Compile-and-locate pipeline behind the chat
live PDF preview (the "Overleaf feel": an agent TeX edit recompiles the
paper and the panel scrolls to the edited spot).

Flow for one preview request:

  1. Resolve the target source line. The browser sends the edited ``file``
     plus a ``needle`` (the inserted text / section title); we read the
     current file through the Overleaf MCP bridge and map the needle to a
     1-based line so SyncTeX can point at the *new* content.
  2. ``mcp__overleaf__download_pdf`` compiles the project and stages the
     PDF on the local cache path (per-build URL internally — the legacy
     ``/output/output.pdf`` shortcut 404s on current Overleaf).
  3. ``mcp__overleaf__locate_in_pdf`` maps (file, line) to a PDF page and
     bounding rectangle(s) in PostScript points (``v`` from page top).

The route layer owns HTTP concerns; everything here is synchronous and
bridge-driven so tests substitute a fake bridge.

Cache policy: one PDF per project under ``<data>/cache/overleaf/``, pruned
to the newest ``_CACHE_KEEP`` projects. PDFs are reconstructible transport
cache, never durable user state.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from lib.log import get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

PROJECT_ID_RE = re.compile(r'^[0-9a-f]{24}$')

#: Preview requests for one project serialize behind this lock so a rapid
#: edit burst cannot run concurrent Overleaf compiles of the same tree.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

_CACHE_KEEP = 8
#: locate_in_pdf needs a plausible source line; never scan absurd needles.
_NEEDLE_MAX = 4000


class PreviewError(Exception):
    """User-presentable failure (MCP down, compile failed, bad args)."""


def cache_dir() -> str:
    return os.path.join(data_root(), 'cache', 'overleaf')


def cached_pdf_path(project_id: str, *, base: str | None = None) -> str:
    return os.path.join(base or cache_dir(), f'{project_id}.pdf')


def _project_lock(project_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(project_id)
        if lock is None:
            lock = _LOCKS[project_id] = threading.Lock()
    return lock


def _prune_cache(base: str) -> None:
    try:
        pdfs = [os.path.join(base, name) for name in os.listdir(base)
                if name.endswith('.pdf')]
        pdfs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in pdfs[_CACHE_KEEP:]:
            os.unlink(stale)
    except OSError:
        pass


def needle_to_line(text: str, needle: str) -> int | None:
    """Map inserted text to a 1-based line in the CURRENT file content.

    Exact substring first; then a whitespace-tolerant pass comparing the
    needle's first non-blank line against each source line (the model's
    ``new_string`` often differs from the committed file by indentation).
    """
    if not needle:
        return None
    needle = needle[:_NEEDLE_MAX]
    idx = text.find(needle)
    if idx >= 0:
        return text.count('\n', 0, idx) + 1
    first = next((ln.strip() for ln in needle.splitlines() if ln.strip()), '')
    if not first:
        return None
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.strip() == first:
            return lineno
    return None


def parse_locate_result(raw: str) -> dict:
    """Normalize the SyncTeX locate tool's text into {page, rects}.

    Tolerates a bare rect dict, a list of rects, or an envelope carrying
    ``rects``/``boxes``. Rects are ``{page, h, v, width, height}`` in
    PostScript points with ``v`` measured from the page top.
    """
    empty = {'page': None, 'rects': []}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return empty
    if isinstance(data, dict) and isinstance(data.get('rects'), list):
        rects = data['rects']
    elif isinstance(data, dict) and isinstance(data.get('boxes'), list):
        rects = data['boxes']
    elif isinstance(data, list):
        rects = data
    elif isinstance(data, dict) and 'v' in data and 'h' in data:
        rects = [data]
    else:
        rects = []
    rects = [r for r in rects if isinstance(r, dict)
             and all(k in r for k in ('h', 'v', 'width', 'height'))]
    page = None
    if isinstance(data, dict) and isinstance(data.get('page'), int):
        page = data['page']
    if page is None and rects and isinstance(rects[0].get('page'), int):
        page = rects[0]['page']
    return {'page': page, 'rects': rects}


def _call(bridge, tool: str, args: dict) -> str:
    try:
        return bridge.call_tool(f'mcp__overleaf__{tool}', args)
    except ValueError as exc:
        raise PreviewError(str(exc)) from exc
    except Exception as exc:  # MCP transport / upstream Overleaf failure
        raise PreviewError(f'overleaf {tool} failed: {exc}') from exc


def run_preview(bridge, project_id: str, *, file: str = '',
                needle: str = '', line: int | None = None,
                base: str | None = None, timeout: int = 180) -> dict:
    """Compile ``project_id`` and locate the edited line in the fresh PDF.

    Returns ``{pdf_path, compiled_at, page, rects}``. Raises PreviewError
    with a presentable message on any failure.
    """
    if not PROJECT_ID_RE.match(project_id or ''):
        raise PreviewError('project_id must be a 24-hex Overleaf project id')
    file = (file or '').strip() or None
    needle = (needle or '')[:_NEEDLE_MAX]

    if file and line is None and needle:
        content = _call(bridge, 'read_file', {
            'project_id': project_id, 'file_path': file})
        line = needle_to_line(content, needle)

    base = base or cache_dir()
    os.makedirs(base, exist_ok=True)
    pdf_path = cached_pdf_path(project_id, base=base)

    lock = _project_lock(project_id)
    # Compile+download serializes per project; locate (SyncTeX against the
    # just-built PDF) must ride the same critical section or a concurrent
    # recompile could swap the build underneath it.
    with lock:
        _call(bridge, 'download_pdf', {
            'project_id': project_id, 'output_path': pdf_path},
        )
        if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) == 0:
            raise PreviewError('compile finished but produced no PDF '
                               '(check the Overleaf compile log)')
        located = {'page': None, 'rects': []}
        if file and line:
            raw = _call(bridge, 'locate_in_pdf', {
                'project_id': project_id, 'file': file, 'line': int(line)})
            located = parse_locate_result(raw)
        compiled_at = time.time()
        _prune_cache(base)
    logger.info('[OverleafPreview] %s compiled; page=%s rects=%d',
                project_id[:8], located['page'], len(located['rects']))
    return {
        'pdf_path': pdf_path,
        'compiled_at': compiled_at,
        'page': located['page'],
        'rects': located['rects'],
    }
