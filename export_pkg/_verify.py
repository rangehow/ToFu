"""Export-time syntax verifiers (split out of ``export.py``)."""
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

from export_pkg._lists import C_BOLD, C_DIM, C_END, C_RED, logger

import time

from export_pkg import _state
from export_pkg._lists import (
    C_GREEN,
    ROOT,
)
class ExportSyntaxError(RuntimeError):
    """Raised when the sanitizer produced JavaScript that fails to parse.

    This is a HARD failure: shipping invalid ESM or an incomplete Vite graph
    white-screens every user. Fail the export at build time rather than on a
    fresh install.
    """
def _verify_exported_js_syntax(dest: Path) -> None:
    """Best-effort ``node --check`` over every exported ``.js`` file.

    A safety net for sanitizer-induced corruption: any find-replace in
    ``_sanitize_source_opensource`` / ``_sanitize_defaults_for_export`` that
    rewrites a code identifier into a non-identifier token (the classic
    ``meituan`` → ``your-provider`` unquoted-key bug) produces invalid JS that
    the frontend build could otherwise ship. This catches that class at EXPORT time,
    file-agnostically, so a regression can never reach a user.

    The prebuilt artifact is validated even when ``node`` is absent. Source
    syntax checking is build-host defense-in-depth; end-user hosts need no Node.

    Raises:
        ExportSyntaxError: if any exported ``.js`` file fails ``node --check``.
    """
    verifier = dest / 'scripts' / 'verify_frontend_dist.py'
    if verifier.is_file() and (dest / 'static' / 'vite').is_dir():
        result = subprocess.run(
            [sys.executable, str(verifier)], cwd=dest, capture_output=True,
            text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise ExportSyntaxError(
                (result.stderr or result.stdout or 'invalid Vite artifact').strip())

    node = shutil.which('node')
    if not node:
        print(f"  {C_DIM}\u23ed node not found \u2014 prebuilt graph validated; "
              f"skipping source syntax verification{C_END}")
        return

    js_files = [
        p for root in (dest / 'frontend' / 'src', dest / 'static' / 'vite' / 'assets')
        if root.is_dir()
        for pattern in ('*.js', '*.mjs')
        for p in root.rglob(pattern)
        if p.is_file()
    ]
    print(f"  {C_BOLD}\U0001f50d Verifying exported JS syntax ({len(js_files)} files)\u2026{C_END}")

    broken: list[tuple[str, str]] = []
    for p in js_files:
        try:
            r = subprocess.run(
                [node, '--input-type=module', '--check'],
                capture_output=True, text=True, timeout=30,
                input=p.read_text(encoding='utf-8'),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning('node --check could not run on %s: %s', p, e)
            continue
        if r.returncode != 0:
            rel = p.relative_to(dest)
            detail = (r.stderr or r.stdout or '').strip().splitlines()
            first = detail[0] if detail else 'syntax error'
            broken.append((str(rel), first))
            print(f"    {C_RED}\u2717 {rel}: {first}{C_END}")

    if broken:
        print(f"\n  {C_RED}\u26a0 {len(broken)} exported JS file(s) FAIL to parse \u2014 "
              f"the sanitizer corrupted them.{C_END}")
        raise ExportSyntaxError(
            'Sanitizer produced %d syntactically-invalid JS file(s): %s'
            % (len(broken), ', '.join(f'{rel} ({err})' for rel, err in broken))
        )


def _verify_exported_py_syntax(dest: Path) -> None:
    """``ast.parse`` every exported ``.py`` — the Python twin of the JS check.

    The 2026-08-05 incident proved the gap: the sanitizer hyphenated an
    identifier in tests/llm_sanitize/test_gateway_sanitize.py, the JS-only net never looked
    at it, and the corruption was only caught because CI lints the whole tree.
    ast.parse is fast (~1k files in seconds) and needs no toolchain, so this
    runs unconditionally. Scope is the whole exported tree (CI runs
    ``ruff check .`` over everything, including tests/ and scripts/).

    Raises:
        ExportSyntaxError: if any exported ``.py`` file fails to parse.
    """
    py_files = [p for p in dest.rglob('*.py')
                if p.is_file() and '.git' not in p.parts]
    print(f"  {C_BOLD}\U0001f50d Verifying exported Python syntax ({len(py_files)} files)\u2026{C_END}")

    broken: list[tuple[str, str]] = []
    for p in py_files:
        try:
            src = p.read_text(encoding='utf-8')
            ast.parse(src, filename=str(p))
        except SyntaxError as e:
            rel = p.relative_to(dest)
            broken.append((str(rel), f'{e.msg} (line {e.lineno})'))
            print(f"    {C_RED}\u2717 {rel}: {e.msg} (line {e.lineno}){C_END}")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning('py-syntax: could not read %s: %s', p, e)

    if broken:
        print(f"\n  {C_RED}\u26a0 {len(broken)} exported Python file(s) FAIL to parse \u2014 "
              f"the sanitizer corrupted them.{C_END}")
        raise ExportSyntaxError(
            'Sanitizer produced %d syntactically-invalid Python file(s): %s'
            % (len(broken), ', '.join(f'{rel} ({err})' for rel, err in broken))
        )


class ExportTestSurfaceError(RuntimeError):
    """Raised when exported tests import first-party modules the export dropped.

    Why this gate exists (2026-09 CI archaeology): the opensource export
    strips the root ``scripts/`` tree except an explicit keep list and drops
    several first-party roots (``tofu_db/``, ``bundles/`` …), yet ships the
    full ``tests/`` tree verbatim. Every test that imports a stripped module
    then fails in the public CI with ``ModuleNotFoundError`` at COLLECTION
    time — rangehow/ToFu went 41 CI runs with exactly two greens, and the
    dominant error class was tests importing modules the export had removed
    (``scripts.gen_wire_fixtures``, ``scripts.tofudb_v2_client``,
    ``lib.database``). Worse, keep-list fixes landed internally but no gate
    forced the exported test surface to match the exported module surface, so
    the same class kept returning.

    This check makes the invariant explicit and mechanical: **a test shipped
    in the public tree must be collectable against the public tree.** It
    AST-scans the exported tests (no imports executed, so it is fast and
    cannot run arbitrary code) and fails the export when a top-level import
    names a first-party module that is absent from the export. The operator
    then either keeps the module (extend ``_OPENSOURCE_KEEP_FILES``) or drops
    the test for opensource mode — a deliberate decision, not drift.
    """


def _first_party_roots(src: Path) -> frozenset[str]:
    """Top-level source-tree names that are Python-importable.

    A directory qualifies when it contains at least one ``.py`` within a
    bounded depth (covers namespace packages without ``__init__.py``); a
    top-level ``.py`` file qualifies as a module. Heavy/generated/data roots
    are skipped without descending.
    """
    _SKIP = {
        '.git', '.venv', 'venv', 'node_modules', 'static', 'data', 'logs',
        'outputs', 'uploads', '__pycache__', '.pytest_cache', '.ruff_cache',
        'dist', 'build', 'htmlcov', 'docs', 'designs', 'deploy', '.tofu',
        '.chatui', '.project_sessions', '.project_indexes', '.tofu_trash',
    }
    roots: set[str] = set()
    try:
        entries = sorted(os.scandir(src), key=lambda e: e.name)
    except OSError as e:
        logger.warning('test-surface: cannot list source root %s: %s', src, e)
        return frozenset()
    for entry in entries:
        name = entry.name
        if name in _SKIP or name.startswith('.'):
            continue
        if entry.is_file() and name.endswith('.py'):
            roots.add(name[:-3])
            continue
        if not entry.is_dir():
            continue
        base = src / name
        found = False
        for dirpath, dirnames, filenames in os.walk(base):
            rel = os.path.relpath(dirpath, base)
            if rel != os.curdir and rel.count(os.sep) >= 2:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP and not d.startswith('.')]
            if any(f.endswith('.py') for f in filenames):
                found = True
                break
        if found:
            roots.add(name)
    return frozenset(roots)


def _module_resolves(dest: Path, dotted: str) -> bool:
    """Return True when ``dotted`` resolves to a module file/package in dest.

    Resolution stops at the first module FILE (later parts are attribute
    accesses, which static analysis cannot disprove). A package ``__init__``
    defining ``__getattr__`` (PEP 562 lazy re-export) tolerates any missing
    remainder, mirroring the import system.
    """
    node = dest
    parts = dotted.split('.')
    for index, part in enumerate(parts):
        if (node / f'{part}.py').is_file():
            return True
        cand_dir = node / part
        if cand_dir.is_dir():
            node = cand_dir
            continue
        init = node / '__init__.py'
        try:
            if init.is_file() and '__getattr__' in init.read_text(
                    encoding='utf-8', errors='replace'):
                return True
        except OSError:
            pass
        return False
    return True


def _guarded_module_imports(
        tree: ast.AST,
) -> tuple[list[tuple[str, tuple[str, ...], int]], set[str], list[tuple[str, int]]]:
    """Split module-scope imports into (required, optional-roots, skip guards).

    Each required entry is ``(dotted, names, lineno)``: ``import a.b.c``
    yields ``('a.b.c', (), line)`` and ``from a.b import c, d`` yields
    ``('a.b', ('c', 'd'), line)``. Optional (tolerated) = imports under any
    ``try`` block, an ``if TYPE_CHECKING`` guard, or any function/class body.
    Skip guards are module-level ``pytest.importorskip('root')`` calls as
    ``(root, lineno)``: when ``root`` is absent from the export, pytest skips
    the whole module at that line, so later imports can never execute — a
    deliberate conditional-availability seam (the suite's ``export`` gate).
    """
    required: list[tuple[str, tuple[str, ...], int]] = []
    optional: set[str] = set()
    skip_guards: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0       # function/class nesting
            self.guarded = 0     # try / TYPE_CHECKING nesting

        def _record(self, dotted: str, names: tuple[str, ...] = (),
                    lineno: int = 0) -> None:
            if self.depth or self.guarded:
                optional.add(dotted.split('.')[0])
            else:
                required.append((dotted, names, lineno))

        def visit_FunctionDef(self, node):  # noqa: N802
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):  # noqa: N802
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_Try(self, node):  # noqa: N802
            self.guarded += 1
            self.generic_visit(node)
            self.guarded -= 1

        def visit_If(self, node):  # noqa: N802
            type_checking = (
                (isinstance(node.test, ast.Name) and node.test.id == 'TYPE_CHECKING')
                or (isinstance(node.test, ast.Attribute)
                    and node.test.attr == 'TYPE_CHECKING'))
            if type_checking:
                self.guarded += 1
                self.generic_visit(node)
                self.guarded -= 1
            else:
                self.generic_visit(node)

        def visit_Import(self, node):  # noqa: N802
            for alias in node.names:
                self._record(alias.name, lineno=node.lineno)

        def visit_ImportFrom(self, node):  # noqa: N802
            if node.level or not node.module:
                return
            names = tuple(a.name for a in node.names if a.name != '*')
            self._record(node.module, names, node.lineno)

        def visit_Call(self, node):  # noqa: N802
            func = node.func
            is_importorskip = (
                (isinstance(func, ast.Attribute) and func.attr == 'importorskip')
                or (isinstance(func, ast.Name) and func.id == 'importorskip'))
            if is_importorskip and node.args and isinstance(
                    node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                root = node.args[0].value.split('.')[0]
                if self.depth or self.guarded:
                    optional.add(root)
                else:
                    skip_guards.append((root, node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return required, optional, skip_guards


def _is_submodule_in_src(src: Path, dotted: str, name: str) -> bool:
    """True when ``name`` is a real submodule of ``dotted`` in the SOURCE tree.

    Distinguishes ``from scripts import gen_wire_fixtures`` (a submodule the
    export may drop) from ``from lib.pricing import pricing_for_model`` (a
    plain attribute re-export, invisible to static analysis).
    """
    base = src.joinpath(*dotted.split('.'))
    return (base / f'{name}.py').is_file() or (base / name / '__init__.py').is_file()


def _test_surface_findings(dest: Path, src: Path) -> list[tuple[str, str]]:
    """Return (test file, missing first-party module) pairs for an export."""
    tests_dir = dest / 'tests'
    if not tests_dir.is_dir():
        return []
    first_party = _first_party_roots(src)
    findings: list[tuple[str, str]] = []
    for path in sorted(tests_dir.rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'),
                             filename=str(path))
        except (SyntaxError, OSError, UnicodeDecodeError) as e:
            logger.warning('test-surface: skipping unparseable %s: %s', path, e)
            continue
        required, _optional_roots, skip_guards = _guarded_module_imports(tree)
        rel = path.relative_to(dest).as_posix()
        # A module-level importorskip on a missing first-party root skips the
        # whole module at that line; nothing after it can execute.
        active_skip_lines = [
            lineno for root, lineno in skip_guards
            if root in first_party and not _module_resolves(dest, root)]
        for dotted, names, lineno in required:
            root = dotted.split('.')[0]
            if root not in first_party:
                continue
            if any(skip_line < lineno for skip_line in active_skip_lines):
                continue
            if not _module_resolves(dest, dotted):
                findings.append((rel, dotted))
                continue
            for name in names:
                if _is_submodule_in_src(src, dotted, name) and not _module_resolves(
                        dest, f'{dotted}.{name}'):
                    findings.append((rel, f'{dotted}.{name}'))
    return findings


def _verify_exported_test_surface(dest: Path, src: Path | None = None) -> None:
    """Fail the export when shipped tests import modules the export dropped.

    ``src`` is the source checkout used to decide which import roots are
    first-party (defaults to the repository this export is running from).

    Raises:
        ExportTestSurfaceError: listing the offending (test, module) pairs.
    """
    source = src if src is not None else ROOT
    findings = _test_surface_findings(dest, source)
    if not findings:
        print(f"  {C_GREEN}✅ Exported test surface imports only shipped modules.{C_END}")
        return
    by_module: dict[str, int] = {}
    for _rel, dotted in findings:
        by_module[dotted] = by_module.get(dotted, 0) + 1
    top = sorted(by_module.items(), key=lambda kv: -kv[1])[:10]
    print(f"\n  {C_RED}⚠ {len(findings)} exported-test import(s) resolve to modules "
          f"ABSENT from the export:{C_END}")
    for rel, dotted in findings[:20]:
        print(f"    {C_RED}✗ {rel}: {dotted}{C_END}")
    print(f"  {C_DIM}Most-missed modules: "
          f"{', '.join(f'{m} (x{n})' for m, n in top)}\n"
          f"  Either keep the module (extend _OPENSOURCE_KEEP_FILES / the opensource "
          f"dir set) or exclude the test for opensource mode — a test shipped in the "
          f"public tree must be collectable against it.{C_END}")
    raise ExportTestSurfaceError(
        'Exported test surface references %d missing first-party module(s): %s'
        % (len(findings), ', '.join(f'{rel} -> {dotted}'
                                    for rel, dotted in findings[:30])))


class ExportIntegrityError(RuntimeError):
    """Raised when the exported Python tree is missing/truncated vs the source.

    The classic failure this guards against: a facade ``__init__.py`` that
    re-exports symbols from a submodule ends up 0 bytes in the export (a
    concurrent export into the same ``--dest`` + a cross-DC FUSE atomic-move
    window can leave a half-written file). A 0-byte file still ``py_compile``s
    cleanly, so a syntax check would NOT catch it — only a byte-size check
    against the source does. The result is a tree that installs "successfully"
    yet crashes at boot with ``ImportError: cannot import name X`` the moment
    ``routes/__init__.py`` touches the empty facade. Fail the export loudly at
    build time instead of shipping that.
    """


# ═══════════════════════════════════════════════════════════
#  Post-copy integrity verification
# ═══════════════════════════════════════════════════════════
def _verify_exported_py_integrity(dest: Path) -> None:
    """Verify every tracked source ``.py`` copied to ``dest`` non-empty.

    Compares the git-tracked Python file list (fast, local) against the export:
    a file that is non-empty in the source but MISSING or 0 bytes in ``dest``
    is a corrupted copy. Each dest stat is retried a few times to absorb
    cross-datacenter FUSE latency / atomic-move windows (a spurious ENOENT on a
    remote mount would otherwise flag a file that is actually fine).

    Raises:
        ExportIntegrityError: if any tracked source ``.py`` is missing or
            truncated to 0 bytes in the export.
    """
    try:
        listed = subprocess.run(
            ['git', 'ls-tree', '-r', '--name-only', _state._EXPORT_SOURCE_SHA or 'HEAD'],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning('py-integrity: git ls-files failed (%s) — skipping check', e)
        print(f"  {C_DIM}\u23ed git ls-files unavailable \u2014 skipping Python integrity check{C_END}")
        return

    rels = [ln.strip() for ln in (listed.stdout or '').splitlines()
            if ln.strip().endswith('.py')
            and (ln.strip().startswith(('lib/', 'routes/'))
                 or ln.strip() in (
                     'server.py', 'serverctl.py', 'server_manager.py',
                     'supervisor.py', 'bootstrap.py'))]
    print(f"  {C_BOLD}\U0001f50d Verifying exported Python integrity ({len(rels)} files)\u2026{C_END}")

    def _size_with_retry(p: Path) -> int:
        """Return file size, retrying to ride out FUSE ENOENT flakiness.

        Returns -1 only if every attempt failed (treated as truly missing).
        """
        for attempt in range(5):
            try:
                return p.stat().st_size
            except OSError as e:
                logger.debug('py-integrity stat retry %d for %s: %s', attempt, p, e)
                time.sleep(0.4)
        return -1

    bad: list[tuple[str, str]] = []
    for rel in rels:
        # Only flag files that are genuinely non-empty in the source: an empty
        # source file (rare) copying to empty is not corruption.
        try:
            if (ROOT / rel).stat().st_size == 0:
                continue
        except OSError:
            continue
        size = _size_with_retry(dest / rel)
        if size < 0:
            bad.append((rel, 'missing'))
            print(f"    {C_RED}\u2717 {rel}: missing{C_END}")
        elif size == 0:
            bad.append((rel, '0 bytes'))
            print(f"    {C_RED}\u2717 {rel}: 0 bytes (truncated copy){C_END}")

    if bad:
        print(f"\n  {C_RED}\u26a0 {len(bad)} exported Python file(s) missing/truncated \u2014 "
              f"the export tree is incomplete.{C_END}")
        print(f"  {C_DIM}Likely cause: a concurrent export into the same --dest, or a "
              f"cross-DC FUSE write interrupted mid-copy. Re-run the export (ensure no "
              f"other export.py targets this dest).{C_END}")
        raise ExportIntegrityError(
            'Export tree incomplete: %d tracked .py file(s) missing/empty: %s'
            % (len(bad), ', '.join(f'{rel} ({why})' for rel, why in bad))
        )
    print(f"  {C_GREEN}\u2705 All exported Python files present and non-empty.{C_END}")
