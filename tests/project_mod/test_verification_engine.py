"""The verification front door checks frozen edits and explains evidence reuse."""
from pathlib import Path

import pytest

from tests.project_mod.test_landing_verification import (
    commit, git, private_local_root as private_local_root, shared as shared, write,
)

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/test_calc.py', 'tests/test_new.py', 'tests/test_untracked.py', 'tests/missing.py',
    'tests/test_root.py',
}


def verify(root, paths=None, **kwargs):
    from lib.project_mod.verification import verify_tests
    return verify_tests(root, paths or ['tests/test_calc.py'], **kwargs)


def test_completed_run_is_reused_without_starting_pytest(shared, monkeypatch):
    from lib.project_mod import landing_verification
    first = verify(shared)
    assert first.status == 'passed'
    assert first.executed == ('tests/test_calc.py',)
    assert first.recorded == 1

    def unexpected(*args, **kwargs):
        pytest.fail('unchanged complete evidence must not restart pytest')

    monkeypatch.setattr(landing_verification, '_run', unexpected)
    monkeypatch.setattr(landing_verification, 'merge_checkout', unexpected)
    second = verify(shared)
    assert second.status == 'passed'
    assert second.executed == ()
    assert second.reused == ('tests/test_calc.py',)
    assert second.tree == first.tree


