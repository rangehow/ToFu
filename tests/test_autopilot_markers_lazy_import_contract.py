#!/usr/bin/env python3
"""Lazy-import contract guard for autopilot_markers ↔ autopilot cycle.

**Why this test exists**

The slice-2 extraction (commit 01d2db3a) moved ``disarm_autopilot`` from
``lib/tasks_pkg/autopilot.py`` into ``lib/tasks_pkg/autopilot_markers.py``.
``disarm_autopilot`` calls ``conclude_run``, which STAYED in ``autopilot.py``.
And ``autopilot.py`` in turn re-exports the marker cluster from
``autopilot_markers`` for facade identity — so the two modules mutually
depend.

A NAIVE ``from lib.tasks_pkg.autopilot import conclude_run`` at the top of
``autopilot_markers.py`` would create a circular import loop that Python
resolves only by lucky import order — whichever module happens to be imported
FIRST hits a half-initialised partner module. The slice-2 fix threads the
``conclude_run`` import THROUGH the function body (lazy import), so
``autopilot_markers.py`` has NO top-level dependency on ``autopilot.py`` and
can be imported first in any order.

A future well-intentioned refactor ("clean up the lazy import — hoist it to
the top like every other module") would silently re-introduce the cycle. The
markers-functional suite covers the RUNTIME contract via
``test_disarm_calls_conclude_run_lazy_import``, but that only bites if a test
actually calls ``disarm_autopilot``; a silent regression could still slip
through if a package-load cycle emerges without a runtime call site.

This file closes that last surface with TWO STRUCTURAL guards on the LAYOUT
itself, independent of runtime behaviour:

  1. ``test_autopilot_markers_imports_alone_in_fresh_subprocess`` — runs
     ``python -c "import lib.tasks_pkg.autopilot_markers"`` in a FRESH
     subprocess where nothing has pre-imported ``autopilot``. Under the lazy
     contract, this MUST succeed. Under a regressed contract (top-level
     ``from lib.tasks_pkg.autopilot import conclude_run``), Python would
     either hit a partial-init ``AttributeError`` (autopilot_markers imported
     first → autopilot loads → hits its own ``from autopilot_markers import``
     re-export block → autopilot_markers is still initialising → the
     ``conclude_run`` attribute doesn't exist yet on the partial module) OR
     an ``ImportError`` outright. Either way the subprocess exit code is
     non-zero and the guard bites.

  2. ``test_disarm_autopilot_conclude_run_import_is_lazy_via_ast`` — parses
     ``autopilot_markers.py`` and confirms:
       (a) NO top-level ``from lib.tasks_pkg.autopilot import`` at module
           scope, and
       (b) The ``from lib.tasks_pkg.autopilot import conclude_run`` line
           lives INSIDE the ``disarm_autopilot`` function body (function-
           scope AST node, not module-scope).

     A future edit that hoists the import out flips (a); a future edit that
     removes the import entirely (silently dropping conclude_run) flips (b).

Both tests target the STRUCTURE, not the observable side effects, so they
survive any future rewrite of ``disarm_autopilot``'s body as long as the
lazy-import contract holds.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MARKERS_PATH = os.path.join(_ROOT, 'lib', 'tasks_pkg', 'autopilot_markers.py')


# ══════════════════════════════════════════════════════════
#  Guard 1 — fresh-subprocess import-order test
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_autopilot_markers_imports_alone_in_fresh_subprocess():
    """``lib.tasks_pkg.autopilot_markers`` must be import-safe as the FIRST
    thing loaded — no top-level dependency on ``lib.tasks_pkg.autopilot``.

    Runs in a FRESH ``python -c`` subprocess so the current test process's
    already-imported ``autopilot`` module cannot mask a regression: the
    subprocess starts with a clean sys.modules and imports ONLY
    autopilot_markers plus its true (non-cyclic) dependencies.

    Under the lazy-import contract this exits 0 with a clean stdout.
    A regression that hoists the ``conclude_run`` import to the top of
    autopilot_markers.py would cause Python to try to load autopilot.py
    during autopilot_markers's own initialisation → autopilot.py's own
    ``from lib.tasks_pkg.autopilot_markers import ...`` (the facade
    re-export) would then see autopilot_markers as a partially-initialised
    module (its ``__dict__`` is still being populated) → ImportError.
    """
    # The subprocess script: import autopilot_markers, confirm the three
    # extracted callables are present, exit 0. Written as a heredoc-esque
    # string so it survives quoting in ``python -c``.
    script = textwrap.dedent("""
        import sys
        # Ensure THIS module is the very first `lib.tasks_pkg.*` thing loaded.
        # A prior import of `lib.tasks_pkg.autopilot` would defeat the guard.
        assert 'lib.tasks_pkg.autopilot' not in sys.modules, (
            'unexpected pre-import of lib.tasks_pkg.autopilot in subprocess')
        assert 'lib.tasks_pkg.autopilot_markers' not in sys.modules, (
            'unexpected pre-import of lib.tasks_pkg.autopilot_markers')

        import lib.tasks_pkg.autopilot_markers as m

        # Sanity: the three extracted symbols must be defined at module top
        # after the fresh import (proves the module's __init__ ran to
        # completion, not aborted midway on a cycle).
        for name in ('arm_autopilot', 'disarm_autopilot', '_marker_exists'):
            assert callable(getattr(m, name, None)), (
                'lib.tasks_pkg.autopilot_markers.' + name +
                ' missing after fresh import — lazy contract regressed')

        # Prove `lib.tasks_pkg.autopilot` was NOT pulled in as a side effect
        # of importing autopilot_markers. If a future refactor hoisted the
        # `from lib.tasks_pkg.autopilot import conclude_run` line to the
        # module top, this assertion would flip: autopilot.py would be
        # sitting in sys.modules right now.
        assert 'lib.tasks_pkg.autopilot' not in sys.modules, (
            'importing autopilot_markers pulled in lib.tasks_pkg.autopilot '
            '— the lazy-import contract has regressed. The `conclude_run` '
            'import inside disarm_autopilot() has been hoisted to module '
            'top, reintroducing the circular dependency.')

        print('OK')
    """).strip()

    proc = subprocess.run(
        [sys.executable, '-c', script],
        cwd=_ROOT,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        'Fresh-subprocess import of lib.tasks_pkg.autopilot_markers failed — '
        'the lazy-import contract is broken.\n'
        f'stdout:\n{proc.stdout}\n'
        f'stderr:\n{proc.stderr}'
    )
    assert 'OK' in proc.stdout, (
        f'Subprocess exited 0 but did not confirm OK. stdout: {proc.stdout!r}')


# ══════════════════════════════════════════════════════════
#  Guard 2 — AST-level lazy-import layout check
# ══════════════════════════════════════════════════════════

def _collect_top_level_imports_from_autopilot(tree: ast.Module) -> list[ast.ImportFrom]:
    """Return module-scope ``from lib.tasks_pkg.autopilot import ...`` nodes.

    Only inspects direct children of the Module — imports nested inside
    function bodies or class bodies are DELIBERATELY excluded, since those
    ARE the lazy contract we want to preserve.
    """
    out: list[ast.ImportFrom] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'lib.tasks_pkg.autopilot':
            out.append(node)
    return out


def _collect_conclude_run_imports_in_function(
        tree: ast.Module, func_name: str) -> list[ast.ImportFrom]:
    """Return ``from lib.tasks_pkg.autopilot import conclude_run`` nodes that
    live INSIDE the named function's body (any nesting depth, so a future
    try/except wrapper around the import still counts as function-scope).
    """
    out: list[ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if not isinstance(sub, ast.ImportFrom):
                    continue
                if sub.module != 'lib.tasks_pkg.autopilot':
                    continue
                if any(alias.name == 'conclude_run' for alias in sub.names):
                    out.append(sub)
    return out


@pytest.mark.unit
def test_disarm_autopilot_conclude_run_import_is_lazy_via_ast():
    """AST-level lock on the lazy-import layout.

    Two structural assertions on ``lib/tasks_pkg/autopilot_markers.py``:

      1. NO ``from lib.tasks_pkg.autopilot import ...`` at module scope.
         A hoisted top-level import (well-intentioned "cleanup") would
         re-introduce the circular dependency; flag it before it merges.

      2. The ``from lib.tasks_pkg.autopilot import conclude_run`` line
         MUST exist inside ``disarm_autopilot``'s function body — proves the
         lazy call is actually WIRED, not silently dropped by an edit that
         removed the try/except block.
    """
    with open(_MARKERS_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)

    # (1) No top-level import from lib.tasks_pkg.autopilot.
    module_level = _collect_top_level_imports_from_autopilot(tree)
    assert not module_level, (
        'lib/tasks_pkg/autopilot_markers.py MUST NOT import from '
        'lib.tasks_pkg.autopilot at module scope — that reintroduces the '
        'circular dependency between the two modules. Move any such import '
        'INSIDE the function body that needs it (the lazy-import pattern '
        'disarm_autopilot already uses for conclude_run).\n'
        f'Found top-level offender(s) on line(s): '
        f'{[n.lineno for n in module_level]}'
    )

    # (2) disarm_autopilot MUST lazy-import conclude_run inside its body.
    lazy_imports = _collect_conclude_run_imports_in_function(
        tree, 'disarm_autopilot')
    assert lazy_imports, (
        'lib/tasks_pkg/autopilot_markers.py::disarm_autopilot MUST lazy-'
        'import conclude_run from lib.tasks_pkg.autopilot INSIDE its function '
        'body (guard against a silent regression that removed the call).\n'
        'Expected an `from lib.tasks_pkg.autopilot import conclude_run` line '
        'somewhere inside disarm_autopilot(...) — found none.'
    )
    # Sanity: the specific line lives on a plausible position (inside the
    # try-block of the conclude_run call, which sits ~180 lines into the
    # file). We assert it's below the disarm_autopilot def line, not above.
    def_line = next(
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == 'disarm_autopilot')
    for imp in lazy_imports:
        assert imp.lineno > def_line, (
            f'Lazy conclude_run import at line {imp.lineno} is BEFORE the '
            f'disarm_autopilot def at line {def_line} — this is impossible '
            f'unless the source layout has been mangled; investigate.'
        )


# ══════════════════════════════════════════════════════════
#  Guard 3 — bonus symmetric check on autopilot.py's side
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_autopilot_py_reexports_markers_at_module_top_only():
    """The COMPLEMENT structural guard on autopilot.py's side of the cycle:
    autopilot.py's ``from lib.tasks_pkg.autopilot_markers import ...``
    re-export IS load-bearing at module top (unlike the reverse direction).
    This is safe under the lazy-import contract because autopilot_markers.py
    has ZERO top-level dependency on autopilot.py — Python resolves the pair
    with no cycle.

    Guard here: the re-export from autopilot_markers into autopilot.py MUST
    remain at module scope so ``from lib.tasks_pkg.autopilot import
    arm_autopilot`` (via facade) keeps working. A well-intentioned "move
    the re-export inside a function to symmetry with the reverse direction"
    edit would break every external caller (routes/chat_queue.py,
    lib/chat_dispatch.py, and the four sibling monkeypatch tests).
    """
    ap_path = os.path.join(_ROOT, 'lib', 'tasks_pkg', 'autopilot.py')
    with open(ap_path, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    top_level_reexports = [
        n for n in tree.body
        if isinstance(n, ast.ImportFrom)
        and n.module == 'lib.tasks_pkg.autopilot_markers'
    ]
    assert top_level_reexports, (
        'lib/tasks_pkg/autopilot.py MUST re-export from '
        'autopilot_markers at MODULE TOP — otherwise the facade contract '
        '(from lib.tasks_pkg.autopilot import arm_autopilot / disarm_autopilot '
        '/ _marker_exists) breaks for every external caller. This is the '
        'complement side of the lazy-import contract: autopilot_markers has '
        'no top-level dep on autopilot (verified by guards 1+2), so it is '
        'safe for autopilot to have a top-level dep on autopilot_markers.'
    )
    # Sanity: the re-export must actually pull the three symbols.
    imported_names: set[str] = set()
    for n in top_level_reexports:
        for alias in n.names:
            imported_names.add(alias.asname or alias.name)
    for required in ('arm_autopilot', 'disarm_autopilot', '_marker_exists'):
        assert required in imported_names, (
            f'facade re-export in autopilot.py must include {required}; '
            f'found: {sorted(imported_names)}'
        )


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
