"""Behavioral specifications for affected-test discovery, selection and execution."""

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
_SCRIPT = os.path.join(ROOT, 'scripts', 'test_select.py')


def _load():
    spec = importlib.util.spec_from_file_location('test_select', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load()


def _p(head, rest):
    """Synthetic fixture path, built in TWO segments on purpose.

    The paths below point NOWHERE — they are inputs/expected-outputs of the
    pure refs-extraction/selection functions, not references to repo files.
    Written as 'dir' + 'rest' so the suite-health census (_PATH_RE dead-anchor
    scan, scripts/audit_tests.py category F) does not mistake them for real
    stale anchors — a plain 'lib/foo/bar.py' literal would be flagged as a
    missing-path reference, which is exactly what these strings are NOT.
    """
    return head + '/' + rest


class TestRefsOfTestFile:
    @pytest.mark.parametrize('body', [
        'def owner():\n    {statement}',
        'async def owner():\n    {statement}',
        'class Owner:\n    {statement}',
        'if ready:\n    pass\nelse:\n    {statement}',
        'for value in values:\n    pass\nelse:\n    {statement}',
        'while ready:\n    {statement}',
        'with context():\n    {statement}',
        'async def owner():\n    async for value in values:\n        {statement}',
        'async def owner():\n    async with context():\n        {statement}',
        'try:\n    {statement}\nexcept Exception:\n    pass',
        'try:\n    pass\nexcept Exception:\n    {statement}',
        'try:\n    pass\nexcept* Exception:\n    {statement}',
        'try:\n    pass\nexcept Exception:\n    pass\nelse:\n    {statement}',
        'try:\n    pass\nfinally:\n    {statement}',
        'match value:\n    case 1 if ready:\n        {statement}',
    ])
    @pytest.mark.parametrize('owner', ['test', 'source'])
    def test_imports_in_every_statement_container(self, body, owner):
        source = body.format(statement='from lib.branch import value')
        refs = (ts.refs_of_test_file(source) if owner == 'test'
                else ts.source_references(source, _p('tests', 'owner.py')))
        expected = {_p('lib', path) for path in (
            'branch.py', 'branch/__init__.py', 'branch/value.py', 'branch/value/__init__.py')}
        assert refs == expected | ({'lib/__init__.py'} if owner == 'source' else set())

    def test_static_import_pass_does_not_traverse_expression_payloads(self, monkeypatch):
        expressions = set()
        original = ts.ast.iter_child_nodes

        def children(node):
            if isinstance(node, ts.ast.expr):
                expressions.add(type(node).__name__)
            return original(node)

        source = (
            'from lib.branch import value\n'
            'payload = [item + 1 for item in range(100)]\n'
            'path = "frontend/src/api/transport.ts"\n')
        tree = ts.ast.parse(source)
        with monkeypatch.context() as patch:
            patch.setattr(ts.ast, 'iter_child_nodes', children)
            imports = list(ts._import_statements(tree))
        assert [(node.module, [alias.name for alias in node.names])
                for node in imports] == [('lib.branch', ['value'])]
        assert expressions == set()

        refs = ts.source_references(source, _p('tests', 'owner.py'), tree=tree)
        assert _p('lib', 'branch.py') in refs
        assert 'frontend/src/api/transport.ts' not in refs

    @pytest.mark.parametrize('source', [
        'commands = {"build": ["make test"]}',
        'run("gmake", "all")',
        'path = "Ma" "kefile"',
        r'path = "M\x61kefile"',
        'command = f"make {target}"',
    ])
    def test_makefile_literals_in_expressions_remain_test_dependencies(self, source):
        assert 'Makefile' in ts.refs_of_test_file(source)
        assert ts.source_references(source, _p('tests', 'owner.py')) == set()

    def test_declared_plugin_is_a_direct_input(self):
        assert 'tests/support/postgres_cluster.py' in ts.refs_of_test_file(
            'pytest_plugins = "tests.support.postgres_cluster"\n')

    def test_unknown_plugin_is_not_silently_omitted(self):
        consumer = _p('tests', 'test_plugin_user.py')
        guards = {f'tests/contracts/{name}': set() for name in ts.GUARD_CORE_BASENAMES}
        index = {**guards, ts.AGENT_WHEEL_BOUNDARY_GUARD: set(),
                 consumer: ts.refs_of_test_file('pytest_plugins = choose_plugins()\n')}
        assert consumer in ts.select_tests(index, [_p('lib', 'changed.py')])[0]

    def test_import_maps_to_repo_path(self):
        src = "import lib.foo.bar\nfrom routes.api_v1 import chat\n"
        refs = ts.refs_of_test_file(src)
        assert _p('lib', 'foo/bar.py') in refs
        assert (_p('routes', 'api_v1/__init__.py') in refs
                or _p('routes', 'api_v1.py') in refs)

    def test_from_import_of_module_maps_to_module_path(self):
        src = "from lib.tasks_pkg.manager import get_task\n"
        refs = ts.refs_of_test_file(src)
        assert (_p('lib', 'tasks_pkg/manager.py') in refs
                or _p('lib', 'tasks_pkg/manager/__init__.py') in refs)

    def test_tests_helper_import_maps(self):
        src = "from tests._jsdom import run_harness\n"
        refs = ts.refs_of_test_file(src)
        assert 'tests/_jsdom.py' in refs

    def test_literal_path_refs_captured(self):
        src = "JS = os.path.join(ROOT, 'frontend', 'src', 'main.ts')\n" \
              "X = 'frontend/src/api/transport.ts'\n" \
              "DOC = 'docs/contracts/API_CONTRACT.md'\n"
        refs = ts.refs_of_test_file(src)
        assert _p('frontend', 'src/api/transport.ts') in refs
        assert _p('docs', 'contracts/API_CONTRACT.md') in refs

    def test_external_packages_not_mapped(self):
        src = "import pytest\nimport tofu_search\nimport requests\n"
        refs = ts.refs_of_test_file(src)
        # Outside the module roots an import stays an unresolved package-relative
        # candidate: the repository suite drops it, and a package suite resolves
        # it only inside its own directory, where its session imports it first.
        assert all(r.startswith(ts.PACKAGE_RELATIVE) for r in refs)
        assert ts.resolve_references(_p('tests', 'test_x.py'), refs) == []
        package_test = _p('packages', 'demo/tests/test_x.py')
        imports = set(ts.resolve_references(package_test, refs)) - set(
            ts.resolve_references(package_test, ()))
        assert imports and all(r.startswith(_p('packages', 'demo/')) for r in imports)


class TestSelectTests:
    A = _p('tests', 'test_a.py')
    B = _p('tests', 'test_b.py')
    C = _p('tests', 'test_frontend_c.py')
    D = _p('tests', 'test_d.py')
    LIB_X = _p('lib', 'x.py')
    LIB_W = _p('lib', 'w.py')
    UI_Y = _p('frontend', 'src/features/y.ts')
    CORE_Z = _p('frontend', 'src/runtime/app-runtime.js')
    JSDOM_HELPER = _p('tests', '_jsdom.py')
    API_JS = _p('frontend', 'src/api/transport.ts')
    CONFTEST = _p('tests', 'conftest.py')
    INDEX = {
        A: {LIB_X},
        B: {UI_Y},
        C: {CORE_Z},
        D: {LIB_X, LIB_W},
        **{f'tests/contracts/{name}': set() for name in ts.GUARD_CORE_BASENAMES},
        ts.AGENT_WHEEL_BOUNDARY_GUARD: set(),
    }

    def test_changed_source_selects_importers(self):
        selected, _ = ts.select_tests(self.INDEX, [self.LIB_X])
        assert selected >= {self.A, self.D}
        assert self.B not in selected

    def test_changed_test_file_selects_itself(self):
        selected, _ = ts.select_tests(self.INDEX, [self.B])
        assert self.B in selected

    def test_blast_radius_jsdom_helper_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, [self.JSDOM_HELPER])
        assert self.C in selected

    def test_blast_radius_api_transport_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, [self.API_JS])
        assert self.C in selected

    def test_guard_core_always_runs(self):
        selected, _ = ts.select_tests(self.INDEX, [self.LIB_X])
        assert any('contract' in os.path.basename(f) for f in selected), (
            'guard core must always be in the selection')

    def test_unknown_change_falls_back_to_guard_core(self):
        selected, reasons = ts.select_tests(self.INDEX, ['README.md'])
        direct = {f for f in selected if reasons[f] != 'guard-core'}
        assert not direct, 'unrelated change must not pull behaviour suites'
        assert selected, 'guard core must still run as the smoke floor'

    def test_conftest_change_selects_everything(self):
        selected, _ = ts.select_tests(self.INDEX, [self.CONFTEST])
        assert selected >= set(self.INDEX), (
            'conftest touches every session — the blast radius is the whole suite')

    def test_many_changes_read_each_reference_collection_once(self):
        reads = []

        class References(list):
            def __iter__(self):
                reads.append(self)
                return super().__iter__()

        index = dict(self.INDEX)
        index[self.A] = {'refs': References([self.LIB_X, self.LIB_W])}
        selected, _ = ts.select_tests(index, [self.LIB_X, self.LIB_W, 'README.md'])
        assert selected == {self.A, self.D, ts.AGENT_WHEEL_BOUNDARY_GUARD} | {
            path for path in index if path.startswith('tests/contracts/')}
        assert len(reads) == 1

    def test_empty_change_set_reads_no_dependency_collections(self):
        class NoReferences:
            def __iter__(self):
                raise AssertionError('no changes need no dependency lookup')

        index = dict(self.INDEX)
        index[self.A] = {'refs': NoReferences()}
        selected, reasons = ts.select_tests(index, [])
        assert selected == {path for path in index if path.startswith('tests/contracts/')}
        assert set(reasons.values()) == {'guard-core'}

    @pytest.mark.parametrize('changed, target, reason', [
        (A, A, 'self'),
        (CONFTEST, A, 'fixture:tests/conftest.py'),
        (JSDOM_HELPER, C, 'blast:tests/_jsdom.py'),
        (LIB_X, A, 'direct'),
    ])
    def test_structural_reasons_win_over_same_path_direct_references(self, changed, target, reason):
        index = dict(self.INDEX)
        index[target] = {'refs': [changed, changed]}
        selected, reasons = ts.select_tests(index, [changed])
        assert target in selected
        assert reasons[target] == reason