def test_uncommitted_bytes_are_checked_without_changing_the_real_index_or_branch(shared):
    original_head = git(shared, 'rev-parse', 'HEAD')
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    git(shared, 'add', 'pkg/calc.py')
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    write(shared, 'tests/test_new.py', 'def test_new():\n    assert False\n')
    index = Path(git(shared, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
    before_index = index.read_bytes()
    before_diff = git(shared, 'diff', 'HEAD')
    result = verify(shared, ['tests'])
    assert result.status == 'failed'
    assert len(result.failed_cases) == 2
    assert git(shared, 'rev-parse', 'HEAD') == original_head
    assert index.read_bytes() == before_index
    assert git(shared, 'diff', 'HEAD') == before_diff
    assert (shared / 'tests/test_new.py').exists()


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_capture_includes_working_bytes_hidden_by_index_flags(shared, flag):
    from lib.project_mod.lanes import capture_working_tree
    git(shared, 'update-index', flag, 'pkg/calc.py')
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    index = Path(git(shared, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
    original = index.read_bytes()
    _, tree = capture_working_tree(shared)
    assert git(shared, 'show', f'{tree}:pkg/calc.py') == (shared / 'pkg/calc.py').read_text()
    assert index.read_bytes() == original


def test_used_source_change_invalidates_evidence_and_failures_are_never_reused(shared):
    assert verify(shared).status == 'passed'
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    changed = verify(shared)
    assert changed.status == 'failed'
    assert 'pkg/calc.py' in changed.reasons['tests/test_calc.py']
    again = verify(shared)
    assert again.status == 'failed' and again.executed == ('tests/test_calc.py',)


def test_environment_change_invalidates_an_otherwise_identical_run(shared, monkeypatch):
    write(shared, 'tests/test_calc.py',
          'import os\nfrom pkg.calc import scale\ndef test_environment():\n'
          '    assert scale(2) == int(os.environ["TOFU_TEST_SCENARIO_EXPECTED"])\n')
    monkeypatch.setenv('TOFU_TEST_SCENARIO_EXPECTED', '4')
    assert verify(shared).status == 'passed'
    monkeypatch.setenv('TOFU_TEST_SCENARIO_EXPECTED', '5')
    assert verify(shared).status == 'failed'


def test_a_pass_recorded_under_one_launcher_is_reused_under_another(shared, monkeypatch):
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/conversations/a/lane')
    assert verify(shared).executed == ('tests/test_calc.py',)
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/conversations/b/lane')
    monkeypatch.setenv('TOFU_DATA_DIR', '/server/data')
    again = verify(shared)
    assert again.status == 'passed' and again.executed == ()
    assert again.reused == ('tests/test_calc.py',)


def test_returning_environment_reuses_pass_without_consuming_the_other_environment_failure(shared, monkeypatch):
    write(shared, 'tests/test_calc.py',
          'import os\nfrom pkg.calc import scale\ndef test_environment():\n'
          '    assert scale(2) == int(os.environ["TOFU_TEST_SCENARIO_EXPECTED"])\n')
    monkeypatch.setenv('TOFU_TEST_SCENARIO_EXPECTED', '4')
    first = verify(shared)
    assert first.status == 'passed' and first.executed == ('tests/test_calc.py',)
    monkeypatch.setenv('TOFU_TEST_SCENARIO_EXPECTED', '5')
    assert verify(shared).status == 'failed'
    monkeypatch.setenv('TOFU_TEST_SCENARIO_EXPECTED', '4')
    restored = verify(shared)
    assert restored.status == 'passed'
    assert restored.executed == ()
    assert restored.reused == ('tests/test_calc.py',)


@pytest.mark.parametrize('body', ['pytest.skip("optional")', 'pytest.exit("interrupted")'])
def test_incomplete_evidence_is_explicit_and_cannot_turn_green(shared, body):
    write(shared, 'tests/test_calc.py', 'import pytest\ndef test_case():\n    ' + body + '\n')
    result = verify(shared)
    assert result.status == 'incomplete'
    assert result.unverified == ('tests/test_calc.py',)
    assert result.recorded == 0


def test_unobserved_subprocesses_always_execute(shared):
    write(shared, 'tests/test_calc.py',
          'import subprocess, sys\ndef test_child():\n'
          '    subprocess.run([sys.executable, "-c", "pass"], check=True)\n')
    assert verify(shared).status == 'passed'
    second = verify(shared)
    assert second.executed == ('tests/test_calc.py',)
    assert 'unobserved' in second.reasons['tests/test_calc.py']


def test_plan_and_invalid_paths_do_not_execute_tests(shared, monkeypatch):
    from lib.project_mod import landing_verification

    def unexpected(*args, **kwargs):
        pytest.fail('planning and invalid scopes cannot execute tests')

    monkeypatch.setattr(landing_verification, '_run', unexpected)
    result = verify(shared, execute=False)
    assert result.status == 'planned' and result.executed == ()
    assert result.selected == ('tests/test_calc.py',)
    with pytest.raises(ValueError, match='matched'):
        verify(shared, ['tests/test_calc.py', 'tests/missing.py'])


@pytest.fixture
def package_repo(tmp_path):
    """A repository suite plus a package suite, under the shipped selector and execution policy."""
    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'trunk')
    git(repo, 'config', 'user.name', 'Verifier')
    git(repo, 'config', 'user.email', 'verifier@example.invalid')
    write(repo, '.gitignore', '__pycache__/\n')
    for script in ('scripts/test_select.py', 'scripts/test_execution.py',
                   'scripts/agent_wheel_manifest.py', 'tests/__init__.py',
                   'tests/support/__init__.py', 'tests/support/process_priority.py',
                   'tests/support/lower_priority_plugin.py'):
        write(repo, script, (root / script).read_text())
    write(repo, 'pyproject.toml', '[tool.pytest.ini_options]\naddopts = "-p pytest_timeout"\n'
          'markers = ["unit", "api", "visual", "slow", "live_llm"]\n')
    write(repo, 'tests/test_root.py', 'import pytest\npytestmark = pytest.mark.unit\n'
          'def test_root():\n    assert True\n')
    # The package's own configuration must apply: its marker is strict and registered only there.
    write(repo, 'packages/demo/pyproject.toml',
          '[tool.pytest.ini_options]\naddopts = "--strict-markers"\nmarkers = ["demo_only"]\n')
    write(repo, 'packages/demo/demo/__init__.py', 'VALUE = 3\n')
    write(repo, 'packages/demo/tests/test_package.py',
          'import os\nimport pytest\nfrom demo import VALUE\n\n@pytest.mark.demo_only\n'
          'def test_package():\n    assert os.path.basename(os.getcwd()) == "demo"\n'
          '    assert VALUE == 3\n')
    commit(repo, 'repository and package suites')
    return repo


def test_package_suites_run_from_their_directory_and_reuse_their_evidence(package_repo):
    scope = ['tests', 'packages/demo/tests']
    first = verify(package_repo, scope)
    assert first.status == 'passed', first.evidence
    assert first.executed == ('packages/demo/tests/test_package.py', 'tests/test_root.py')
    assert first.recorded == 2
    again = verify(package_repo, scope)
    assert again.executed == ()
    assert again.reused == ('packages/demo/tests/test_package.py', 'tests/test_root.py')
    write(package_repo, 'packages/demo/demo/__init__.py', 'VALUE = 4\n')
    changed = verify(package_repo, scope)
    assert changed.status == 'failed'
    assert changed.executed == ('packages/demo/tests/test_package.py',)
    assert 'packages/demo/demo/__init__.py' in changed.reasons['packages/demo/tests/test_package.py']


def test_a_package_configuration_change_invalidates_its_evidence(package_repo):
    assert verify(package_repo, ['packages/demo']).status == 'passed'
    write(package_repo, 'packages/demo/pyproject.toml',
          '[tool.pytest.ini_options]\naddopts = "--strict-markers"\nmarkers = []\n')
    result = verify(package_repo, ['packages/demo'])
    assert result.status != 'passed'
    assert 'packages/demo/pyproject.toml' in result.reasons['packages/demo/tests/test_package.py']


def test_a_failing_session_stays_in_the_evidence_a_later_session_would_push_out(package_repo):
    # The later session's warning summary alone outgrows the reported evidence tail.
    write(package_repo, 'packages/zeta/tests/test_later.py',
          'import warnings\n\ndef test_later():\n    for index in range(80):\n'
          '        warnings.warn(f"later session warning {index}")\n')
    write(package_repo, 'packages/demo/demo/__init__.py', 'VALUE = 4\n')
    result = verify(package_repo, ['packages'])
    assert result.status == 'failed'
    assert result.failed_cases == ('packages/demo/tests/test_package.py::test_package',)
    assert 'assert 4 == 3' in result.evidence


def test_cli_json_identifies_snapshot_and_each_phase(shared, capsys):
    import json
    from lib.project_mod.verification import main
    assert main(['run', 'tests/test_calc.py', '--into', str(shared), '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'passed'
    assert result['tree'] == git(shared, 'rev-parse', 'HEAD^{tree}').strip()
    assert {'snapshot', 'prepare', 'index', 'plan', 'execute', 'record', 'total'} <= result['seconds'].keys()


def test_new_untracked_test_is_included_in_the_snapshot(shared):
    write(shared, 'tests/test_untracked.py', 'def test_new():\n    assert True\n')
    result = verify(shared, ['tests/test_untracked.py'])
    assert result.status == 'passed' and result.recorded == 1
    assert '?? tests/test_untracked.py' in git(shared, 'status', '--short')


def test_edits_after_capture_are_not_mistaken_for_the_tested_snapshot(shared, monkeypatch):
    from lib.project_mod import landing_verification
    original = landing_verification._run

    def edit_live_checkout(*args, **kwargs):
        write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
        return original(*args, **kwargs)

    monkeypatch.setattr(landing_verification, '_run', edit_live_checkout)
    result = verify(shared)
    assert result.status == 'passed'
    monkeypatch.setattr(landing_verification, '_run', original)
    assert verify(shared).status == 'failed'


def test_a_test_rewriting_its_snapshot_cannot_publish_evidence(shared):
    write(shared, 'tests/test_calc.py',
          'from pathlib import Path\ndef test_rewrite():\n'
          '    Path("pkg/calc.py").write_text("def scale(x): return 100\\n")\n')
    result = verify(shared)
    assert result.status == 'incomplete'
    assert result.recorded == 0
    assert 'snapshot' in result.evidence.lower()
    assert (shared / 'pkg/calc.py').read_text() == 'def scale(x):\n    return x * 2\n'


def test_interpreter_bytecode_is_not_mistaken_for_new_source(shared):
    (shared / '.gitignore').unlink()
    result = verify(shared)
    assert result.status == 'passed' and result.recorded == 1


def test_a_new_conftest_invalidates_preexisting_evidence(shared):
    assert verify(shared).status == 'passed'
    write(shared, 'tests/conftest.py',
          'def pytest_runtest_setup(item):\n    raise RuntimeError("new fixture broke setup")\n')
    result = verify(shared)
    assert result.status == 'failed'
    assert 'conftest.py' in result.reasons['tests/test_calc.py']


def test_a_previously_absent_read_file_is_a_dependency(shared):
    write(shared, 'tests/test_calc.py',
          'from pathlib import Path\ndef test_optional_file():\n'
          '    try:\n        value = Path("optional.txt").read_text()\n'
          '    except FileNotFoundError:\n        return\n'
          '    assert value == "expected"\n')
    assert verify(shared).status == 'passed'
    write(shared, 'optional.txt', 'changed')
    assert verify(shared).status == 'failed'


def test_external_file_reads_require_fresh_execution(shared, tmp_path):
    external = tmp_path / 'external-input.txt'
    external.write_text('expected')
    write(shared, 'tests/test_calc.py',
          'from pathlib import Path\ndef test_external():\n'
          f'    assert Path({str(external)!r}).read_text() == "expected"\n')
    assert verify(shared).status == 'passed'
    external.write_text('changed')
    result = verify(shared)
    assert result.status == 'failed'
    assert 'unobserved' in result.reasons['tests/test_calc.py']


def test_changing_directory_cannot_hide_an_external_input(shared, tmp_path):
    external = tmp_path / 'outside'
    external.mkdir()
    (external / 'input.txt').write_text('expected')
    write(shared, 'input.txt', 'expected')
    write(shared, 'tests/test_calc.py',
          'import os\nfrom pathlib import Path\ndef test_relative():\n'
          '    assert Path("input.txt").read_text() == "expected"\n'
          '    original = os.getcwd()\n    try:\n'
          f'        os.chdir({str(external)!r})\n'
          '        assert Path("input.txt").read_text() == "expected"\n'
          '    finally:\n        os.chdir(original)\n')
    assert verify(shared).status == 'passed'
    (external / 'input.txt').write_text('changed')
    result = verify(shared)
    assert result.status == 'failed'
    assert 'unobserved' in result.reasons['tests/test_calc.py']
