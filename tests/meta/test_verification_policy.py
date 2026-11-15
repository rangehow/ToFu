"""Selection edits affect their consumers; actual execution-policy edits fan out."""
from pathlib import Path
import importlib.util

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_make.py', 'tests/test_app.py', 'lib/app.py',
                               'lib/router/retired_module.py', 'lib/retired_package/module.py'}


def selector():
    spec = importlib.util.spec_from_file_location('verification_policy_selector', ROOT / 'scripts/test_select.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('source', [
    'from pathlib import Path\nx = Path("Makefile").read_text()\n',
    'import subprocess\nsubprocess.run(["make", "test-unit"], check=True)\n',
    'import subprocess\nsubprocess.run("make test-unit", shell=True, check=True)\n',
])
def test_make_consumers_have_a_static_dependency(source):
    assert 'Makefile' in selector().refs_of_test_file(source)


def test_make_edits_select_consumers_and_guards_without_unrelated_application_tests():
    module = selector()
    guards = {'tests/meta/' + name: set() for name in module.GUARD_CORE_BASENAMES}
    index = {**guards, 'tests/test_make.py': {'Makefile'}, 'tests/test_app.py': {'lib/app.py'}}
    selected, reasons = module.select_tests(index, ['Makefile'])
    assert selected == set(guards) | {'tests/test_make.py'}
    assert reasons['tests/test_make.py'] == 'direct'


def _wheel_guard_index(module):
    guards = {'tests/meta/' + name: set() for name in module.GUARD_CORE_BASENAMES}
    return {**guards, module.AGENT_WHEEL_BOUNDARY_GUARD: set(), 'tests/test_app.py': set()}


@pytest.mark.parametrize('changed', [
    'lib/router/instructions.py', 'tofu_agent/cli.py', 'chromium_env.py',
    # Deleting shipped code changes the wheel too: a removed module, a removed package.
    'lib/router/retired_module.py', 'lib/retired_package/module.py',
])
def test_a_change_to_a_shipped_module_selects_the_storage_free_probe(changed):
    module = selector()
    selected, reasons = module.select_tests(_wheel_guard_index(module), [changed])
    assert reasons[module.AGENT_WHEEL_BOUNDARY_GUARD] == 'agent-wheel-boundary'
    assert 'tests/test_app.py' not in selected


@pytest.mark.parametrize('changed', [
    'lib/storage/protocol.py', 'lib/storage_authority_sql/operations_pkg/_common.py',
    'serverctl_pkg/local_placement.py', 'routes/__init__.py', 'scripts/test_select.py',
    'tests/support/agent_wheel_run.py', 'docs/TESTING_STRATEGY.md',
    'lib/model_info/data/openai.json',
])
def test_a_change_outside_the_wheel_does_not_select_the_storage_free_probe(changed):
    module = selector()
    selected, _ = module.select_tests(_wheel_guard_index(module), [changed])
    assert module.AGENT_WHEEL_BOUNDARY_GUARD not in selected


def test_a_shipped_change_refuses_a_selection_without_the_storage_free_probe():
    module = selector()
    index = _wheel_guard_index(module)
    del index[module.AGENT_WHEEL_BOUNDARY_GUARD]
    with pytest.raises(ValueError, match='required guard is missing'):
        module.select_tests(index, ['lib/router/instructions.py'])


def test_selection_implementation_is_not_a_global_runtime_input():
    module = selector()
    from lib.project_mod import landing_verification
    inputs = landing_verification._global_inputs(module)
    assert 'Makefile' not in inputs
    assert 'scripts/test_select.py' not in inputs
    assert inputs['scripts/test_execution.py'] == 'ALL'
    assert inputs['tests/conftest.py'] == 'ALL'
    assert inputs['pyproject.toml'] == 'ALL'


def execution_sources():
    definitions = ("def _parallel_workers(existing, jobs):\n    return 'auto'\n\n"
                   "def build_pytest_command(existing, jobs):\n"
                   "    return [sys.executable, '--timeout=300', '-q']\n")
    standalone = 'import sys\n\n' + definitions
    legacy = 'import sys\nBLAST_RADIUS = {"Makefile": "ALL"}\n\n' + definitions
    return legacy, standalone


def test_unchanged_extraction_keeps_execution_identity():
    from lib.project_mod.verification_policy import same_extracted_execution
    legacy, standalone = execution_sources()
    assert same_extracted_execution(legacy, standalone)
    assert same_extracted_execution('# comment\n' + legacy, standalone)
    assert same_extracted_execution(legacy + '\ndef select(index=None):\n    return index\n', standalone)


@pytest.mark.parametrize('change', [
    lambda source: source.replace('--timeout=300', '--timeout=301'),
    lambda source: source.replace("'-q'", "'-x'"),
    lambda source: source.replace("return 'auto'", "return '0'"),
    lambda source: source + '\nsys.executable = "other-python"\n',
    lambda source: source + '\nlen = lambda value: 1\n',
    lambda source: source + '\nexec("changed runtime")\n',
    lambda source: source + '\nsys.__dict__["executable"] = "other-python"\n',
    lambda source: source + '\nsys.__dict__.update(executable="other-python")\n',
    lambda source: source + '\nconfigure_runtime()\n',
    lambda source: 'invalid Python!\n' + source,
])
def test_changed_or_unknown_execution_shapes_expand_scope(change):
    from lib.project_mod.verification_policy import same_extracted_execution
    legacy, standalone = execution_sources()
    assert not same_extracted_execution(legacy, change(standalone))
    assert not same_extracted_execution(change(legacy), standalone)


def test_cli_never_broadens_by_an_arbitrary_selected_fraction(monkeypatch, capsys):
    module = selector()
    guards = {'tests/meta/' + name: set() for name in module.GUARD_CORE_BASENAMES}
    unrelated = 'tests/test_app.py'
    monkeypatch.setattr(module, 'changed_files', lambda base: ['README.md'])
    monkeypatch.setattr(module, 'build_index', lambda: {**guards, unrelated: set()})
    assert module.main(['--print']) == 0
    assert unrelated not in capsys.readouterr().out
