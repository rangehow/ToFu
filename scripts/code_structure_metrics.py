#!/usr/bin/env python3
"""Measure the repository's code structure: the baseline docs/CODE_STRUCTURE_AUDIT.md records.

Inputs: the checkout's tracked and untracked non-ignored files (``git ls-files``).
Output: a JSON report (``--json``) or a Markdown summary on stdout:

* Python lines (non-blank) by top-level package, and inside ``lib/`` by package;
* the largest modules and functions;
* module-level import cycles among ``lib``/``routes`` (strongly connected
  components of the top-level import graph; function-level and
  ``TYPE_CHECKING`` imports are lazy by construction and excluded);
* duplicate implementations: functions in different modules whose normalized
  AST bodies are identical, and private helper names defined in many modules;
* dead-code candidates: ``lib``/``routes`` modules no tracked file imports or
  names, and top-level functions whose identifier occurs exactly once in the
  tracked text (its definition).

Dependencies: standard library only. Read-only; the candidates are evidence
for a human or agent decision, never deleted automatically.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# Cold reads on the network volume dominate; overlap them (as check_function_sizes does).
_CONCURRENT_READS = 32
_TEXT_SUFFIXES = frozenset({'.py', '.js', '.mjs', '.ts', '.json', '.yaml', '.yml', '.toml',
                            '.cfg', '.ini', '.sh', '.html', '.md', '.txt', '.rs'})
_GRAPH_ROOTS = ('lib', 'routes')
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_QUALIFIED = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z_][A-Za-z0-9_]*)+')
# Framework hooks are called by name from outside the repository.
_EXTERNAL_HOOK_PREFIXES = ('pytest_',)
# Report sizes: how much of each ranking the summary prints, not a product limit.
_REPORT_ROWS = 25
# A body this short (statements) is a trivial accessor; identical copies are not a finding.
_MIN_DUPLICATE_STATEMENTS = 4


def tracked_files(root: Path = ROOT) -> list[str]:
    # Untracked, non-ignored files count too: a new module is gated before its first commit.
    out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                          '--exclude-standard'], check=True,
                         capture_output=True).stdout.decode('utf-8', 'surrogateescape')
    return sorted(name for name in out.split('\0') if name)


def _read(root: Path, name: str) -> str | None:
    try:
        return (root / name).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None


def module_name(path: str) -> str:
    parts = path[:-3].split('/')
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING') or (
        isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')


def _toplevel_imports(tree: ast.Module):
    """Imports executed when the module loads: top level, including try/if bodies."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            if not _is_type_checking(node):
                stack.extend(node.body + node.orelse)
        elif isinstance(node, ast.Try):
            stack.extend(node.body + node.orelse + node.finalbody)
            for handler in node.handlers:
                stack.extend(handler.body)
        elif isinstance(node, ast.With):
            stack.extend(node.body)


def _all_imports(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def _resolve(node, module: str, is_package: bool, modules: set[str]) -> list[str]:
    """The tracked modules an import statement loads (its target and named submodules)."""
    if isinstance(node, ast.Import):
        targets = [alias.name for alias in node.names]
    else:
        if node.level:
            base = module.split('.') if is_package else module.split('.')[:-1]
            base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
            prefix = '.'.join(base + ([node.module] if node.module else []))
        else:
            prefix = node.module or ''
        submodules = [f'{prefix}.{alias.name}' for alias in node.names]
        # ``from pkg import sub`` of submodules only needs ``pkg`` in sys.modules,
        # not its finished body: it is no dependency on the package facade.
        named_only_submodules = all(name in modules for name in submodules)
        targets = submodules if named_only_submodules else [prefix] + submodules
    resolved = []
    for target in targets:
        while target and target not in modules:
            target = target.rpartition('.')[0]
        if target:
            resolved.append(target)
    return resolved


def _sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components (iterative), size > 1 only."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    return sorted(result, key=lambda c: (-len(c), c))


def _body_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], 'value', None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    statements = sum(1 for stmt in body for _ in ast.walk(stmt) if isinstance(_, ast.stmt))
    if statements < _MIN_DUPLICATE_STATEMENTS:
        return None
    dumped = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
    dumped += ast.dump(node.args, annotate_fields=False)
    return hashlib.sha256(dumped.encode()).hexdigest()