class TestPytestCommand:
    def test_parallel_command_only_selects_worker_count(self):
        cmd = ts.build_pytest_command([_p('tests', 'test_a.py')], '2')

        workers = cmd.index('-n')
        assert cmd[workers + 1] == '2'
        assert 'xdist.plugin' not in cmd
        assert '--dist' not in cmd

    def test_serial_command_does_not_pass_worker_flags(self):
        cmd = ts.build_pytest_command(None, '0')

        assert '-n' not in cmd
        assert cmd[cmd.index('-m', 3) + 1] == '(unit or api) and not (visual or slow or live_llm)'

    def test_implicit_single_file_selection_stays_serial(self):
        cmd = ts.build_pytest_command([_p('tests', 'test_a.py')], None)

        assert '-n' not in cmd

    def test_implicit_multi_file_selection_uses_shared_auto_policy(self):
        selected = [f'tests/test_{i}.py' for i in range(10)]
        cmd = ts.build_pytest_command(selected, None)

        assert cmd[cmd.index('-n') + 1] == 'auto'


@pytest.fixture
def selection_repo(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'tests').mkdir()
    return tmp_path


def write_test(root, relative, content='assert True\n'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_index_discovers_nested_and_untracked_tests_but_not_ignored(selection_repo):
    import subprocess
    root = selection_repo
    tracked = write_test(root, _p('tests', 'domain/test_tracked.py'), 'import lib.example\n')
    subprocess.run(['git', 'add', str(tracked)], cwd=root, check=True)
    nested = write_test(root, _p('tests', 'new domain/test_new.py'))
    (root / '.gitignore').write_text('tests/ignored/\n')
    write_test(root, _p('tests', 'ignored/test_cache.py'))
    index = ts.build_index(str(root / 'tests'), None)
    assert set(index) == {tracked.relative_to(root).as_posix(), nested.relative_to(root).as_posix()}
    assert _p('lib', 'example.py') in index[tracked.relative_to(root).as_posix()]['refs']


def test_git_failure_cannot_be_an_empty_success(tmp_path):
    import subprocess
    (tmp_path / 'tests').mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        ts.build_index(str(tmp_path / 'tests'), None)


def test_guard_core_resolves_current_paths_and_deleted_tests_are_not_selected():
    guards = {_p('tests', 'contracts/' + name): set() for name in ts.GUARD_CORE_BASENAMES}
    deleted = _p('tests', 'domain/test_removed.py')
    selected, reasons = ts.select_tests(guards, [deleted])
    assert selected == set(guards)
    assert set(reasons.values()) == {'guard-core'}


def test_missing_guard_fails_with_actionable_error():
    with pytest.raises(ValueError, match='guard'):
        ts.select_tests({}, ['README.md'])


def test_empty_selection_cannot_launch_unscoped_pytest():
    with pytest.raises(ValueError, match='empty'):
        ts.build_pytest_command([], None)


def test_nested_conftest_selects_its_subtree():
    guards = {_p('tests', 'contracts/' + name): set() for name in ts.GUARD_CORE_BASENAMES}
    inside, outside = _p('tests', 'domain/test_a.py'), _p('tests', 'neighbor/test_b.py')
    selected, _ = ts.select_tests({**guards, inside: set(), outside: set()},
                                  [_p('tests', 'domain/conftest.py')])
    assert inside in selected
    assert outside not in selected



def test_cache_forgets_removed_files_and_rebuilds_invalid_entries(selection_repo):
    import json
    root = selection_repo
    keep = write_test(root, _p('tests', 'domain/test_keep.py'), 'import lib.before\n')
    removed = write_test(root, _p('tests', 'domain/test_removed.py'))
    cache = root / 'cache.json'
    first = ts.build_index(str(root / 'tests'), str(cache))
    assert len(first) == 2
    removed.unlink()
    keep.write_text('from lib import after\n')
    changed = ts.build_index(str(root / 'tests'), str(cache))
    assert set(changed) == {keep.relative_to(root).as_posix()}
    assert _p('lib', 'after.py') in next(iter(changed.values()))['refs']
    assert ts.build_index(str(root / 'tests'), str(cache)) == changed
    cache.write_text(json.dumps({'version': 4, 'index': []}))
    assert ts.build_index(str(root / 'tests'), str(cache)) == changed
    cache.write_text('{')
    assert ts.build_index(str(root / 'tests'), str(cache)) == changed


def test_old_reference_cache_cannot_hide_plugin_inputs(selection_repo):
    import json
    root = selection_repo
    path = write_test(root, _p('tests', 'test_plugin_user.py'),
                      'pytest_plugins = "tests.support.postgres_cluster"\n')
    stat = path.stat()
    relative = path.relative_to(root).as_posix()
    cache = root / 'cache.json'
    cache.write_text(json.dumps({'version': 2, 'index': {
        relative: {'fingerprint': [stat.st_mtime_ns, stat.st_size], 'refs': []},
    }}))
    assert 'tests/support/postgres_cluster.py' in ts.build_index(
        str(root / 'tests'), str(cache))[relative]['refs']


def test_git_changes_preserve_rename_endpoints_and_unusual_names(selection_repo, monkeypatch):
    import subprocess
    root = selection_repo
    before = write_test(root, _p('tests', 'domain/test_before.py'))
    def git(*args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c',
                               'user.email=test@example.invalid', *args], cwd=root,
                              check=True, text=True, capture_output=True).stdout.strip()
    git('add', '.')
    git('commit', '-qm', 'initial')
    base = git('rev-parse', 'HEAD')
    after = before.with_name('test_after space\nline.py')
    before.rename(after)
    extra = write_test(root, _p('tests', 'new domain/test_extra.py'))
    monkeypatch.setattr(ts, 'ROOT', str(root))
    expected = sorted(p.relative_to(root).as_posix() for p in (before, after, extra))
    assert ts.changed_files(None) == expected
    git('add', '.')
    git('commit', '-qm', 'rename and add')
    assert ts.changed_files(base) == expected
    assert ts.changed_files(None) == []


