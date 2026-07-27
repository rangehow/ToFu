"""tests/_conv_bundle_sources.py — resolve which shipped JS files a node
harness must eval to get a symbol defined, using the PRODUCTION bundle list.

WHY THIS EXISTS
---------------
`static/js/core/conversations.js` has been progressively decomposed (epic
pt_3879f00e sub-part 2: slice 3 pulled the persist/freshness/rebase helper
cluster into `core/conv_persist_helpers.js`, slice 4 the image hydrator, slice 5
`_applySettingsToConv`, ...). Every slice moved symbols OUT of that file while
keeping runtime behaviour identical, because the bundler concatenates the pieces
in a declared order.

Eleven node-harness guards hard-coded `core/conversations.js` as "the file that
defines these symbols" and eval'd it standalone. Each extraction slice therefore
broke a batch of them at once: the harness eval'd a file that no longer contains
the symbol, and the guard failed with a confusing shape (`typeof X !== 'function'`
or `substring not found`) that looks like a product regression but is pure
harness drift. That is the 8th recurrence of the "guard anchored on a path
instead of a symbol" family in this project.

THE FIX (single source of truth, not 11 copies)
-----------------------------------------------
`lib/js_bundler._BUNDLE_FILES` is the PRODUCTION load order — the same list the
browser gets. So instead of guessing a filename, a harness asks:

    sources_defining('_trimMsgForPersist')   -> ordered abs paths to eval

which searches the bundle's core files for the definition and returns every file
that must be eval'd, IN BUNDLE ORDER, so bare cross-file references resolve
exactly as they do at runtime. When a future slice moves the symbol again, these
guards follow it automatically instead of going red.

Deliberately reads the bundler's own list rather than globbing the directory:
globbing would silently pick up a file the bundle does NOT ship (a leftover, a
vendored copy) and eval it, which is how a guard ends up testing code that never
reaches a user.
"""

from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def bundle_files():
    """The production bundle order (list of paths relative to static/js)."""
    from lib.js_bundler import _BUNDLE_FILES
    return list(_BUNDLE_FILES)


def _defines(path, symbol):
    """True when *path* defines ``function <symbol>(`` or ``const/let/var
    <symbol> =`` at the top level (column 0) or as an indented module member."""
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return False
    pat = re.compile(
        r'^[ \t]*(?:async\s+)?function\s+' + re.escape(symbol) + r'\s*\('
        r'|^[ \t]*(?:const|let|var)\s+' + re.escape(symbol) + r'\s*=',
        re.M)
    return bool(pat.search(src))


def files_defining(symbol, *, subtree='core/'):
    """Bundle-relative paths (in bundle order) that define *symbol*.

    Restricted to *subtree* by default so a same-named local in an unrelated
    feature module cannot hijack the lookup.
    """
    return [f for f in bundle_files()
            if f.startswith(subtree) and _defines(os.path.join(JS_DIR, f), symbol)]


def sources_defining(*symbols, subtree='core/'):
    """Absolute paths to eval, in BUNDLE ORDER, so *symbols* all resolve.

    Raises with a THREE-STATE diagnosis (the distinction that the old
    hard-coded-path guards could not make):
      none -> the implementation is gone: a REAL regression, fix the product
      many -> the single source of truth was copied: collapse it first
      one  -> resolved; the caller evals the returned files
    """
    out = []
    for sym in symbols:
        hits = files_defining(sym, subtree=subtree)
        if not hits:
            raise AssertionError(
                f'{sym} is not defined by any bundled file under {subtree!r} — '
                f'the implementation was REMOVED. This is a product regression, '
                f'not harness drift: restore it before touching the guard.')
        if len(hits) > 1:
            raise AssertionError(
                f'{sym} is defined by {len(hits)} bundled files ({hits}) — the '
                f'single source of truth was duplicated; collapse it before '
                f're-pointing the guard.')
        out.append(hits[0])
    order = bundle_files()
    uniq = sorted(set(out), key=order.index)
    return [os.path.join(JS_DIR, f) for f in uniq]


def source_argv(*symbols, override=None, subtree='core/'):
    """Ordered abs paths for ``node harness <paths...>``, with optional override.

    *override* maps a bundle-relative path (e.g. ``'core/conversations.js'``) to
    a substitute file — the NEUTER pattern several guards use: write a mutated
    copy to tmp_path and eval that instead of the shipped file, leaving the real
    tree untouched. Passing an override for a path this symbol set does not need
    raises, so a stale neuter target is reported instead of silently ignored
    (which would make the NEUTER "not bite" and read as a passing guard).
    """
    paths = sources_defining(*symbols, subtree=subtree)
    if not override:
        return paths
    out = []
    matched = set()
    for p in paths:
        rel = os.path.relpath(p, JS_DIR).replace(os.sep, '/')
        if rel in override:
            out.append(override[rel])
            matched.add(rel)
        else:
            out.append(p)
    unused = set(override) - matched
    if unused:
        raise AssertionError(
            f'override targets {sorted(unused)} are not among the files needed '
            f'for {symbols} ({[os.path.relpath(p, JS_DIR) for p in paths]}) — '
            f'the neuter would silently NOT bite. Re-point it.')
    return out


def eval_prelude(*symbols, subtree='core/'):
    """A node snippet that eval's every file needed to define *symbols*.

    Drop-in replacement for a harness's hard-coded
    ``eval(fs.readFileSync('core/conversations.js'))``.
    """
    paths = sources_defining(*symbols, subtree=subtree)
    lines = ["const fs = require('fs');"]
    for p in paths:
        lines.append(f'eval(fs.readFileSync({p!r}, "utf8"));')
    return '\n'.join(lines)
