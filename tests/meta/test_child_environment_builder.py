"""Children Tofu starts take their environment from one builder.

Authority: docs/contracts/TOOL_EXECUTION_POLICY.md, "Children Tofu starts never
inherit the running server's storage handles". ``inherited_child_environment``
in ``lib/tools/process_environment.py`` is the only place application code takes
a child environment from this process's environment: the running server's
storage handles and ambient credentials stay behind. Spawn sites once bypassed
it in two ways. Some copied ``os.environ`` themselves (MCP stdio servers,
local model servers, renders, paper builds, Tofu's own
Git). Others launched Tofu-started work without ``env=`` and so inherited
everything (scheduled jobs, readiness predicates, Project
Brain checkers, media helpers). This guard finds both in ``lib/`` and
``routes/``; ``DECLARED_SITES`` names the few that must read ``os.environ``
themselves, each with its reason. The server, storage and lifecycle processes
Tofu runs itself (``server_manager.py``, ``supervisor.py``, ``serverctl*``,
``bootstrap_pkg/``) are outside that tree and inherit the whole environment by
design.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ('lib/tools/process_environment.py', 'inherited_child_environment')

# Application code that reads os.environ for a child itself on purpose, keyed by
# (path, enclosing function), with the reason.
DECLARED_SITES: dict[tuple[str, str], str] = {
    ('lib/storage/supervisor.py', 'StorageSupervisor.start'):
        "Starts the storage authority child, which reads the storage host's service "
        'identity (lib/storage_authority_sql/native_launch.py); it drops the inherited '
        'connection file and sets its own token and parent pid.',
    ('lib/desktop_agent/_exec.py', '_StreamedProcess.start'):
        "Runs in the Desktop Agent on the user's own machine, never in a server process; "
        "the user's commands get that machine's environment, as in its terminal.",
    ('lib/project_mod/git_index_snapshot_exec.py', 'main'):
        'Pinned standard-library helper run from a frozen copy (PinnedHelperSource) as the '
        'Git query process itself; its environment is the one its caller passed, which '
        'this guard keeps built from inherited_child_environment.',
    ('lib/tasks_pkg/tool_dispatch/_pipeline.py', '_git_guard_presence_roots'):
        "Reads Git routing variables to resolve a command's targets "
        '(lib/project_mod/git_command_scope.py); starts no process.',
    ('lib/tasks_pkg/workspace_delivery.py', 'command_workspace_refusal.judge'):
        "Reads Git routing variables to resolve where a command's Git works "
        '(lib/project_mod/git_command_scope.py) for the isolation guard; starts no process.',
    ('lib/self_update/_git.py', '_run_git'):
        "Tofu updating its own served checkout (status, fetch, ls-remote) authenticates with "
        "the owner's own Git setup, as the lifecycle tools outside lib/ do; it inherited the "
        'whole environment before its index reads went through prepare_git_index_read, which '
        'now receives that mapping explicitly.',
}
# Process launchers that take the environment as a positional argument.
_EXEC_WITH_ENVIRONMENT = frozenset({
    'execve', 'execvpe', 'execle', 'execlpe', 'spawnve', 'spawnvpe', 'spawnle', 'spawnlpe',
    'posix_spawn', 'posix_spawnp',
})

# A spawn of Tofu-started work is a call of one of these that consumes
# ``work_process_argv(...)`` (docs/RESOURCE_CONTROL.md, "Tofu-started work is
# the kernel's first OOM victim").
_SPAWNERS = frozenset({
    'Popen', 'run', 'call', 'check_call', 'check_output', 'create_subprocess_exec',
    'create_subprocess_shell', 'open_process', 'StdioServerParameters', 'popen', 'runner',
})
_REPORTED_PATHS = frozenset({
    'lib/mcp/client/_bridge.py', 'lib/scheduler/process_runner.py',
    'lib/conversations/project_brain.py',
})


def _sources() -> list[str]:
    listed = subprocess.run(['git', 'ls-files', '-z', '--', 'lib', 'routes'], cwd=ROOT,
                            capture_output=True, check=True).stdout.decode()
    return sorted(path for path in listed.split('\0') if path.endswith('.py'))


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')


def _own_nodes(function):
    """The function's own nodes, without the bodies of nested scopes."""
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


def _mentions(node, names: set[str]) -> bool:
    return any(isinstance(child, ast.Name) and child.id in names for child in ast.walk(node))


def _is_work_argv(node) -> bool:
    return isinstance(node, ast.Call) and _callee(node) == 'work_process_argv'