def test_default_execution_never_enables_opt_in_tiers():
    for files in (None, [_p('tests', 'domain/test_api.py')]):
        command = ts.build_pytest_command(files, '0')
        assert command[command.index('-m', 3) + 1] == '(unit or api) and not (visual or slow or live_llm)'


class TestPackageSessions:
    ROOT_TEST = _p('tests', 'domain/test_a.py')
    PACKAGE_TEST = _p('packages', 'demo/tests/test_b.py')
    NESTED_TEST = _p('bundles', 'site/provider/tests/test_c.py')

    def test_repository_tests_keep_the_repository_command(self):
        assert ts.build_pytest_sessions([self.ROOT_TEST], '2', '/repo') == [
            ('', ts.build_pytest_command([self.ROOT_TEST], '2'))]

    def test_each_package_runs_from_its_own_directory_under_repository_node_ids(self):
        case = self.PACKAGE_TEST + '::test_case[x/y]'
        sessions = dict(ts.build_pytest_sessions(
            [self.NESTED_TEST, case, self.ROOT_TEST, self.PACKAGE_TEST], '4', '/repo'))
        assert set(sessions) == {'', _p('packages', 'demo'), _p('bundles', 'site/provider')}
        package = sessions[_p('packages', 'demo')]
        # No tests/conftest.py: the session loads the hang guard and the priority yield itself.
        for plugin in ('pytest_timeout', 'tests.support.lower_priority_plugin'):
            assert package[package.index(plugin) - 1] == '-p'
        assert _p('tests', 'test_b.py::test_case[x/y]') in package
        assert _p('tests', 'test_b.py') in package
        assert package[package.index('--rootdir') + 1] == '/repo'
        # Untagged package tests run: no repository conftest tags them `unit`.
        assert package[package.index('-m', 3) + 1] == 'not (visual or slow or live_llm)'
        # The package configuration loads no xdist, and its sizing hook is absent.
        assert '-n' not in package
        assert sessions[''] == ts.build_pytest_command([self.ROOT_TEST], '4')
        assert sessions[_p('bundles', 'site/provider')][9] == _p('tests', 'test_c.py')

    @pytest.mark.parametrize('tests', [[], [_p('scripts', 'test_tool.py')]])
    def test_no_session_for_an_empty_selection_or_a_path_outside_a_test_root(self, tests):
        with pytest.raises(ValueError):
            ts.build_pytest_sessions(tests, None, '/repo')

    def test_a_change_inside_a_package_selects_its_whole_suite(self):
        guards = {_p('tests', 'contracts/' + name): set() for name in ts.GUARD_CORE_BASENAMES}
        sibling = _p('packages', 'demo/tests/test_other.py')
        index = {**guards, self.ROOT_TEST: set(), self.PACKAGE_TEST: set(), sibling: set(),
                 _p('bundles', 'site/tests/test_parent.py'): set(), self.NESTED_TEST: set()}
        selected, reasons = ts.select_tests(index, [_p('packages', 'demo/demo/core.py')])
        assert {self.PACKAGE_TEST, sibling} <= selected
        assert reasons[sibling] == 'package:' + _p('packages', 'demo')
        assert self.ROOT_TEST not in selected and self.NESTED_TEST not in selected
        selected, _ = ts.select_tests(index, [_p('bundles', 'site/provider/adapter.py')])
        assert self.NESTED_TEST in selected
        assert _p('bundles', 'site/tests/test_parent.py') not in selected

    def test_package_imports_and_configuration_resolve_at_the_test_path(self):
        refs = ts.refs_of_test_file('from demo.core import value\nimport lib.alpha\n')
        assert ts.PACKAGE_RELATIVE + 'demo/core.py' in refs
        package = ts.resolve_references(self.PACKAGE_TEST, refs)
        assert {_p('packages', 'demo/demo/core.py'), _p('packages', 'demo/demo/core/__init__.py'),
                _p('lib', 'alpha.py'), _p('packages', 'demo/pyproject.toml'),
                _p('packages', 'demo/tests/pytest.ini'), _p('packages', 'setup.cfg')} <= set(package)
        assert 'pyproject.toml' not in package  # the root's is already a global input
        # The repository suite imports only its module roots by path.
        assert ts.resolve_references(self.ROOT_TEST, refs) == sorted(
            ref for ref in refs if not ref.startswith(ts.PACKAGE_RELATIVE))


