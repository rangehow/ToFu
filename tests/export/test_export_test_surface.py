"""Opensource export must not ship tests that import modules it dropped.

The public CI collects the shipped tests against the shipped tree. When the
export strips a first-party module (the root ``scripts/`` tree minus the keep
list, ``tofu_db/``, ``bundles/`` …) but keeps a test importing it, the public
lane fails at collection with ModuleNotFoundError — the dominant red class on
rangehow/ToFu through 2026-09. The gate in ``export_pkg._verify`` makes the
invariant mechanical: a shipped test must be collectable against the shipped
tree.
"""

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = (
    'scripts/helper.py', 'scripts/private.py', 'tests/test_retained.py',
    'tests/test_portable.py',
)

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


def _make_src(root):
    """Minimal first-party layout the verifier treats as importable roots."""
    (root / 'pkg_core' / 'pricing').mkdir(parents=True)
    (root / 'pkg_core' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'pkg_core' / 'pricing' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'scripts').mkdir()
    (root / 'scripts' / 'gen_x.py').write_text('X = 1\n', encoding='utf-8')
    (root / 'tofu_db').mkdir()
    (root / 'tofu_db' / '__init__.py').write_text('', encoding='utf-8')


def _make_dest(dest, test_sources, with_lib=True, with_gen_x=True):
    tests = dest / 'tests'
    tests.mkdir(parents=True)
    for name, source in test_sources.items():
        (tests / name).write_text(source, encoding='utf-8')
    if with_lib:
        (dest / 'pkg_core' / 'pricing').mkdir(parents=True)
        (dest / 'pkg_core' / '__init__.py').write_text('', encoding='utf-8')
        (dest / 'pkg_core' / 'pricing' / '__init__.py').write_text('', encoding='utf-8')
    if with_gen_x:
        (dest / 'scripts').mkdir(exist_ok=True)
        (dest / 'scripts' / 'gen_x.py').write_text('X = 1\n', encoding='utf-8')


def test_clean_tree_passes(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_ok.py': 'import pkg_core.pricing\nfrom scripts import gen_x\n\n'
                      'def test_x():\n    assert True\n',
    })
    _verify_exported_test_surface(dest, src)


def test_missing_root_module_is_blocked(tmp_path):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {'test_db.py': 'import tofu_db\n'})

    with pytest.raises(ExportTestSurfaceError, match=r'test_db\.py -> tofu_db'):
        _verify_exported_test_surface(dest, src)


def test_missing_second_level_submodule_is_blocked(tmp_path):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'scripts' / 'gen_wire_fixtures.py').write_text('', encoding='utf-8')
    dest = tmp_path / 'dest'
    # scripts/ survives the export, but this one generator does not.
    _make_dest(dest, {'test_wire.py': 'from scripts import gen_wire_fixtures\n'})

    with pytest.raises(ExportTestSurfaceError,
                       match=r'test_wire\.py -> scripts\.gen_wire_fixtures'):
        _verify_exported_test_surface(dest, src)


def test_importorskip_is_tolerated(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_gate.py': "import pytest\n"
                        "pytest.importorskip('tofu_db', reason='optional')\n",
    })
    _verify_exported_test_surface(dest, src)


def test_importorskip_guard_suppresses_later_first_party_imports(tmp_path):
    """The suite's `export` gate: importorskip('export') skips the whole module
    in public trees, so imports BEHIND it (e.g. export_pkg internals) can never
    execute there and must not be flagged."""
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'export.py').write_text('', encoding='utf-8')
    (src / 'export_pkg').mkdir()
    (src / 'export_pkg' / '__init__.py').write_text('', encoding='utf-8')
    (src / 'export_pkg' / '_handoff.py').write_text('', encoding='utf-8')
    dest = tmp_path / 'dest'
    # Neither export.py nor export_pkg/ ships in the public tree.
    _make_dest(dest, {
        'test_handoff.py': (
            "import pytest\n"
            "pytest.importorskip('export', reason='not shipped')\n"
            "import export_pkg._handoff as handoff  # noqa: E402\n"
        ),
    })
    _verify_exported_test_surface(dest, src)


