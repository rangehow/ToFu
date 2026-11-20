#!/usr/bin/env python3
"""Generate the Phase 0 test classification manifest for the Rust rewrite.

The manifest (contract ``tofu.test-classification/v1``) is derived on demand
and never committed: it is a function of every test file, so a committed copy
went stale with any test change (docs/RUST_REWRITE_EVIDENCE.md "Progress",
"The test classification manifest is derived on demand"). Authored input:
``scripts/test_classification_parity_cases.json`` (the parity mapping) and the
"Behaviours that are easy to lose" table of ``docs/RUST_REWRITE_EVIDENCE.md``.
Usage: ``python3 scripts/gen_test_classification.py`` prints the manifest;
``--output PATH`` writes it atomically; ``--check`` (``make contracts-check``)
only builds it, which fails on an unmapped or stale parity row, a missing
referenced test or an unparsable test file. Consumers in Python call
``build_manifest``. Standard library only; deterministic for a given work tree.

Which files are classified
  Every path under ``tests/`` in Git's view of the work tree (``git ls-files
  --cached --others --exclude-standard``: tracked plus not-ignored new files,
  so a test is classified before its first commit) that is a Python test module
  (``test_*.py`` or ``*_test.py``) or a JS/TS test (``*.test.{js,mjs,cjs,ts,tsx}``).
  Python ``testCount`` counts every ``def test_*`` / ``async def test_*``
  (functions, methods and nested definitions) through ``ast``. JS
  ``testCount`` uses the inventory regex of the evidence document: ``it(`` or
  ``test(`` (optionally ``.each(...)``) at line start.
  ``owner`` is ``lib/<x>`` when ``tests/<x>/`` has a tracked ``lib/<x>/``
  directory, else ``tests/<x>`` (``tests`` for top-level files).

Surfaces and their evidence (Python, from the module's syntax tree)
  http          a test or fixture parameter named ``flask_client``,
                ``live_server`` or an HTTP fixture defined by a ``conftest.py``
                above the file (``fixture:<name>``); any ``.test_client(``
                call (``call:test_client``). A conftest fixture is an HTTP
                fixture when it calls ``.test_client(`` or takes/names
                (``getfixturevalue``) another HTTP fixture.
  sse           http evidence plus a ``text/event-stream`` string literal.
  websocket     a ``.websocket(`` call, an import of ``websockets``/``wsproto``,
                or a ``/api/push`` string literal together with http or
                process evidence.
  storage-wire  an import of the storage wire client or frame codec:
                ``lib.storage.{client,protocol,v2_client,connection_file}``,
                ``tofu_db.wire``; or the ``TOFU_TEST_TOFUDB_BIN`` literal (an
                engine binary the test talks to).
  process       an import of ``tests.support.scratch_server`` or
                ``tests.chaos.instance``; or a subprocess launch
                (``subprocess.*``/``asyncio.create_subprocess_*``) whose
                arguments (or a value assigned to a name passed as one)
                contain a server entry literal (``server.py``,
                ``serverctl``, ``execution_host``, ``supervisor.sh``).
  A Python file with at least one surface is ``black-box`` (it exercises
  HTTP, SSE, WebSocket, storage wire or process behaviour and can be pointed
  at a server URL or process later); otherwise it is ``white-box``
  (Python-internal, rewritten as Rust tests when its owner moves). Signals
  record the evidence; ``fixture:flask_client``/``call:test_client`` mean the
  test still drives the app in-process today.
  JS/TS tests exercise browser code in Node: they are ``white-box`` of the
  frontend, which the plan keeps unchanged, so they carry no surfaces.

Parity cases
  Each table row must have exactly one authored mapping (matched by Area and
  Behaviour text); a new, edited or removed row fails generation. Referenced
  paths must be classified test files (a trailing ``/`` names a directory
  holding some), referenced functions must exist (``name`` or
  ``Class::name``; JS: an exact test title), and a row without tests must
  state its ``gap``.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARITY_CASES = 'scripts/test_classification_parity_cases.json'
EVIDENCE_DOC = 'docs/RUST_REWRITE_EVIDENCE.md'
PARITY_SECTION = '### Behaviours that are easy to lose'
# docs/RUST_REWRITE_EVIDENCE.md "Size by top-level owner": Python + JS tests at
# its measured commit.
DOCUMENTED_TEST_COUNT = 42864
DOCUMENTED_COMMIT = '235644fae3'

SURFACES = ('http', 'sse', 'websocket', 'storage-wire', 'process')
JS_SUFFIXES = ('.js', '.mjs', '.cjs', '.ts', '.tsx')
JS_TEST = re.compile(r'^\s*(?:it|test)(?:\.each\([^)]*\))?\s*\(', re.M)
JS_TITLE = re.compile(r'^\s*(?:it|test)(?:\.each\([^)]*\))?\s*\(\s*([\'"`])(.*?)\1', re.M)
PY_TEST_REGEX = re.compile(r'^\s*(?:async\s+)?def\s+test_\w*\s*\(', re.M)
HTTP_BASE_FIXTURES = frozenset({'flask_client', 'live_server'})
STORAGE_WIRE_MODULES = ('lib.storage.client', 'lib.storage.protocol', 'lib.storage.v2_client',
                        'lib.storage.connection_file', 'tofu_db.wire')
PROCESS_MODULES = ('tests.support.scratch_server', 'tests.chaos.instance')
WEBSOCKET_MODULES = ('websockets', 'wsproto')
SERVER_ENTRY_LITERALS = ('server.py', 'serverctl', 'execution_host', 'supervisor.sh')
LAUNCH_CALLS = frozenset({'Popen', 'run', 'check_output', 'check_call', 'call',
                          'create_subprocess_exec', 'create_subprocess_shell'})


class GenerationError(Exception):
    pass


def _git_ls_files(root: Path, *paths: str) -> list[str]:
    """Git's view of the work tree: tracked plus untracked, unignored files that exist."""
    out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
                          '--', *paths], cwd=root, check=True, capture_output=True).stdout
    return sorted({p for p in out.decode('utf-8').split('\0') if p and (root / p).is_file()})