def test_index_includes_every_test_root_with_its_session_inputs(selection_repo):
    root = selection_repo
    write_test(root, _p('tests', 'test_root.py'))
    write_test(root, _p('packages', 'demo/tests/test_pkg.py'))
    write_test(root, _p('packages', 'demo/demo/test_helpers.py'))
    index = ts.build_index(str(root / 'tests'), None)
    assert set(index) == {_p('tests', 'test_root.py'), _p('packages', 'demo/tests/test_pkg.py')}
    assert _p('packages', 'demo/pyproject.toml') in index[_p('packages', 'demo/tests/test_pkg.py')]['refs']


def test_selector_cli_runs_each_package_suite_from_its_directory(selection_repo):
    import shutil
    import subprocess
    import sys
    root = selection_repo
    scripts = root / 'scripts'
    scripts.mkdir()
    shutil.copyfile(_SCRIPT, scripts / 'test_select.py')
    shutil.copyfile(os.path.join(os.path.dirname(_SCRIPT), 'test_execution.py'), scripts / 'test_execution.py')
    for sibling in ('agent_wheel_manifest.py', 'check_static_gates.py'):
        shutil.copyfile(os.path.join(os.path.dirname(_SCRIPT), sibling), scripts / sibling)
    (root / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytest_timeout"\n'
        'markers = ["unit", "api", "visual", "slow", "live_llm"]\n')
    for name in ts.GUARD_CORE_BASENAMES:
        write_test(root, _p('tests', 'guards/' + name),
                   'import pytest\npytestmark = pytest.mark.unit\ndef test_guard():\n    assert True\n')
    for support in ('tests/__init__.py', 'tests/support/__init__.py',
                    'tests/support/process_priority.py', 'tests/support/lower_priority_plugin.py'):
        (root / support).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, support), root / support)
    package = root / 'packages' / 'demo'
    # The package's own configuration applies: its marker is strict and registered only there.
    write_test(root, _p('packages', 'demo/pyproject.toml'),
               '[tool.pytest.ini_options]\naddopts = "--strict-markers"\nmarkers = ["demo_only"]\n')
    write_test(root, _p('packages', 'demo/demo/__init__.py'), 'VALUE = 3\n')
    write_test(root, _p('packages', 'demo/tests/test_pkg.py'),
               'import os\nimport sys\nimport pytest\nfrom pathlib import Path\nfrom demo import VALUE\n\n'
               '@pytest.mark.demo_only\ndef test_package():\n'
               '    Path("ran-in").write_text(os.getcwd())\n    assert VALUE == 3\n'
               '    assert "tests.support.lower_priority_plugin" in sys.modules\n')
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'initial'], cwd=root, check=True)
    write_test(root, _p('packages', 'demo/demo/__init__.py'), 'VALUE = 3  # edited\n')
    result = subprocess.run([sys.executable, str(scripts / 'test_select.py'), '--run', '--jobs', '0'],
                            cwd=root, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (package / 'ran-in').read_text() == str(package.resolve())



def _frontend_repo(root, files):
    for relative, content in files.items():
        write_test(root, relative, content)
    return ts.build_frontend_spec_index(str(root))


SECTION = _p('frontend/src/runtime/sections', 'synthetic_presence.js')
SPEC_RAW = _p('frontend/src/features', 'entry.test.ts')
SPEC_READ = _p('frontend/src/conversation', 'draft.test.ts')
SPEC_OTHER = _p('frontend/src/core', 'other.test.ts')


class TestFrontendSpecSelection:
    """Vitest specs selected for a change, as the delivery gate runs them."""

    def test_raw_import_and_repo_path_read_both_select_the_section_owner(self, selection_repo):
        index = _frontend_repo(selection_repo, {
            SECTION: 'var presence = 1;\n',
            SPEC_RAW: "import source from '../runtime/sections/synthetic_presence.js?raw';\n",
            SPEC_READ: ('const presence = readFileSync(`${cwd()}/'
                        + SECTION + "`, 'utf8');\n"),
            SPEC_OTHER: "import { it } from 'vitest';\n",
        })
        selected, reasons = ts.select_frontend_specs(index, [SECTION])
        assert selected == {SPEC_RAW, SPEC_READ}
        assert reasons[SPEC_RAW] == 'import' and reasons[SPEC_READ] == 'literal'

    def test_interpolated_directory_read_selects_only_files_beneath_it(self, selection_repo):
        spec = _p('frontend/src/conversation', 'retained.test.ts')
        main_dir = _p('frontend/src/runtime/sections', 'main/')
        index = _frontend_repo(selection_repo, {
            spec: 'const read = (name) => readFileSync(`${cwd()}/' + main_dir + '${name}`);\n',
            main_dir + 'composer.js': 'var composer;\n',
            SECTION: 'var presence;\n',
        })
        assert ts.select_frontend_specs(index, [main_dir + 'composer.js'])[0] == {spec}
        assert ts.select_frontend_specs(index, [SECTION])[0] == set()

    def test_paths_in_comments_and_prose_are_not_inputs(self, selection_repo):
        doc = _p('docs', 'OWNER.md')
        index = _frontend_repo(selection_repo, {
            SPEC_RAW: ('// these tests cover lib behavior; docs and scripts stay out\n'
                       f'/** The owner contract ({doc} C1); see {_p("lib", "owner")}/. */\n'),
            SPEC_OTHER: f"const contract = readFileSync('{doc}', 'utf8');\n",
        })
        changed = [_p('tests', 'test_owner.py'), _p('lib', 'owner/policy.py'), doc]
        assert ts.select_frontend_specs(index, changed) == ({SPEC_OTHER}, {SPEC_OTHER: 'literal'})

    def test_transitive_extensionless_imports_select_and_unrelated_specs_stay_out(self, selection_repo):
        state = _p('frontend/src/core', 'synthetic-state.ts')
        helper = _p('frontend/src/features', 'synthetic-helper.ts')
        index = _frontend_repo(selection_repo, {
            state: 'export const state = 1;\n',
            helper: "export { state } from '../core/synthetic-state';\n",
            SPEC_RAW: "import {\n  state,\n} from './synthetic-helper';\n",
            SPEC_OTHER: "import { it } from 'vitest';\n",
        })
        selected, reasons = ts.select_frontend_specs(index, [state])
        assert selected == {SPEC_RAW}
        assert reasons[SPEC_RAW] == 'import'

    def test_deleted_import_target_still_selects_its_importer(self, selection_repo):
        removed = _p('frontend/src/core', 'removed-owner.ts')
        index = _frontend_repo(selection_repo, {
            SPEC_RAW: "import { owner } from '../core/removed-owner';\n",
        })
        assert ts.select_frontend_specs(index, [removed])[0] == {SPEC_RAW}

    @pytest.mark.parametrize('runner_input', ['vitest.config.mjs', 'package-lock.json',
                                              'package.json', 'tsconfig.json'])
    def test_runner_inputs_select_every_spec(self, selection_repo, runner_input):
        index = _frontend_repo(selection_repo, {SPEC_RAW: '', SPEC_OTHER: ''})
        selected, reasons = ts.select_frontend_specs(index, [runner_input])
        assert selected == {SPEC_RAW, SPEC_OTHER}
        assert set(reasons.values()) == {f'blast:{runner_input}'}

    def test_changed_spec_selects_itself_and_python_only_changes_select_none(self, selection_repo):
        index = _frontend_repo(selection_repo, {SPEC_RAW: '', SPEC_OTHER: ''})
        assert ts.select_frontend_specs(index, [SPEC_OTHER]) == ({SPEC_OTHER}, {SPEC_OTHER: 'self'})
        assert ts.select_frontend_specs(index, [_p('lib', 'owner.py')])[0] == set()

    def test_index_covers_exactly_the_vitest_include_glob(self, selection_repo):
        index = _frontend_repo(selection_repo, {
            SPEC_RAW: '', _p('frontend/src', 'legacy.test.cjs'): '',
            _p('tests', 'ui_nav/nav.test.ts'): '',
        })
        assert set(index) == {SPEC_RAW}
        config = open(os.path.join(ROOT, 'vitest.config.mjs'), encoding='utf-8').read()
        assert "include: ['frontend/src/**/*.test.ts']" in config


def test_vitest_hang_guard_is_the_pytest_hang_guard():
    """The gate runs both runners; neither may fail a healthy test on host speed."""
    import re
    import tomllib
    with open(os.path.join(ROOT, 'pyproject.toml'), 'rb') as fh:
        pytest_seconds = tomllib.load(fh)['tool']['pytest']['ini_options']['timeout']
    config = open(os.path.join(ROOT, 'vitest.config.mjs'), encoding='utf-8').read()
    guard = re.search(r'const PER_TEST_HANG_GUARD_MS = ([\d_]+);', config)
    assert guard and int(guard.group(1).replace('_', '')) == pytest_seconds * 1000
    assert 'testTimeout: PER_TEST_HANG_GUARD_MS' in config
    assert 'hookTimeout: PER_TEST_HANG_GUARD_MS' in config


def test_workflow_literal_selects_its_behavioral_contract():
    workflow = '.github/workflows/ci.yml'
    assert workflow in ts.refs_of_test_file("path = ROOT / '.github/workflows/ci.yml'")


def test_selector_cli_runs_api_and_unit_without_opt_in_tiers(selection_repo):
    import shutil
    import subprocess
    import sys
    root = selection_repo
    scripts = root / 'scripts'
    scripts.mkdir()
    shutil.copyfile(_SCRIPT, scripts / 'test_select.py')
    for sibling in ('test_execution.py', 'agent_wheel_manifest.py', 'check_static_gates.py'):
        shutil.copyfile(os.path.join(os.path.dirname(_SCRIPT), sibling), scripts / sibling)
    (root / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytest_timeout"\n'
        'markers = ["unit", "api", "visual", "slow", "live_llm"]\n')
    for name in ts.GUARD_CORE_BASENAMES:
        write_test(root, _p('tests', 'guards/' + name),
                   'import pytest\npytestmark = pytest.mark.unit\ndef test_guard():\n    assert True\n')
    write_test(root, _p('tests', 'domain/test_api.py'),
               'import pytest\nfrom pathlib import Path\npytestmark = pytest.mark.api\n'
               'def test_api():\n    Path("api-ran").write_text("yes")\n')
    for marker in ('visual', 'slow', 'live_llm'):
        write_test(root, _p('tests', f'optional/test_{marker}.py'),
                   f'import pytest\npytestmark = pytest.mark.{marker}\n'
                   'def test_optional():\n    pytest.fail("opt-in tier must not execute")\n')
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'initial'], cwd=root, check=True)
    result = subprocess.run([sys.executable, str(scripts / 'test_select.py'), '--run', '--jobs', '0'],
                            cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'api-ran').read_text() == 'yes'