def test_import_before_the_skip_guard_is_still_flagged(tmp_path):
    """Ordering matters: an import placed BEFORE the importorskip line would
    crash collection before the skip can fire."""
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'export.py').write_text('', encoding='utf-8')
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_order.py': (
            "import tofu_db\n"
            "import pytest\n"
            "pytest.importorskip('export', reason='not shipped')\n"
        ),
    })
    with pytest.raises(ExportTestSurfaceError, match=r'test_order\.py -> tofu_db'):
        _verify_exported_test_surface(dest, src)


def test_try_guarded_and_function_level_imports_are_tolerated(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_opt.py': (
            'try:\n'
            '    import tofu_db\n'
            'except ImportError:\n'
            '    tofu_db = None\n'
            'def test_lazy():\n'
            '    import scripts.missing_helper  # deferred, not collection-time\n'
        ),
    })
    _verify_exported_test_surface(dest, src)


def test_type_checking_imports_are_tolerated(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_typing.py': 'from typing import TYPE_CHECKING\n'
                          'if TYPE_CHECKING:\n'
                          '    import tofu_db\n',
    })
    _verify_exported_test_surface(dest, src)


def test_from_import_of_module_attribute_is_not_flagged(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    # `pricing_for_model` is a NAME inside pkg_core/pricing/__init__.py, not a
    # submodule; only the module part must resolve.
    _make_dest(dest, {
        'test_pricing.py': 'from pkg_core.pricing import pricing_for_model\n',
    })
    _verify_exported_test_surface(dest, src)


def test_package_getattr_tolerates_lazy_submodules(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'pkg_core' / 'lazy').mkdir()
    (src / 'pkg_core' / 'lazy' / '__init__.py').write_text(
        'def __getattr__(name):\n    raise AttributeError(name)\n', encoding='utf-8')
    dest = tmp_path / 'dest'
    _make_dest(dest, {'test_lazy.py': 'import pkg_core.lazy.deferred\n'})
    (dest / 'pkg_core' / 'lazy').mkdir()
    (dest / 'pkg_core' / 'lazy' / '__init__.py').write_text(
        'def __getattr__(name):\n    raise AttributeError(name)\n', encoding='utf-8')
    _verify_exported_test_surface(dest, src)


def test_third_party_imports_are_ignored(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_deps.py': 'import pytest\nimport numpy\nfrom flask import Flask\n',
    })
    _verify_exported_test_surface(dest, src)


def test_findings_report_every_offending_pair(tmp_path):
    from export import _test_surface_findings

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_a.py': 'import tofu_db\n',
        'test_b.py': 'import tofu_db\nimport scripts.missing_helper\n',
        'conftest.py': 'import tofu_db\n',
    })
    findings = _test_surface_findings(dest, src)
    # Path fragments stay split so the suite-health audit's dead-path scan
    # (category F) does not read these expectations as repo-path references.
    t = 'tests'
    assert sorted(findings) == [
        (t + '/conftest.py', 'tofu_db'),
        (t + '/test_a.py', 'tofu_db'),
        (t + '/test_b.py', 'scripts.missing_helper'),
        (t + '/test_b.py', 'tofu_db'),
    ]


