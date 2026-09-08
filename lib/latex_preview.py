"""lib/latex_preview.py — Local LaTeX compile-and-locate pipeline behind the
chat live PDF preview.

The Overleaf variant (lib/overleaf_preview.py) drives a remote project
through the MCP bridge; this module is the LOCAL twin: an agent edit to a
``.tex`` file under a workspace root recompiles the paper with the host's
TeX engine (pdflatex/xelatex), SyncTeX maps the edited line into the fresh
PDF, and the chat panel scrolls to it. When the user never mentioned
Overleaf this is the default path.

Flow for one preview request:

  1. Resolve the model-written ``path`` (absolute, ``~``, relative to the
     primary root, or ``rootname:rel``) to an absolute ``.tex`` file that
     must live inside a known workspace root.
  2. Map ``needle`` (the inserted text) to a 1-based line in the current
     file — same helper the Overleaf pipeline uses.
  3. Pick the compile entry point: the edited file itself when it holds
     ``\\documentclass``, else the best main-document candidate in its
     directory (name preference + mtime).
  4. Compile with ``-synctex=1`` into a per-main cache dir (aux artifacts
     never touch the source tree), rerunning once when the log asks for it.
  5. Parse ``.synctex.gz`` (no synctex CLI on this host) and convert the
     nearest record for (edited file, line) to a PDF rect in PostScript
     points with ``v`` from the page top — the same rect contract the
     Overleaf locate tool returns.

Cache policy: one build per main document under ``<data>/cache/latex/<sha>/``,
pruned to the newest ``_CACHE_KEEP`` documents. PDFs are reconstructible
transport cache, never durable user state.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time

from lib.log import get_logger
from lib.overleaf_preview import PreviewError, needle_to_line
from lib.runtime_paths import data_root

logger = get_logger(__name__)

TEX_SUFFIXES = ('.tex', '.ltx')

#: Preview requests for one main document serialize behind this lock so a
#: rapid edit burst cannot run concurrent compiles of the same tree.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

_CACHE_KEEP = 8
_COMPILE_TIMEOUT_S = 180
_NEEDLE_MAX = 4000
_SP_PER_PT = 65536.0

_DOCCLASS_RE = re.compile(r'\\documentclass')
#: Main-document filename preference, best first.
_MAIN_NAME_ORDER = ('main', 'paper', 'root', 'ms', 'thesis', 'index')
#: Documents using these packages need a Unicode engine.
_XELATEX_HINT_RE = re.compile(
    r'\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*(?:fontspec|xeCJK|unicode-math)'
    r'|\\documentclass\s*(?:\[[^\]]*\])?\s*\{ctex'
    r'|\\usepackage\s*(?:\[[^\]]*\])?\s*\{ctex')
_RERUN_RE = re.compile(r'Rerun (to get|LaTeX|needed)', re.IGNORECASE)

# SyncTeX record forms (see module probe; coordinates are absolute page sp):
#   (tag,line:h,v:W,H,D      hbox open     x tag,line:h,v       math/char
#   [tag,line:h,v:W,H,D      vbox open     k tag,line:h,v:W     kern
_RECORD_RE = re.compile(
    r'^([(\[xkgvhr])(\d+),(\d+):(-?\d+),(-?\d+)'
    r'(?::(-?\d+)(?:,(-?\d+)(?:,(-?\d+))?)?)?$')
_INPUT_RE = re.compile(r'^Input:(\d+):(.*)$')


def cache_dir() -> str:
    return os.path.join(data_root(), 'cache', 'latex')


def _main_key(main_abs: str) -> str:
    return hashlib.sha1(main_abs.encode('utf-8')).hexdigest()[:16]


def _main_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
    return lock


def _prune_cache(base: str) -> None:
    try:
        dirs = [os.path.join(base, name) for name in os.listdir(base)
                if os.path.isdir(os.path.join(base, name))]
        dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in dirs[_CACHE_KEEP:]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        pass


def _known_roots(conv_id: str | None) -> list[str]:
    from lib.project_mod.config import (
        get_conv_roots,
        get_project_path,
        get_roots,
    )
    roots: list[str] = []
    primary = (get_project_path() or '').strip()
    if primary:
        roots.append(primary)
    registry = get_conv_roots(conv_id) if conv_id else get_roots()
    for state in registry.values():
        path = (state.get('path') or '').strip()
        if path:
            roots.append(path)
    return roots


def resolve_tex_path(raw_path: str, *, conv_id: str | None = None) -> str:
    """Resolve a model-written path to an absolute .tex file inside a known
    workspace root. Raises PreviewError with a presentable message."""
    raw = (raw_path or '').strip()
    if not raw:
        raise PreviewError('path is required')
    if os.path.isabs(raw) or raw.startswith('~'):
        abs_p = os.path.abspath(os.path.expanduser(raw))
    else:
        from lib.project_mod.config import get_project_path
        base = (get_project_path() or '').strip()
        if not base:
            raise PreviewError('no active project root for a relative path')
        if ':' in raw:
            from lib.project_mod.path_resolution import _resolve_base
            try:
                bp, rp = _resolve_base(base, raw, conv_id=conv_id)
            except Exception as exc:
                raise PreviewError(str(exc)) from exc
            abs_p = (os.path.abspath(os.path.expanduser(rp))
                     if os.path.isabs(rp) or rp.startswith('~')
                     else os.path.abspath(os.path.join(bp, rp)))
        else:
            abs_p = os.path.abspath(os.path.join(base, raw))
    if not abs_p.lower().endswith(TEX_SUFFIXES):
        raise PreviewError('path must be a .tex file')
    if not os.path.isfile(abs_p):
        raise PreviewError(f'file not found: {raw}')
    for root in _known_roots(conv_id):
        root_abs = os.path.abspath(root)
        try:
            if os.path.commonpath([abs_p, root_abs]) == root_abs:
                return abs_p
        except ValueError:  # different drives (Windows)
            continue
    raise PreviewError('path is outside the workspace roots')


def _read_head(path: str, limit: int = 131072) -> str:
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read(limit)
    except OSError as exc:
        raise PreviewError(f'cannot read {os.path.basename(path)}: {exc}') from exc


def find_main_document(edited_abs: str) -> str:
    """Pick the compile entry point for an edited .tex file.

    The edited file wins when it holds ``\\documentclass``; otherwise scan
    its directory for the best main-document candidate (``main.tex`` /
    ``paper.tex`` / … preferred, newest mtime breaks ties). Falls back to
    the edited file so the engine's own error reaches the user.
    """
    if _DOCCLASS_RE.search(_read_head(edited_abs)):
        return edited_abs
    scan_dir = os.path.dirname(edited_abs)
    candidates: list[str] = []
    try:
        names = os.listdir(scan_dir)
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith(TEX_SUFFIXES):
            continue
        full = os.path.join(scan_dir, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding='utf-8', errors='replace') as fh:
                if _DOCCLASS_RE.search(fh.read(65536)):
                    candidates.append(full)
        except OSError:
            continue
    if not candidates:
        return edited_abs

    def _rank(path: str) -> tuple:
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        pref = (_MAIN_NAME_ORDER.index(stem)
                if stem in _MAIN_NAME_ORDER else len(_MAIN_NAME_ORDER))
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        return (pref, -mtime)

    candidates.sort(key=_rank)
    return candidates[0]


def pick_engine(main_abs: str) -> str:
    """Choose xelatex for Unicode-package documents, else pdflatex."""
    want = 'xelatex' if _XELATEX_HINT_RE.search(_read_head(main_abs)) else 'pdflatex'
    exe = shutil.which(want)
    if not exe:
        fallback = 'pdflatex' if want == 'xelatex' else 'xelatex'
        exe = shutil.which(fallback)
    if not exe:
        raise PreviewError(
            'no local TeX engine found (install TeX Live: pdflatex/xelatex)')
    return exe


def parse_synctex(gz_path: str, *, target_file: str, target_line: int) -> dict:
    """Locate (target_file, target_line) in a .synctex.gz build artifact.

    Returns the same rect contract as the Overleaf locate tool:
    ``{page, rects:[{page, h, v, width, height}]}`` in PostScript points,
    ``v`` from the page top. ``{page: None, rects: []}`` when the edited
    file produced no records (e.g. not \\input by the main document).
    """
    empty = {'page': None, 'rects': []}
    try:
        with gzip.open(gz_path, 'rt', encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
    except (OSError, EOFError):
        return empty
    inputs: dict[int, str] = {}
    x_offset_sp = 0
    y_offset_sp = 0
    for ln in lines:
        m = _INPUT_RE.match(ln)
        if m:
            inputs[int(m.group(1))] = os.path.normpath(m.group(2).strip())
            continue
        if ln.startswith('X Offset:'):
            try:
                x_offset_sp = int(ln.split(':', 1)[1])
            except ValueError:
                pass
        elif ln.startswith('Y Offset:'):
            try:
                y_offset_sp = int(ln.split(':', 1)[1])
            except ValueError:
                pass
        elif ln == 'Content:':
            break
    target_norm = os.path.normpath(target_file)
    target_tags = {tag for tag, p in inputs.items() if p == target_norm}
    if not target_tags:
        return empty

    page = 0
    depth = 0
    order = 0
    best: tuple | None = None  # (line distance, order, page, h, v, w, h+d)
    for ln in lines:
        if not ln:
            continue
        c0 = ln[0]
        if c0 == '{':
            if depth == 0:
                try:
                    page = int(ln[1:])
                except ValueError:
                    pass
            depth += 1
            continue
        if c0 in ')]}':
            depth = max(0, depth - 1)
            continue
        if c0 == '!':
            continue
        m = _RECORD_RE.match(ln)
        if not m:
            continue
        tag = int(m.group(2))
        if tag not in target_tags:
            continue
        rec_line = int(m.group(3))
        h_sp = int(m.group(4))
        v_sp = int(m.group(5))
        w_sp = int(m.group(6) or 0)
        h_height = int(m.group(7) or 0)
        h_depth = int(m.group(8) or 0)
        dist = abs(rec_line - target_line)
        candidate = (dist, order, page, h_sp, v_sp, w_sp, h_height + h_depth)
        order += 1
        if best is None or candidate < best:
            best = candidate
    if best is None or page <= 0:
        return empty
    _, _, page, h_sp, v_sp, w_sp, box_h = best
    # Record `v` is the box BASELINE measured from the page top; the rect
    # contract wants the box top, so subtract the height component.
    h_pt = (h_sp - x_offset_sp) / _SP_PER_PT
    v_pt = (v_sp - y_offset_sp) / _SP_PER_PT
    height_pt = box_h / _SP_PER_PT if box_h > 0 else 10.0
    top_pt = v_pt - height_pt if box_h > 0 else v_pt - 7.0
    width_pt = w_sp / _SP_PER_PT if w_sp > 0 else 24.0
    rect = {
        'page': page,
        'h': round(h_pt, 3),
        'v': round(max(0.0, top_pt), 3),
        'width': round(width_pt, 3),
        'height': round(height_pt, 3),
    }
    return {'page': page, 'rects': [rect]}


def _compile(main_abs: str, outdir: str) -> tuple[str, str]:
    """Run the engine (plus one rerun when the log asks) into ``outdir``.

    Returns (pdf_path, synctex_gz_path). Aux artifacts stay inside outdir —
    the source tree is never polluted. Raises PreviewError with the log
    tail when no PDF materializes.
    """
    exe = pick_engine(main_abs)
    cmd = [exe, '-interaction=nonstopmode', '-synctex=1',
           '-output-directory', outdir, main_abs]
    cwd = os.path.dirname(main_abs)

    def _one_pass() -> str:
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=_COMPILE_TIMEOUT_S,
                text=True, errors='replace')
        except subprocess.TimeoutExpired as exc:
            raise PreviewError(
                f'latex compile exceeded {_COMPILE_TIMEOUT_S}s') from exc
        return proc.stdout or ''

    output = _one_pass()
    if _RERUN_RE.search(output):
        output = _one_pass()
    base = os.path.splitext(os.path.basename(main_abs))[0]
    pdf_path = os.path.join(outdir, f'{base}.pdf')
    if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) == 0:
        tail = '\n'.join((output or '').splitlines()[-12:])
        raise PreviewError(
            f'latex compile failed for {os.path.basename(main_abs)}'
            + (f':\n{tail}' if tail else ' (no log output)'))
    synctex_gz = os.path.join(outdir, f'{base}.synctex.gz')
    return pdf_path, synctex_gz


def run_local_preview(raw_path: str, *, needle: str = '',
                      line: int | None = None,
                      conv_id: str | None = None,
                      base: str | None = None) -> dict:
    """Compile the paper containing ``raw_path`` and locate the edit.

    Returns ``{pdf_path, pdf_key, compiled_at, page, rects}``. Raises
    PreviewError with a presentable message on any failure.
    """
    edited_abs = resolve_tex_path(raw_path, conv_id=conv_id)
    needle = (needle or '')[:_NEEDLE_MAX]
    if line is None and needle:
        line = needle_to_line(_read_head(edited_abs, limit=4 * 1024 * 1024),
                              needle)
    main_abs = find_main_document(edited_abs)
    key = _main_key(main_abs)
    root = base or cache_dir()
    outdir = os.path.join(root, key)
    os.makedirs(outdir, exist_ok=True)

    lock = _main_lock(key)
    # Compile + SyncTeX parse ride the same critical section or a
    # concurrent recompile could swap the build underneath the parse.
    with lock:
        pdf_path, synctex_gz = _compile(main_abs, outdir)
        located = {'page': None, 'rects': []}
        if line and os.path.isfile(synctex_gz):
            located = parse_synctex(
                synctex_gz, target_file=edited_abs, target_line=int(line))
        # Stable serving name: the route serves <key>/current.pdf.
        final_pdf = os.path.join(outdir, 'current.pdf')
        os.replace(pdf_path, final_pdf)
        compiled_at = time.time()
        _prune_cache(root)
    logger.info('[LatexPreview] %s compiled (%s); page=%s rects=%d',
                os.path.basename(main_abs), key[:8],
                located['page'], len(located['rects']))
    return {
        'pdf_path': final_pdf,
        'pdf_key': key,
        'compiled_at': compiled_at,
        'page': located['page'],
        'rects': located['rects'],
    }


def cached_pdf(pdf_key: str, *, base: str | None = None) -> str:
    """Path of the last build for a key, '' when absent/invalid."""
    if not re.fullmatch(r'[0-9a-f]{16}', pdf_key or ''):
        return ''
    path = os.path.join(base or cache_dir(), pdf_key, 'current.pdf')
    return path if os.path.isfile(path) else ''