def _selector_repo_with_spec(root):
    """A committed selector checkout whose working tree changes one section."""
    import shutil
    import subprocess
    scripts = root / 'scripts'
    scripts.mkdir()
    shutil.copyfile(_SCRIPT, scripts / 'test_select.py')
    for sibling in ('test_execution.py', 'agent_wheel_manifest.py', 'check_static_gates.py'):
        shutil.copyfile(os.path.join(os.path.dirname(_SCRIPT), sibling), scripts / sibling)
    (root / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytest_timeout"\n'
        'markers = ["unit", "api", "visual", "slow", "live_llm"]\n')
    for name in ts.GUARD_CORE_BASENAMES:
        write_test(root, _p('tests', 'guards/' + name),
                   'import pytest\npytestmark = pytest.mark.unit\ndef test_guard():\n    assert True\n')
    write_test(root, SECTION, 'var presence = 1;\n')
    write_test(root, SPEC_RAW, "import source from '../runtime/sections/synthetic_presence.js?raw';\n")
    write_test(root, SPEC_OTHER, "import { it } from 'vitest';\n")
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'initial'], cwd=root, check=True)
    write_test(root, SECTION, 'var presence = 2;\n')
    return scripts / 'test_select.py'


def _bin_with(tmp_path, npm_exit):
    """PATH holding git plus, unless npm_exit is None, a recording npm stub."""
    import shutil
    directory = tmp_path / 'bin'
    directory.mkdir()
    (directory / 'git').symlink_to(shutil.which('git'))
    if npm_exit is not None:
        stub = directory / 'npm'
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "$NPM_ARGS"\nexit {npm_exit}\n')
        stub.chmod(0o755)
    return str(directory)