def is_python_test(path: str) -> bool:
    name = path.rsplit('/', 1)[-1]
    return name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py'))


def is_js_test(path: str) -> bool:
    name = path.rsplit('/', 1)[-1]
    return '.test.' in name and name.endswith(JS_SUFFIXES)


def _imports(modules: set[str], prefixes) -> list[str]:
    return sorted({p for p in prefixes for m in modules if m == p or m.startswith(p + '.')})


def _string_literals(tree: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _parameters(function) -> set[str]:
    args = function.args
    return {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _attribute_calls(tree: ast.AST) -> set[str]:
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _is_fixture(function) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, 'id', '')
        if name == 'fixture':
            return True
    return False


def conftest_http_fixtures(root: Path, conftests: list[str]) -> dict[str, set[str]]:
    """Directory -> HTTP fixture names that conftest defines (rule in the header)."""
    by_directory: dict[str, set[str]] = {}
    for path in sorted(conftests, key=lambda p: p.count('/')):
        directory = path.rsplit('/', 1)[0]
        inherited = set(HTTP_BASE_FIXTURES)
        for parent, names in by_directory.items():
            if directory == parent or directory.startswith(parent + '/'):
                inherited |= names
        tree = ast.parse((root / path).read_bytes(), filename=path)
        fixtures = [f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and _is_fixture(f)]
        found: set[str] = set()
        changed = True
        while changed:
            changed = False
            for fixture in fixtures:
                if fixture.name in found:
                    continue
                known = inherited | found
                if ('test_client' in _attribute_calls(fixture)
                        or _parameters(fixture) & known
                        or set(_string_literals(fixture)) & known):
                    found.add(fixture.name)
                    changed = True
        by_directory[directory] = found
    return by_directory


def _http_fixtures_for(path: str, by_directory: dict[str, set[str]]) -> set[str]:
    names = set(HTTP_BASE_FIXTURES)
    for directory, fixtures in by_directory.items():
        if path.startswith(directory + '/'):
            names |= fixtures
    return names


# Expression-level facts are gathered only when the source text holds one of
# these; statements (functions, imports) are always read. Same result, less walking.
_EXPRESSION_TOKENS = ('test_client', 'websocket', 'text/event-stream', '/api/push',
                      'TOFU_TEST_TOFUDB_BIN', *SERVER_ENTRY_LITERALS)
_STATEMENT_BODIES = ('body', 'orelse', 'finalbody', 'handlers', 'cases')


def _statements(nodes):
    for node in nodes:
        if isinstance(node, (ast.stmt, ast.excepthandler, ast.match_case)):
            yield node
            for field in _STATEMENT_BODIES:
                yield from _statements(getattr(node, field, ()) or ())


class _ModuleFacts:
    """Everything the rules read from one module."""

    def __init__(self, tree: ast.Module, text: str):
        self.modules: set[str] = set()
        self.literals: list[str] = []
        self.attribute_calls: set[str] = set()
        self.functions: list = []
        self.launches: list[ast.Call] = []
        self.assigned: dict[str, list[ast.AST]] = {}
        for node in _statements(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(node)
            elif isinstance(node, ast.Import):
                self.modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                self.modules.add(node.module)
                self.modules.update(f'{node.module}.{alias.name}' for alias in node.names)
        if not any(token in text for token in _EXPRESSION_TOKENS):
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    self.literals.append(node.value)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    self.attribute_calls.add(node.func.attr)
                    if node.func.attr in LAUNCH_CALLS:
                        self.launches.append(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assigned.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(node.target, ast.Name) and node.value is not None:
                    self.assigned.setdefault(node.target.id, []).append(node.value)

    def server_launches(self) -> list[str]:
        """``launch:<call>:<entry>`` for subprocess launches whose argv names a server entry.

        The entry literal must sit in the call's arguments, or in a value assigned
        anywhere in the module to a name passed as one of them.
        """
        found = set()
        for call in self.launches:
            roots = [*call.args, *(keyword.value for keyword in call.keywords)]
            roots += [value for argument in list(roots) for name in ast.walk(argument)
                      if isinstance(name, ast.Name) for value in self.assigned.get(name.id, ())]
            literals = [n.value for root in roots for n in ast.walk(root)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            found.update(f'launch:{call.func.attr}:{entry}' for entry in SERVER_ENTRY_LITERALS
                         if any(entry in literal for literal in literals))
        return sorted(found)


def classify_python(path: str, source: bytes, http_fixtures: set[str]) -> dict:
    text = source.decode('utf-8', 'replace')
    facts = _ModuleFacts(ast.parse(source, filename=path), text)
    test_count = sum(1 for f in facts.functions if f.name.startswith('test_'))
    parameters = set().union(*(_parameters(f) for f in facts.functions))
    literals = facts.literals
    signals: dict[str, list[str]] = {surface: [] for surface in SURFACES}

    signals['http'] += [f'fixture:{name}' for name in sorted(parameters & http_fixtures)]
    if 'test_client' in facts.attribute_calls:
        signals['http'].append('call:test_client')

    signals['process'] += [f'import:{m}' for m in _imports(facts.modules, PROCESS_MODULES)]
    if _imports(facts.modules, ('subprocess', 'asyncio')):
        signals['process'] += facts.server_launches()

    if signals['http'] and any('text/event-stream' in literal for literal in literals):
        signals['sse'].append('literal:text/event-stream')

    if 'websocket' in facts.attribute_calls:
        signals['websocket'].append('call:websocket')
    signals['websocket'] += [f'import:{m}' for m in _imports(facts.modules, WEBSOCKET_MODULES)]
    if (signals['http'] or signals['process']) and any('/api/push' in literal for literal in literals):
        signals['websocket'].append('literal:/api/push')

    signals['storage-wire'] += [f'import:{m}' for m in _imports(facts.modules, STORAGE_WIRE_MODULES)]
    if any('TOFU_TEST_TOFUDB_BIN' in literal for literal in literals):
        signals['storage-wire'].append('literal:TOFU_TEST_TOFUDB_BIN')

    surfaces = [surface for surface in SURFACES if signals[surface]]
    return {
        'testCount': test_count,
        'regexTestCount': len(PY_TEST_REGEX.findall(text)),
        'kind': 'black-box' if surfaces else 'white-box',
        'surfaces': surfaces,
        'signals': [f'{surface}:{signal}' for surface in surfaces for signal in signals[surface]],
    }


def _owner(path: str, lib_directories: set[str]) -> str:
    parts = path.split('/')
    if len(parts) < 3:
        return 'tests'
    return f'lib/{parts[1]}' if parts[1] in lib_directories else f'tests/{parts[1]}'


def parse_parity_rows(text: str) -> list[dict]:
    if PARITY_SECTION not in text:
        raise GenerationError(f'{EVIDENCE_DOC}: section {PARITY_SECTION!r} not found')
    section = text.split(PARITY_SECTION, 1)[1]
    rows, in_table = [], False
    for line in section.splitlines():
        if line.startswith('|'):
            in_table = True
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if cells[0] in ('Area', '---') or set(cells[0]) <= {'-'}:
                continue
            if len(cells) != 3:
                raise GenerationError(f'{EVIDENCE_DOC}: parity row has {len(cells)} cells: {line}')
            rows.append({'area': cells[0], 'behaviour': cells[1], 'source': cells[2]})
        elif in_table:
            break
    if not rows:
        raise GenerationError(f'{EVIDENCE_DOC}: parity table has no rows')
    return rows


def _defined_python_tests(source: bytes) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.update(f'{node.name}::{m.name}' for m in node.body
                         if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return names


def build_parity_cases(root: Path, rows: list[dict], authored: dict, classified: set[str]) -> list[dict]:
    mapping: dict[tuple[str, str], dict] = {}
    for case in authored['cases']:
        key = (case['area'], case['behaviour'])
        if key in mapping:
            raise GenerationError(f'{PARITY_CASES}: duplicate mapping for {key}')
        mapping[key] = case
    row_keys = {(row['area'], row['behaviour']) for row in rows}
    problems = [f'doc row without mapping: {key}' for key in
                ((row['area'], row['behaviour']) for row in rows) if key not in mapping]
    problems += [f'mapping without doc row: {key}' for key in mapping if key not in row_keys]
    cases = []
    for row in rows:
        case = mapping.get((row['area'], row['behaviour']))
        if case is None:
            continue
        tests = []
        for reference in case['tests']:
            path = reference['path']
            functions = list(reference.get('functions', ()))
            if path.endswith('/'):
                if not any(p.startswith(path) for p in classified):
                    problems.append(f'{row["behaviour"]!r}: no classified test under {path}')
                if functions:
                    problems.append(f'{row["behaviour"]!r}: functions on directory {path}')
            elif path not in classified:
                problems.append(f'{row["behaviour"]!r}: {path} is not a classified test file')
            elif functions:
                source = (root / path).read_bytes()
                defined = (_defined_python_tests(source) if path.endswith('.py') else
                           {m.group(2) for m in JS_TITLE.finditer(source.decode('utf-8', 'replace'))})
                problems += [f'{row["behaviour"]!r}: {path} has no test {name!r}'
                             for name in functions if name not in defined]
            entry = {'path': path}
            if functions:
                entry['functions'] = functions
            tests.append(entry)
        gap = case.get('gap')
        if not tests and not gap:
            problems.append(f'{row["behaviour"]!r}: no tests and no gap')
        out = {'area': row['area'], 'behaviour': row['behaviour'], 'source': row['source'],
               'tests': tests}
        if gap:
            out['gap'] = gap
        cases.append(out)
    if problems:
        raise GenerationError('parity mapping invalid:\n  ' + '\n  '.join(problems))
    return cases


def build_manifest(root: Path = ROOT) -> dict:
    tracked = _git_ls_files(root, 'tests')
    lib_directories = {p.split('/')[1] for p in _git_ls_files(root, 'lib') if p.count('/') >= 2}
    conftests = [p for p in tracked if p.rsplit('/', 1)[-1] == 'conftest.py']
    by_directory = conftest_http_fixtures(root, conftests)
    files = []
    for path in tracked:
        if is_python_test(path):
            entry = classify_python(path, (root / path).read_bytes(),
                                    _http_fixtures_for(path, by_directory))
            language = 'python'
        elif is_js_test(path):
            count = len(JS_TEST.findall((root / path).read_text('utf-8', 'replace')))
            entry = {'testCount': count, 'regexTestCount': count, 'kind': 'white-box',
                     'surfaces': [], 'signals': []}
            language = 'js'
        else:
            continue
        files.append({'path': path, 'language': language, 'owner': _owner(path, lib_directories),
                      **entry})

    totals: dict = {'files': len(files), 'tests': sum(f['testCount'] for f in files), 'byKind': {},
                    'bySurface': {}, 'byLanguage': {}}
    for kind in ('black-box', 'white-box'):
        selected = [f for f in files if f['kind'] == kind]
        totals['byKind'][kind] = {'files': len(selected), 'tests': sum(f['testCount'] for f in selected)}
    for surface in SURFACES:
        selected = [f for f in files if surface in f['surfaces']]
        totals['bySurface'][surface] = {'files': len(selected),
                                        'tests': sum(f['testCount'] for f in selected)}
    for language in ('python', 'js'):
        selected = [f for f in files if f['language'] == language]
        totals['byLanguage'][language] = {'files': len(selected),
                                          'tests': sum(f['testCount'] for f in selected)}
    regex_total = sum(f['regexTestCount'] for f in files)
    totals['inventoryRegexTests'] = regex_total
    totals['documented'] = {
        'tests': DOCUMENTED_TEST_COUNT, 'commit': DOCUMENTED_COMMIT,
        'note': ('The evidence document counts the regex over every Python file under tests/ '
                 '(support modules and conftest included) at its measured commit. '
                 'inventoryRegexTests applies that regex to the classified files only; testCount '
                 'uses ast, which skips "def test_" text inside string literals. The difference '
                 'to the documented figure is the commits since, plus those two scope rules.'),
    }

    owners = Counter(f['owner'] for f in files)
    rows = parse_parity_rows((root / EVIDENCE_DOC).read_text('utf-8'))
    authored = json.loads((root / PARITY_CASES).read_text('utf-8'))
    parity = build_parity_cases(root, rows, authored, {f['path'] for f in files})
    return {
        '$comment': (f'AUTO-GENERATED by scripts/gen_test_classification.py from git-tracked tests/, '
                     f'{EVIDENCE_DOC} and {PARITY_CASES}. Rules: the generator header. DO NOT EDIT.'),
        'contract': 'tofu.test-classification/v1',
        'kinds': {
            'black-box': 'exercises HTTP, SSE, WebSocket, storage wire or process behaviour; '
                         'later runs against a server URL or process',
            'white-box': 'Python-internal (or frontend Node) test; rewritten as Rust tests from the '
                         'same cases when its owner moves',
        },
        'totals': totals,
        'owners': dict(sorted(owners.items())),
        'parityCases': parity,
        'parityGaps': [case['behaviour'] for case in parity if case.get('gap')],
        'files': files,
    }


def render(manifest: dict) -> str:
    """Pretty top level, one line per file entry so diffs stay per file."""
    head = {k: v for k, v in manifest.items() if k != 'files'}
    text = json.dumps(head, indent=2, ensure_ascii=False)
    lines = ',\n'.join('    ' + json.dumps(f, ensure_ascii=False, separators=(', ', ': '))
                       for f in manifest['files'])
    return text[:-2] + ',\n  "files": [\n' + lines + '\n  ]\n}\n'


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                      help='build the manifest and report its totals; write nothing')
    mode.add_argument('--output', type=Path, help='write the manifest to this path')
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(ROOT)
    except GenerationError as error:
        print(f'gen_test_classification: {error}', file=sys.stderr)
        return 1
    if args.check:
        totals = manifest['totals']
        print(f"test classification builds: {totals['files']} files, {totals['tests']} tests, "
              f"{len(manifest['parityCases'])} parity rows mapped")
    elif args.output:
        write_atomic(args.output, render(manifest))
    else:
        sys.stdout.write(render(manifest))
    return 0


if __name__ == '__main__':
    sys.exit(main())