def test_missing_tests_tree_is_a_noop(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    dest.mkdir()
    _verify_exported_test_surface(dest, src)


@pytest.mark.parametrize('mode', ('internal', 'opensource'))
def test_retained_generator_is_not_gitignored(tmp_path, monkeypatch, mode):
    import subprocess
    import export_pkg._export_core as core
    from export_pkg import _state

    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    source = tmp_path / 'source'
    target = tmp_path / mode
    (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/helper.py').write_text("VALUE = 'RETAINED_SAMPLE'\n")
    target.mkdir()
    (target / '.gitignore').write_text('/scripts/*\n')
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES', {'scripts/helper.py'})
    monkeypatch.setattr(core, '_sanitize_source_opensource',
                        lambda text, rel: text.replace('RETAINED_SAMPLE', 'PUBLIC_SAMPLE'))
    core._restore_export_kept_files(target, mode)
    expected = 'PUBLIC_SAMPLE' if mode == 'opensource' else 'RETAINED_SAMPLE'
    assert (target / 'scripts/helper.py').read_text() == f"VALUE = '{expected}'\n"
    subprocess.run(['git', 'init', '-q'], cwd=target, check=True)
    retained = subprocess.run(['git', 'check-ignore', 'scripts/helper.py'], cwd=target)
    private = subprocess.run(['git', 'check-ignore', 'scripts/private.py'], cwd=target, capture_output=True)
    assert retained.returncode == 1
    assert private.returncode == 0


@pytest.mark.parametrize('keep_helper', (True, False))
def test_internal_export_restores_and_checks_test_dependencies(tmp_path, monkeypatch, keep_helper):
    import export_pkg._export_core as core
    import export_pkg._orchestrate as orchestrate
    from export_pkg import _state
    from export_pkg._verify import ExportTestSurfaceError, _verify_exported_test_surface

    source = tmp_path / 'source'
    (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/helper.py').write_text('VALUE = 1\n')
    target = tmp_path / 'internal'
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(orchestrate, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES',
                        {'scripts/helper.py'} if keep_helper else set())
    monkeypatch.setattr(_state, '_EXPORT_VERSION', None)
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    monkeypatch.setattr(orchestrate, '_read_version', lambda: '0.18.2')

    def copy_source(mode, dest, **kwargs):
        assert mode == 'internal'
        (dest / 'tests').mkdir(parents=True)
        (dest / 'tests/test_retained.py').write_text('import scripts.helper\n')
        (dest / '.gitignore').write_text('/scripts/*\n')
        return {'copied': 2, 'sanitized': 0}

    checked = []

    def verify(dest):
        _verify_exported_test_surface(dest, source)
        checked.append(dest)

    monkeypatch.setattr(orchestrate, '_export_via_tar_with_sanitize', copy_source)
    monkeypatch.setattr(orchestrate, '_verify_exported_test_surface', verify)
    for name in ('_create_skeleton', '_print_export_summary', '_verify_exported_js_syntax',
                 '_verify_exported_py_syntax', '_verify_exported_py_integrity'):
        monkeypatch.setattr(orchestrate, name, lambda *args: None)

    if keep_helper:
        orchestrate.export_project('internal', target)
        assert (target / 'scripts/helper.py').read_text() == 'VALUE = 1\n'
        assert checked == [target]
    else:
        with pytest.raises(ExportTestSurfaceError, match=r'scripts\.helper'):
            orchestrate.export_project('internal', target)
        assert not checked


@pytest.mark.parametrize('optional_import', (
    'def later():\n    import scripts.gen_x\n',
    'try:\n    import scripts.gen_x\nexcept ImportError:\n    pass\n',
))
def test_optional_sibling_never_waives_mandatory_import(tmp_path, optional_import):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    source = tmp_path / 'source'
    source.mkdir()
    _make_src(source)
    target = tmp_path / 'target'
    _make_dest(target, {
        'test_required.py': 'import scripts.missing_helper\n' + optional_import,
    })
    with pytest.raises(ExportTestSurfaceError, match=r'scripts\.missing_helper'):
        _verify_exported_test_surface(target, source)


@pytest.mark.parametrize('helper', (
    'scripts/fetch_chatgpt_share.py',
    'scripts/inspect_native_crash.py',
    'scripts/tofudb_profile_report.py',
    'benchmarks/project_content_fastpath.py',
    'benchmarks/search_html_reference.py',
    'benchmarks/search_relevance.py',
    'benchmarks/tofudb_context_progression.py',
    'benchmarks/model_dispatch_barrier.py',
    'benchmarks/model_request_cpu.py',
    'benchmarks/round_prep_body.py',
    'benchmarks/round_token_count_cpu.py',
    'benchmarks/route_group_retirement.py',
    'benchmarks/storage_history_cohort.py',
    'benchmarks/task_event_delivery_contention.py',
    'benchmarks/turn_event_batch.py',
    'scripts/analyze_agent_load.py',
    'scripts/harness_metrics.py',
))
@pytest.mark.parametrize('mode', ('internal', 'opensource'))
def test_portable_diagnostic_test_dependencies_follow_retained_source_policy(
        tmp_path, monkeypatch, helper, mode):
    import export_pkg._export_core as core
    from export_pkg import _lists, _state
    from export_pkg._verify import _verify_exported_test_surface

    source = tmp_path / 'source'
    path = source / helper
    path.parent.mkdir(parents=True)
    path.write_text('VALUE = 1\n')
    target = tmp_path / mode
    (target / 'tests').mkdir(parents=True)
    module = helper.removesuffix('.py').replace('/', '.')
    (target / 'tests/test_portable.py').write_text(f'import {module}\n')
    (target / '.gitignore').write_text('/scripts/*\n/benchmarks/*\n')
    # Select only this diagnostic from the real authority; an absent entry
    # must remain a collection failure, never become a fixture exemption.
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES',
                        {helper} & _lists._OPENSOURCE_KEEP_FILES)
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    core._restore_export_kept_files(target, mode)
    _verify_exported_test_surface(target, source)
    assert (target / helper).read_text() == 'VALUE = 1\n'
    assert not (target / 'export_pkg').exists()


_PATH_LOADING_TEST = (
    'import importlib.util\n'
    'from pathlib import Path\n'
    'import pytest\n\n'
    'ROOT = Path(__file__).resolve().parents[1]\n'
    'HERE = Path(__file__).resolve().parent\n\n'
    '@pytest.fixture\n'
    'def helper():\n'
    "    spec = importlib.util.spec_from_file_location('helper', ROOT / 'benchmarks' / 'helper.py')\n"
    '    module = importlib.util.module_from_spec(spec)\n'
    '    spec.loader.exec_module(module)\n'
    '    return module\n'
)


def _make_path_loading_src(root):
    _make_src(root)
    (root / 'benchmarks').mkdir()
    (root / 'benchmarks' / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')


def test_path_loaded_first_party_helper_missing_from_export_is_blocked(tmp_path):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_path_loading_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {'test_measure.py': _PATH_LOADING_TEST})

    with pytest.raises(ExportTestSurfaceError,
                       match=r'test_measure\.py -> benchmarks/helper\.py'):
        _verify_exported_test_surface(dest, src)

    (dest / 'benchmarks').mkdir()
    (dest / 'benchmarks' / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')
    _verify_exported_test_surface(dest, src)


@pytest.mark.parametrize('load', (
    # Module-relative and dynamic paths are not repository-root files.
    "importlib.util.spec_from_file_location('local', HERE / 'helper.py')\n",
    "importlib.util.spec_from_file_location('dyn', ROOT / 'benchmarks' / NAME)\n",
    # A module-level importorskip of an unshipped root skips the whole module.
    "pytest.importorskip('tofu_db')\n"
    "importlib.util.spec_from_file_location('helper', ROOT / 'benchmarks' / 'helper.py')\n",
))
def test_path_loads_that_cannot_name_a_dropped_repository_file_pass(tmp_path, load):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_path_loading_src(src)
    dest = tmp_path / 'dest'
    source = ('import importlib.util\nfrom pathlib import Path\nimport pytest\n'
              "ROOT = Path(__file__).resolve().parents[1]\n"
              "HERE = Path(__file__).resolve().parent\nNAME = 'helper.py'\n" + load)
    _make_dest(dest, {'test_local.py': source})
    _verify_exported_test_surface(dest, src)


def test_public_installer_contract_does_not_require_private_export_toolchain(tmp_path):
    from pathlib import Path
    from export_pkg._lists import ROOT
    from export_pkg._verify import _verify_exported_test_surface

    target = tmp_path / 'public'
    test = Path('tests/installer/test_install_carried_user_state.py')
    (target / test).parent.mkdir(parents=True)
    (target / test).write_bytes((ROOT / test).read_bytes())
    _verify_exported_test_surface(target, ROOT)
    assert not (target / 'export_pkg').exists()


def test_recovery_selector_public_surface_with_synthetic_policy(tmp_path):
    """Exercise real gate/sanitizer functions without importing secret lists."""
    import __future__
    import ast
    import logging
    import os
    from pathlib import Path
    import re
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    selector = 'serverctl_pkg/tofudb_restore_selectors.py'
    behavior_path = 'tests/tofudb/test_restore_selectors.py'

    def public_functions(relative, namespace):
        tree = ast.parse((root / relative).read_text(encoding='utf-8'))
        # Imports are excluded so no live policy/secret authority can be loaded.
        tree.body = [node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
        exec(compile(tree, relative, 'exec', flags=__future__.annotations.compiler_flag),
             namespace)
        return namespace

    verify = public_functions('export_pkg/_verify.py', {
        'ast': ast, 'os': os, 'Path': Path, 'ROOT': tmp_path,
        'logger': logging.getLogger(__name__),
        'C_GREEN': '', 'C_END': '', 'C_RED': '', 'C_DIM': '',
    })
    sanitize = public_functions('export_pkg/_sanitize.py', {
        'os': os, 're': re,
        '_SECRETS': {'synthetic-policy-secret': 'redacted'},
        '_ENDPOINTS': {'synthetic-policy.invalid': 'public.example'},
        '_INTERNAL_DOMAIN_LITERALS': ('.synthetic-policy.invalid',),
        '_INTERNAL_IDENTIFIER_REPLACEMENTS': (('synthetic_private_id', 'public_id'),),
    })['_sanitize_source_opensource']
    probe = sanitize('synthetic-policy-secret synthetic-policy.invalid synthetic_private_id',
                     'probe.txt')
    assert probe == 'redacted public.example public_id'

    exclude = public_functions('export_pkg/_excludes.py', {
        'Path': Path, 'is_agent_artifact': lambda name: name.startswith('.tofu'),
        '_TOP_LEVEL_ONLY_EXCLUDE_DIRS': set(),
        'ALWAYS_EXCLUDE_ROOT_ONLY_DIRS': {'data', 'logs'},
        '_TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS': {'scripts'},
        '_OPENSOURCE_EXCLUDE_PATHS': set(),
        'ALWAYS_EXCLUDE_DIRS': {'export_pkg'},
        'OPENSOURCE_EXTRA_EXCLUDE_DIRS': {'bundles'},
        'ALWAYS_EXCLUDE_FILES': set(),
        'ALWAYS_EXCLUDE_ROOT_ONLY_FILES': {'export.py'},
        'OPENSOURCE_EXTRA_EXCLUDE_FILES': set(), 'ALWAYS_EXCLUDE_GLOBS': set(),
    })['_should_exclude']
    assert exclude(selector, Path(selector).name, 'opensource') is None
    for private in ('scripts/tofudb_restore_selectors.py', 'scripts/private.py',
                    'export.py', 'export_pkg/private.py', 'data/private.json',
                    'bundles/private.py'):
        assert exclude(private, Path(private).name, 'opensource') is not None

    source, staged = tmp_path / 'source', tmp_path / 'staged'
    for relative in ('serverctl_pkg/__init__.py', selector, behavior_path):
        assert exclude(relative, Path(relative).name, 'opensource') is None
        original = (root / relative).read_text(encoding='utf-8')
        sanitized = sanitize(original, relative)
        assert sanitized == original
        for directory, text in ((source, original), (staged, sanitized)):
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    # Recreate precisely the rejected edge, then fix only that edge on the same input.
    old_module = source / 'scripts/tofudb_restore_selectors.py'
    old_module.parent.mkdir()
    old_module.write_text((source / selector).read_text(encoding='utf-8'), encoding='utf-8')
    behavior = staged / behavior_path
    current_test = behavior.read_text(encoding='utf-8')
    behavior.write_text(current_test.replace('from serverctl_pkg.tofudb_restore_selectors',
                                            'from scripts.tofudb_restore_selectors'),
                        encoding='utf-8')
    error = verify['ExportTestSurfaceError']
    gate = verify['_verify_exported_test_surface']
    with pytest.raises(error, match=r'test_restore_selectors.py.*scripts.tofudb_restore_selectors'):
        gate(staged, source)
    behavior.write_text(current_test, encoding='utf-8')
    gate(staged, source)
    assert not (staged / 'scripts').exists()
    assert not (staged / 'export.py').exists()
    assert not (staged / 'export_pkg').exists()

    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-c', '/dev/null', '--noconftest',
         '-o', 'addopts=', '-p', 'no:cacheprovider', behavior_path],
        cwd=staged, env={'PATH': os.defpath, 'HOME': str(tmp_path),
                         'TMPDIR': str(tmp_path), 'PYTHONPATH': str(staged),
                         'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '8 passed' in result.stdout
    assert 'skipped' not in result.stdout


def test_private_pilot_cli_ownership_preserves_internal_unit_selection(tmp_path):
    import shlex
    import subprocess
    import sys
    from pathlib import Path
    from export_pkg._excludes import _should_exclude
    from export_pkg._lists import ROOT, _OPENSOURCE_KEEP_FILES
    from tests.meta.test_gitignore_covers_export_excludes import _EXTRA_KEEPERS

    private = 'tests/longform/test_research_loop_cli.py'
    public = 'tests/longform/test_experiment_stage.py'
    assert private in _EXTRA_KEEPERS
    assert public not in _EXTRA_KEEPERS
    for mode in ('internal', 'personal'):
        assert _should_exclude(private, Path(private).name, mode) is None
        assert _should_exclude('scripts/research_loop.py', 'research_loop.py', mode) is None
    assert _should_exclude(private, Path(private).name, 'opensource') is not None
    assert _should_exclude(public, Path(public).name, 'opensource') is None
    assert 'scripts/research_loop.py' not in _OPENSOURCE_KEEP_FILES
    assert 'scripts/document_pipeline.py' not in _OPENSOURCE_KEEP_FILES
    recipe = subprocess.run(['make', '-n', 'test-unit', 'JOBS=2'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout
    parallel = next(line for line in recipe.splitlines() if 'unit and not serial' in line)
    command = shlex.split(parallel)
    command = [sys.executable, *command[command.index('-m'):], private, '--collect-only']
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'test_cli_refuses_without_host_authority_before_dispatch' in result.stdout
    assert '1 test collected' in result.stdout


def test_retained_experiment_host_collects_without_private_launcher(tmp_path):
    import ast
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    from export_pkg._excludes import _should_exclude
    from export_pkg._lists import ROOT

    public = 'tests/longform/test_experiment_stage.py'
    staged = tmp_path / 'public'
    paths = subprocess.run(
        ['git', 'ls-files', '--', '*.py'], cwd=ROOT,
        capture_output=True, text=True, check=True).stdout.splitlines()
    for relative in paths:
        path = Path(relative)
        if not (relative == public or relative.startswith(('lib/', 'packages/tofu-environments/'))
                or len(path.parts) == 1):
            continue
        if _should_exclude(relative, path.name, 'opensource') is not None:
            continue
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    tree = ast.parse((staged / public).read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(isinstance(node, ast.ImportFrom) and node.module == 'scripts' for node in imports)
    assert not (staged / 'scripts').exists()
    assert not (staged / 'export.py').exists()
    assert not (staged / 'export_pkg').exists()
    probe = (
        'import importlib.util, pathlib, pytest, sys; '
        'assert importlib.util.find_spec("export") is None; '
        'assert importlib.util.find_spec("scripts.research_loop") is None '
        'if importlib.util.find_spec("scripts") else True; '
        'code = pytest.main(sys.argv[1:]); '
        'root = pathlib.Path.cwd(); '
        'assert all(pathlib.Path(m.__file__).is_relative_to(root) '
        'for n, m in sys.modules.items() '
        'if n == "lib" or n.startswith(("lib.", "tofu_environments"))); '
        'raise SystemExit(code)'
    )
    result = subprocess.run(
        [sys.executable, '-c', probe, '--collect-only', '-q', '-c', '/dev/null',
         '--noconftest', '-o', 'addopts=', '-p', 'no:cacheprovider', public],
        cwd=staged, env={'PATH': os.defpath, 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path),
                         'PYTHONPATH': str(staged), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'test_cli_refuses_without_host_authority_before_dispatch' not in result.stdout
    for control in ('test_foreign_actor_and_no_authority_refused',
                    'test_generated_smoke_is_not_executed_with_host_environment',
                    'test_binding_mismatch_refuses_before_submit'):
        assert control in result.stdout
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
            assert node.name in result.stdout
    assert 'skipped' not in result.stdout


def test_deliverable_exact_private_owner_preserves_unit_selection():
    import ast
    from pathlib import Path
    import subprocess

    root = Path(__file__).resolve().parents[2]
    private = 'tests/production/test_deliverable_cli.py'
    public = {'tests/production/test_deliverable_launch.py',
              'tests/longform/test_portfolio_policy.py'}
    cases = {'test_document_cli_run_refuses_missing_inputs_before_any_model_request',
             'test_document_cli_run_goes_through_the_launcher',
             'test_contract_file_cli_preflight_and_run',
             'test_cli_propagates_owner_profile_to_real_entrypoint'}

    def literal_set(path, name):
        tree = ast.parse((root / path).read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
        return ast.literal_eval(node.value)

    excludes = literal_set('export_pkg/_lists.py', '_OPENSOURCE_EXCLUDE_PATHS')
    keepers = literal_set('tests/meta/test_gitignore_covers_export_excludes.py', '_EXTRA_KEEPERS')
    helpers = literal_set('export_pkg/_lists.py', '_OPENSOURCE_KEEP_FILES')
    assert private in excludes and private in keepers
    assert not public & (excludes | keepers)
    assert not {'scripts/document_pipeline.py', 'scripts/deliverable_run.py'} & helpers
    tree = ast.parse((root / private).read_text(encoding='utf-8'))
    assert {n.name for n in tree.body if isinstance(n, ast.FunctionDef)
            and n.name.startswith('test_')} == cases
    assert any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name)
               and t.id == 'pytestmark' for t in n.targets)
               and ast.unparse(n.value) == 'pytest.mark.unit' for n in tree.body)
    for relative in public:
        tree = ast.parse((root / relative).read_text(encoding='utf-8'))
        assert not any(isinstance(n, ast.FunctionDef) and n.name in cases for n in tree.body)
        assert not any(isinstance(n, ast.ImportFrom) and n.module == 'scripts'
                       for n in ast.walk(tree))
        assert not any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                       and ('document_pipeline.py' in n.value or n.value == 'deliverable_run')
                       for n in ast.walk(tree))
    recipe = subprocess.run(['make', '-n', 'test-unit', 'JOBS=2'], cwd=root,
                            capture_output=True, text=True, check=True).stdout
    parallel = next(line for line in recipe.splitlines() if 'unit and not serial' in line)
    assert private not in parallel and '--ignore=tests/production' not in parallel
    assert '--ignore=tests/longform' not in parallel
    assert 'test-unit' in (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')


def test_deliverable_retained_candidate_has_no_private_fallback(tmp_path):
    import ast
    from pathlib import Path
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    public = ('tests/production/test_deliverable_launch.py',
              'tests/longform/test_portfolio_policy.py')
    private = 'tests/production/test_deliverable_cli.py'
    candidate = tmp_path / 'candidate'
    paths = subprocess.run(['git', 'ls-files', '--', '*.py'], cwd=root,
                           capture_output=True, text=True, check=True).stdout.splitlines()
    exact = {*public, 'tests/__init__.py', 'tests/production/__init__.py',
             'tests/longform/__init__.py', 'runtime_path_policy.py',
             'tofu_extensions.py', 'utils.py',
             'contracts/deliverable_launch_v1.schema.json',
             'tests/production/test_deliverable_contract_validation.py'}
    for relative in sorted(set(paths) | exact):
        if relative not in exact and not relative.startswith('lib/'):
            continue
        source = root / relative
        if not source.is_file():
            continue
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    probe = '''
import importlib.abc, importlib.machinery, pathlib, socket, sys
root = pathlib.Path.cwd()
class LocalOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'scripts', 'export', 'export_pkg', 'evaluations'}:
            raise ModuleNotFoundError(fullname)
        if fullname.split('.')[0] in {'lib', 'tests', 'runtime_path_policy',
                                     'tofu_extensions', 'utils'}:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or (spec.origin and not pathlib.Path(spec.origin).is_relative_to(root)):
                raise ModuleNotFoundError(fullname)
            return spec
sys.meta_path.insert(0, LocalOnly())
def denied(*a, **k):
    raise AssertionError('offline candidate attempted network')
socket.socket.connect = denied
socket.create_connection = denied
import pytest
code = pytest.main(sys.argv[1:])
assert all(pathlib.Path(m.__file__).is_relative_to(root)
           for n, m in sys.modules.items() if getattr(m, '__file__', None)
           and (n == 'lib' or n.startswith(('lib.', 'tests.'))))
assert not any(n.split('.')[0] in {'scripts', 'export', 'export_pkg', 'evaluations'}
               for n in sys.modules)
raise SystemExit(code)
'''
    environment = {'PATH': str(tmp_path / 'empty-bin'), 'HOME': str(tmp_path),
                   'TMPDIR': str(tmp_path), 'PYTHONPATH': str(candidate),
                   'TOFU_DATA_DIR': str(tmp_path / 'state'),
                   'TOFU_PROJECT_PATH': str(candidate),
                   'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    args = ['-q', '-c', '/dev/null', '--noconftest', '-o', 'addopts=',
            '-p', 'no:cacheprovider']
    for absent in ('scripts', 'export.py', 'export_pkg', 'evaluations', 'data'):
        assert not (candidate / absent).exists()
    collected = subprocess.run([sys.executable, '-c', probe, *args, '--collect-only', *public],
                               cwd=candidate, env=environment, capture_output=True,
                               text=True, check=False)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert 'skipped' not in collected.stdout
    for relative in public:
        tree = ast.parse((candidate / relative).read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
                assert node.name in collected.stdout
    pure = subprocess.run([sys.executable, '-c', probe, *args, public[0],
                           '-k', 'schema_declares or malformed_contracts or relative_paths or '
                           'document_contract_preserves'], cwd=candidate, env=environment,
                          capture_output=True, text=True, check=False)
    assert pure.returncode == 0, pure.stdout + pure.stderr
    assert '12 passed' in pure.stdout and 'skipped' not in pure.stdout
    validation = subprocess.run(
        [sys.executable, '-c', probe, *args,
         'tests/production/test_deliverable_contract_validation.py'],
        cwd=candidate, env=environment, capture_output=True, text=True, check=False)
    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert '5 passed' in validation.stdout and 'skipped' not in validation.stdout
    target = candidate / private
    shutil.copyfile(root / private, target)
    internal = subprocess.run([sys.executable, '-c', probe, *args, '--collect-only',
                               '-m', 'unit and not serial', private], cwd=candidate,
                              env=environment, capture_output=True, text=True, check=False)
    assert internal.returncode == 0, internal.stdout + internal.stderr
    assert '4 tests collected' in internal.stdout and 'skipped' not in internal.stdout
    for name in ('test_document_cli_run_refuses_missing_inputs_before_any_model_request',
                 'test_document_cli_run_goes_through_the_launcher',
                 'test_contract_file_cli_preflight_and_run',
                 'test_cli_propagates_owner_profile_to_real_entrypoint'):
        assert name in internal.stdout