@pytest.mark.parametrize('npm_exit', [0, 1])
def test_selector_cli_runs_affected_vitest_specs_and_reports_their_failure(selection_repo, tmp_path, npm_exit):
    import subprocess
    import sys
    script = _selector_repo_with_spec(selection_repo)
    record = tmp_path / 'npm-args'
    env = dict(os.environ, PATH=_bin_with(tmp_path, npm_exit), NPM_ARGS=str(record))
    result = subprocess.run([sys.executable, str(script), '--run', '--jobs', '0'],
                            cwd=selection_repo, text=True, capture_output=True, timeout=60, env=env)
    assert record.read_text().split('\n')[-2:] == [SPEC_RAW, '']
    assert SPEC_OTHER not in record.read_text()
    assert (result.returncode == 0) is (npm_exit == 0), result.stdout + result.stderr
    assert f'vitest exit={npm_exit}' in result.stdout.splitlines()[-1]


def test_selected_specs_without_a_runner_fail_closed(selection_repo, tmp_path):
    import subprocess
    import sys
    script = _selector_repo_with_spec(selection_repo)
    env = dict(os.environ, PATH=_bin_with(tmp_path, None))
    result = subprocess.run([sys.executable, str(script), '--run', '--jobs', '0'],
                            cwd=selection_repo, text=True, capture_output=True, timeout=60, env=env)
    assert result.returncode != 0
    assert 'npm' in result.stderr and SPEC_RAW in result.stdout + result.stderr