def _eager_graph(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    """Module -> modules its load-time imports execute, among ``trees``."""
    modules = {module_name(n) for n in trees}
    eager: dict[str, set[str]] = {module: set() for module in modules}
    for name, tree in trees.items():
        module = module_name(name)
        for node in _toplevel_imports(tree):
            for target in _resolve(node, module, name.endswith('__init__.py'), modules):
                if target != module:
                    eager[module].add(target)
    return eager


def import_cycles(root: Path = ROOT) -> list[list[str]]:
    """Module-level import cycles among the tracked ``lib``/``routes`` modules (sorted SCCs)."""
    names = [n for n in tracked_files(root) if n.endswith('.py') and n.split('/')[0] in _GRAPH_ROOTS]
    with ThreadPoolExecutor(_CONCURRENT_READS) as pool:
        texts = dict(zip(names, pool.map(lambda n: _read(root, n), names)))
    trees = {}
    for name, text in texts.items():
        if text is None:
            continue
        try:
            trees[name] = ast.parse(text, filename=name)
        except SyntaxError:
            continue
    return _sccs(_eager_graph(trees))


def measure(root: Path = ROOT) -> dict:
    names = tracked_files(root)
    text_names = [n for n in names if Path(n).suffix in _TEXT_SUFFIXES or Path(n).name == 'Makefile']
    with ThreadPoolExecutor(_CONCURRENT_READS) as pool:
        texts = dict(zip(text_names, pool.map(lambda n: _read(root, n), text_names)))
    py = {n: t for n, t in texts.items() if n.endswith('.py') and t is not None}
    trees: dict[str, ast.Module] = {}
    for name, text in py.items():
        try:
            trees[name] = ast.parse(text, filename=name)
        except SyntaxError:
            continue

    def loc(text: str) -> int:
        return sum(1 for line in text.splitlines() if line.strip())

    by_package: Counter = Counter()
    by_lib_package: Counter = Counter()
    module_sizes = []
    for name, text in py.items():
        lines = loc(text)
        parts = name.split('/')
        by_package[parts[0] if len(parts) > 1 else '<root>'] += lines
        if parts[0] == 'lib':
            by_lib_package[parts[1] if len(parts) > 2 else '<lib root files>'] += lines
        if parts[0] not in ('tests',):
            module_sizes.append((lines, name))

    functions = []
    fingerprints: dict[str, list[str]] = defaultdict(list)
    private_helpers: dict[str, set[str]] = defaultdict(set)
    toplevel_functions: list[tuple[str, str, int]] = []
    for name, tree in trees.items():
        production = not name.startswith('tests/') and '/tests/' not in name
        def visit(node, prefix=''):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    dotted = f'{prefix}{child.name}'
                    if not isinstance(child, ast.ClassDef) and production:
                        size = (child.end_lineno or child.lineno) - child.lineno + 1
                        functions.append((size, f'{name}:{dotted}'))
                        fingerprint = _body_fingerprint(child)
                        if fingerprint and name.split('/')[0] in _GRAPH_ROOTS:
                            fingerprints[fingerprint].append(f'{name}:{dotted}')
                    visit(child, dotted + '.')
        visit(tree)
        if name.split('/')[0] in _GRAPH_ROOTS:
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith('_') and not node.name.startswith('__'):
                        private_helpers[node.name].add(name)
                    if not node.decorator_list:
                        toplevel_functions.append((name, node.name, node.lineno))

    modules = {module_name(n) for n in trees}
    graph_modules = {module_name(n): n for n in trees if n.split('/')[0] in _GRAPH_ROOTS}
    imported_by: dict[str, set[str]] = defaultdict(set)
    for name, tree in trees.items():
        module = module_name(name)
        is_package = name.endswith('__init__.py')
        for node in _all_imports(tree):
            for target in _resolve(node, module, is_package, modules):
                if target != module:
                    imported_by[target].add(name)
    cycles = _sccs(_eager_graph({n: t for n, t in trees.items() if n.split('/')[0] in _GRAPH_ROOTS}))

    tokens: Counter = Counter()
    qualified: set[str] = set()
    for text in texts.values():
        if text is not None:
            tokens.update(_IDENTIFIER.findall(text))
            for reference in set(_QUALIFIED.findall(text)):
                # Every prefix: 'lib.a.b.c' also names the modules lib.a.b and lib.a.
                for cut in re.finditer(r'[./]', reference):
                    qualified.add(reference[:cut.start()])
                qualified.add(reference)
    texts.clear()

    dead_modules = []
    for module, path in sorted(graph_modules.items()):
        if path.endswith('__init__.py') or imported_by.get(module):
            continue
        leaf = module.rpartition('.')[2]
        if module in qualified or path in qualified or path[:-3] in qualified:
            continue
        if leaf in ('__main__', 'conftest'):
            continue
        dead_modules.append(path)

    dead_functions = sorted(f'{path}:{line}:{fn}' for path, fn, line in toplevel_functions
                            if tokens[fn] == 1 and not fn.startswith('__')
                            and not fn.startswith(_EXTERNAL_HOOK_PREFIXES))

    duplicates = sorted((sorted(set(v)) for v in fingerprints.values()
                         if len({p.split(':')[0] for p in v}) > 1), key=lambda g: (-len(g), g))
    helper_clusters = sorted(((name, sorted(paths)) for name, paths in private_helpers.items()
                              if len(paths) >= 3), key=lambda item: (-len(item[1]), item[0]))

    return {
        'python_files': len(py),
        'python_loc_total': sum(by_package.values()),
        'python_loc_by_package': dict(by_package.most_common()),
        'lib_loc_by_package': dict(by_lib_package.most_common()),
        'largest_modules': [{'path': p, 'loc': n} for n, p in sorted(module_sizes, reverse=True)[:_REPORT_ROWS]],
        'largest_functions': [{'function': f, 'lines': n} for n, f in sorted(functions, reverse=True)[:_REPORT_ROWS]],
        'import_cycles': cycles,
        'import_cycle_modules': sum(len(c) for c in cycles),
        'duplicate_function_bodies': duplicates,
        'duplicate_helper_names': [{'name': n, 'modules': p} for n, p in helper_clusters],
        'dead_module_candidates': dead_modules,
        'dead_function_candidates': dead_functions,
    }


def summary(report: dict) -> str:
    out = [f"Python files: {report['python_files']}; non-blank LOC: {report['python_loc_total']}", '',
           'LOC by top-level package:']
    out += [f'  {k}: {v}' for k, v in report['python_loc_by_package'].items()]
    out += ['', 'lib/ LOC by package (top):']
    out += [f'  {k}: {v}' for k, v in list(report['lib_loc_by_package'].items())[:_REPORT_ROWS]]
    out += ['', 'Largest modules:'] + [f"  {r['loc']:6} {r['path']}" for r in report['largest_modules']]
    out += ['', 'Largest functions:'] + [f"  {r['lines']:6} {r['function']}" for r in report['largest_functions']]
    out += ['', f"Module-level import cycles (lib/routes): {len(report['import_cycles'])} "
            f"covering {report['import_cycle_modules']} modules"]
    out += [f'  [{len(c)}] ' + ' -> '.join(c[:8]) + (' ...' if len(c) > 8 else '') for c in report['import_cycles']]
    out += ['', f"Duplicate function bodies across modules: {len(report['duplicate_function_bodies'])} groups"]
    out += ['  ' + ', '.join(g) for g in report['duplicate_function_bodies'][:_REPORT_ROWS]]
    out += ['', f"Private helper names defined in >=3 modules: {len(report['duplicate_helper_names'])}"]
    out += [f"  {r['name']}: {len(r['modules'])}" for r in report['duplicate_helper_names'][:_REPORT_ROWS]]
    out += ['', f"Dead module candidates: {len(report['dead_module_candidates'])}"]
    out += [f'  {p}' for p in report['dead_module_candidates']]
    out += ['', f"Dead top-level function candidates: {len(report['dead_function_candidates'])}"]
    out += [f'  {p}' for p in report['dead_function_candidates']]
    return '\n'.join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--json', action='store_true', help='print the full JSON report')
    args = parser.parse_args(argv)
    report = measure()
    print(json.dumps(report, indent=1) if args.json else summary(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