class _Module(ast.NodeVisitor):
    """Whole-environment copies and env-less work spawns of one module."""

    def __init__(self, tree: ast.Module):
        self.os_names, self.environ_names = {'os'}, set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.os_names.update(alias.asname or 'os' for alias in node.names if alias.name == 'os')
            elif isinstance(node, ast.ImportFrom) and node.module == 'os':
                self.environ_names.update(alias.asname or 'environ' for alias in node.names
                                          if alias.name == 'environ')
        self.scope: list[str] = []
        self.copies: list[tuple[str, int]] = []
        self.work_spawns: list[tuple[str, int, bool]] = []
        self.visit(tree)

    def _qualname(self) -> str:
        return '.'.join(self.scope) or '<module>'

    def _is_environ(self, node) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == 'environ':
            return isinstance(node.value, ast.Name) and node.value.id in self.os_names
        return isinstance(node, ast.Name) and node.id in self.environ_names

    def _may_be_environ(self, node) -> bool:
        if isinstance(node, ast.IfExp):
            return self._may_be_environ(node.body) or self._may_be_environ(node.orelse)
        if isinstance(node, ast.BoolOp):
            return any(self._may_be_environ(value) for value in node.values)
        return self._is_environ(node)

    def _iterates_environ(self, node) -> bool:
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('items', 'keys', 'values')):
            return self._is_environ(node.func.value)
        return self._is_environ(node)

    def _copied(self, node) -> None:
        self.copies.append((self._qualname(), node.lineno))

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self._find_work_spawns(node)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'copy' and self._is_environ(func.value):
            self._copied(node)
        elif _callee(node) == 'dict' and (
                (node.args and self._may_be_environ(node.args[0]))
                or any(keyword.arg is None and self._may_be_environ(keyword.value)
                       for keyword in node.keywords)):
            self._copied(node)
        elif _callee(node) in _EXEC_WITH_ENVIRONMENT and any(
                self._may_be_environ(argument) for argument in node.args):
            self._copied(node)
        elif any(keyword.arg in ('env', 'environment') and self._may_be_environ(keyword.value)
                 for keyword in node.keywords):
            self._copied(node)
        self.generic_visit(node)

    def visit_Dict(self, node):
        if any(key is None and self._may_be_environ(value)
               for key, value in zip(node.keys, node.values)):
            self._copied(node)
        self.generic_visit(node)

    def _visit_comprehension(self, node):
        for generator in node.generators:
            if self._iterates_environ(generator.iter) and not _allowlisted(generator):
                self._copied(node)
        self.generic_visit(node)

    visit_DictComp = visit_ListComp = visit_SetComp = visit_GeneratorExp = _visit_comprehension

    def _find_work_spawns(self, function) -> None:
        nodes = list(_own_nodes(function))
        launches = {target.id for node in nodes if isinstance(node, ast.Assign) and _is_work_argv(node.value)
                    for target in node.targets if isinstance(target, ast.Name)}
        for node in nodes:
            if not isinstance(node, ast.Call) or _callee(node) not in _SPAWNERS:
                continue
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            if not any(_is_work_argv(value) or _mentions(value, launches) for value in values):
                continue
            env = next((keyword.value for keyword in node.keywords if keyword.arg == 'env'), None)
            explicit = env is not None and not (isinstance(env, ast.Constant) and env.value is None)
            self.work_spawns.append((self._qualname(), node.lineno, explicit))


def _allowlisted(generator: ast.comprehension) -> bool:
    """Every condition selects named entries (``name in NAMES``, ``name.startswith(...)``)."""
    target = generator.target
    if isinstance(target, ast.Tuple) and target.elts:
        target = target.elts[0]
    if not isinstance(target, ast.Name) or not generator.ifs:
        return False

    def selects(test) -> bool:
        if isinstance(test, ast.BoolOp):
            return all(selects(value) for value in test.values)
        if isinstance(test, ast.Compare):
            return (len(test.ops) == 1 and isinstance(test.ops[0], ast.In)
                    and _mentions(test.left, {target.id}))
        return (isinstance(test, ast.Call) and _callee(test) == 'startswith'
                and isinstance(test.func, ast.Attribute) and _mentions(test.func.value, {target.id}))

    return all(selects(test) for test in generator.ifs)


@pytest.fixture(scope='module')
def scan():
    copies: dict[tuple[str, str], list[int]] = {}
    spawns: list[tuple[str, str, int, bool]] = []
    for path in _sources():
        text = (ROOT / path).read_text(encoding='utf-8')
        # A copy names os.environ (or an imported environ); a work spawn names work_process_argv.
        if 'environ' not in text and 'work_process_argv' not in text:
            continue
        module = _Module(ast.parse(text, filename=path))
        for qualname, line in module.copies:
            copies.setdefault((path, qualname), []).append(line)
        spawns.extend((path, qualname, line, explicit) for qualname, line, explicit in module.work_spawns)
    return copies, spawns


def test_only_the_builder_and_declared_sites_read_the_environment_for_children(scan):
    copies, _ = scan
    undeclared = sorted(f'{path}:{lines[0]} ({qualname})' for (path, qualname), lines in copies.items()
                        if (path, qualname) != BUILDER and (path, qualname) not in DECLARED_SITES)
    assert not undeclared, (
        'These spawn sites build a child environment from os.environ themselves, so the running '
        "server's storage handles and ambient credentials reach their children. Use "
        'lib.tools.process_environment.inherited_child_environment(), or declare the site in '
        'DECLARED_SITES with the reason it must read os.environ itself:\n'
        + '\n'.join(undeclared))
    stale = sorted(f'{path} ({qualname})' for path, qualname in DECLARED_SITES
                   if (path, qualname) not in copies)
    assert not stale, 'Declared sites that no longer copy the environment; remove them:\n' + '\n'.join(stale)
    assert BUILDER in copies, 'The scan no longer sees the builder itself; the scanner is broken.'


def test_tofu_started_work_passes_an_explicit_environment(scan):
    _, spawns = scan
    seen = {path for path, *_ in spawns}
    assert _REPORTED_PATHS <= seen, f'The scan no longer sees work spawns in {sorted(_REPORTED_PATHS - seen)}.'
    inheriting = sorted(f'{path}:{line} ({qualname})' for path, qualname, line, explicit in spawns
                        if not explicit)
    assert not inheriting, (
        "These spawns of Tofu-started work pass no env= and inherit the server's whole "
        'environment, live storage handles included. Pass env=inherited_child_environment() '
        '(lib/tools/process_environment.py) or a mapping built from it:\n' + '\n'.join(inheriting))
