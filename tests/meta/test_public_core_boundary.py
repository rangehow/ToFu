"""Public-core boundary: core never depends on the private company overlay.

Executable form of docs/PUBLIC_CORE.md. The public core (shipped runtime code
under ``CORE_ROOTS``) must run with no company overlay installed, so it may
read overlay *data* only through declared registries (``tofu_extensions``
bundle discovery, entry points) and never import overlay *code*.

Three checks:

1. Overlay distributions (``OVERLAY_MODULES``) are never imported by core —
   statically, lazily inside a function, or by a dotted-name string handed to
   an importer. This set is empty and stays empty.
2. Company modules still physically inside the core tree
   (``INTERNAL_MODULES_IN_CORE``) are reached only from the importers listed in
   ``INTERNAL_IMPORT_DEBT``. Both are empty since increment 3; the modules that
   moved out (``RETIRED_INTERNAL_MODULES``) are refused like overlay code.
3. Company hosts/paths in executable string values (docstrings and comments
   excluded) appear only where ``HOST_LITERAL_DEBT`` lists them, with exact
   counts; frontend sources use the same exact-count ledger per file.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

#: Shipped runtime code of the public core.
CORE_ROOTS = ('lib/', 'routes/', 'serverctl_pkg/', 'bootstrap_pkg/', 'tofu/', 'tofu_agent/',
              'tofu_workers/', 'rootless_vm/', 'packages/', 'plugins/')
#: Trees that belong to the private overlay or to the private publication toolchain.
OVERLAY_PATHS = ('bundles/', 'packages/tofu-mtsso-auth/', 'export_pkg/', 'export.py')
#: Importable names that exist only in the overlay (or private toolchain).
OVERLAY_MODULES = ('bundles', 'export_pkg', 'hope_mcp', 'llm_mcp', 'tofu_yourprovider', 'tofu_yourprovider_environments',
                   'tofu_mtsso_auth', 'xuecheng_mcp')
#: Company modules that used to live in core and moved behind the plugin
#: interface (docs/PUBLIC_CORE.md increments 2-3); re-creating one fails.
RETIRED_INTERNAL_MODULES = ('lib.catpaw_local_import', 'lib.channels.dx', 'lib.llm.catpaw_gateway')
#: Company modules still inside the core tree, with the increment that moves
#: them, and their exact importers. Empty since increment 3; a new row needs
#: an increment that removes it.
INTERNAL_MODULES_IN_CORE: dict[str, str] = {}
INTERNAL_IMPORT_DEBT: frozenset[tuple[str, str]] = frozenset()
HOST_PATTERN = re.compile(r'example-corp\.com|yourprovider\.com|' + '/mnt/' + 'dolphinfs', re.IGNORECASE)
HOST_LITERAL_DEBT: dict[str, int] = {
    # The off-host repair relay forwards to the project gateway while Tofu
    # itself may be down, so no routing document is readable then; the
    # owner's watcher configuration (repairAgent) lives off this host and
    # names no upstream today. Moving the default needs that config first
    # (docs/PUBLIC_CORE.md remaining increment 5b).
    'serverctl_pkg/oob_relay.py': 1,
}
FRONTEND_HOST_LINE_DEBT: dict[str, int] = {}


def _tracked(*pathspecs: str) -> list[str]:
    output = subprocess.run(['git', 'ls-files', '-z', '--', *pathspecs], cwd=ROOT,
                            check=True, capture_output=True).stdout
    return [path for path in output.decode('utf-8').split('\0') if path]


def _is_test_path(path: str) -> bool:
    parts = path.split('/')
    return ('tests' in parts or 'testing' in parts or parts[-1].startswith('test_')
            or '.test.' in parts[-1] or parts[-1] == 'conftest.py')


def core_python_files() -> list[str]:
    root_modules = [path for path in _tracked('*.py') if '/' not in path]
    files = [path for path in _tracked(*CORE_ROOTS) if path.endswith('.py')] + root_modules
    return sorted(path for path in files
                  if not _is_test_path(path) and not path.startswith(OVERLAY_PATHS))


def _module_name(path: str) -> str:
    return path[:-3].replace('/', '.').removesuffix('.__init__')


def _resolve(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ''
    base = module.split('.')
    base = base[:len(base) - node.level + (1 if is_package else 0)]
    return '.'.join([*base, *([node.module] if node.module else [])])


def _is_within(name: str, module: str) -> bool:
    return name == module or name.startswith(module + '.')


def _doc_string_ids(tree: ast.AST) -> set[int]:
    """String constants that are documentation: any bare string statement."""
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def scan_core() -> tuple[set[tuple[str, str]], Counter]:
    """Return (importer path, imported internal/overlay module) edges and host literals."""
    targets = (*OVERLAY_MODULES, *RETIRED_INTERNAL_MODULES, *INTERNAL_MODULES_IN_CORE)
    edges: set[tuple[str, str]] = set()
    hosts: Counter = Counter()
    for path in core_python_files():
        source = (ROOT / path).read_text(encoding='utf-8')
        tree = ast.parse(source, filename=path)
        module = _module_name(path)
        is_package = path.endswith('__init__.py')
        documentation = _doc_string_ids(tree)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve(node, module, is_package)
                names = [base, *(f'{base}.{alias.name}' for alias in node.names)]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in documentation and HOST_PATTERN.search(node.value):
                    hosts[path] += 1
                # A dotted module path handed to importlib / a lazy-import table.
                names = [node.value] if re.fullmatch(r'\w+(?:\.\w+)+', node.value) else []
            for name in names:
                for target in targets:
                    if _is_within(name, target) and not _is_within(module, target):
                        edges.add((path, target))
    return edges, hosts


def frontend_host_lines() -> Counter:
    counts: Counter = Counter()
    for path in _tracked('frontend/src/'):
        if _is_test_path(path) or not path.endswith(('.ts', '.js', '.mjs', '.html', '.css')):
            continue
        if path.endswith('app-runtime.js'):
            continue  # generated from frontend/src/runtime/sections/, which is scanned
        text = (ROOT / path).read_text(encoding='utf-8')
        hits = sum(1 for line in text.splitlines()
                   if HOST_PATTERN.search(line) and not line.lstrip().startswith(('//', '*', '/*')))
        if hits:
            counts[path] = hits
    return counts


@pytest.fixture(scope='module')
def scanned():
    return scan_core()


def test_core_never_imports_overlay_code(scanned):
    edges, _hosts = scanned
    offenders = sorted(edge for edge in edges if edge[1] in (*OVERLAY_MODULES, *RETIRED_INTERNAL_MODULES))
    assert offenders == [], (
        'core imports private-overlay code; read overlay data through '
        'tofu_extensions or contribute code through an entry point instead: '
        f'{offenders}')


def test_internal_modules_in_core_are_reached_only_by_recorded_debt(scanned):
    edges, _hosts = scanned
    actual = {edge for edge in edges if edge[1] in INTERNAL_MODULES_IN_CORE}
    new = sorted(actual - INTERNAL_IMPORT_DEBT)
    paid = sorted(INTERNAL_IMPORT_DEBT - actual)
    assert not new, f'new core -> company-module imports (use a plugin registry): {new}'
    assert not paid, f'debt paid down; delete these rows from INTERNAL_IMPORT_DEBT: {paid}'


def test_company_host_literals_only_shrink(scanned):
    _edges, hosts = scanned
    assert dict(hosts) == HOST_LITERAL_DEBT, (
        'company host/path literals in core changed; replace a new one with '
        'configuration (gateway profile, bundle data) or record why, and delete '
        f'paid rows. actual={dict(sorted(hosts.items()))}')


def test_frontend_company_host_lines_only_shrink():
    assert dict(frontend_host_lines()) == FRONTEND_HOST_LINE_DEBT, (
        f'actual={dict(sorted(frontend_host_lines().items()))}')
