"""Landing verification: merged results that newly fail tests are regressions.

Every test builds a small repository under tmp_path with its own
``scripts/test_select.py`` (the selector the verification loads from the
merged tree) and real pytest tests, then verifies a worktree branch merged in
the private merge checkout (``verify``), or lands it through
``scripts/git_land.py``, which runs no tests (owner decision 2026-11-08).
Nothing touches the checkout running the suite.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.git_index_probe import stat_dirty_checkout

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/test_calc.py', 'tests/test_report.py', 'tests/test_other.py',
    'tests/test_flaky.py', 'tests/test_hang.py', 'tests/test_partial.py',
}
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'git_land.py'

SELECTOR = '''\
import ast, os, sys

def build_index(tests_dir, cache_path=None):
    index = {}
    for name in sorted(os.listdir(tests_dir)):
        if name.startswith('test_') and name.endswith('.py'):
            with open(os.path.join(tests_dir, name)) as handle:
                tree = ast.parse(handle.read())
            refs = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    refs.add(node.module.replace('.', '/') + '.py')
            index['tests/' + name] = {'refs': sorted(refs)}
    return index

BLAST_RADIUS = {}

def select_tests(index, changed):
    chosen = {test: 'direct' for test, record in index.items()
              if test in changed or set(record['refs']) & set(changed)}
    for path in changed:
        if BLAST_RADIUS.get(path) == 'ALL':
            for test in index:
                chosen.setdefault(test, 'blast:' + path)
    return set(chosen), chosen

def build_pytest_command(existing, jobs):
    return [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', *existing, '-q']
'''


def _load():
    spec = importlib.util.spec_from_file_location('git_land_verification_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


land = _load()


# Fixture repositories built in tmp_path, not the shipped tree.
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_more.py', 'tests/test_native.py'}


def test_relocated_toolchain_homes_reach_recorded_runs(tmp_path, monkeypatch):
    # Rust suites skip without Cargo; a rustup proxy also needs its toolchain home.
    from lib.project_mod import landing_verification

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('CARGO_HOME', str(tmp_path / 'cargo'))
    monkeypatch.setenv('RUSTUP_HOME', str(tmp_path / 'rustup'))
    environment = landing_verification._test_environment()
    assert environment['CARGO_HOME'] == str(tmp_path / 'cargo')
    assert environment['RUSTUP_HOME'] == str(tmp_path / 'rustup')
    assert str(tmp_path / 'cargo' / 'bin') in environment['PATH'].split(os.pathsep)


def test_unusable_impact_cache_keeps_static_selection_available(tmp_path, monkeypatch):
    from lib.project_mod import impact_selection, landing_verification

    monkeypatch.setattr(landing_verification, '_cache_dir', lambda top: tmp_path)
    monkeypatch.setattr(landing_verification, '_environment_key', lambda python, root=None: 'env')
    (tmp_path / 'impact-v5.sqlite').write_bytes(b'corrupt optional cache')

    store = landing_verification._store(tmp_path, sys.executable)
    assert store is None
    assert landing_verification._records(store) == {}

    class UnreadableCache:
        def records(self):
            raise impact_selection.ImpactCacheError('corrupt recorded evidence')

    assert landing_verification._records(UnreadableCache()) == {}


def test_warm_checkout_rejects_same_size_same_timestamp_stale_bytecode(shared, monkeypatch):
    from lib.project_mod import landing_verification as verification

    with verification.merge_checkout(shared, git(shared, 'rev-parse', 'HEAD').strip()) as warm:
        loaded = subprocess.run([sys.executable, '-c', 'from pkg.calc import scale; print(scale(2))'],
                                cwd=warm, capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(warm)})
        assert loaded.returncode == 0 and loaded.stdout.strip() == '4'
        stamp = (warm / 'pkg/calc.py').stat().st_mtime_ns
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(shared, 'same-size source change')
    original = verification._git

    def same_timestamp(root, *args):
        result = original(root, *args)
        if args[:3] == ('reset', '-q', '--hard'):
            os.utime(root / 'pkg/calc.py', ns=(stamp, stamp))
        return result

    monkeypatch.setattr(verification, '_git', same_timestamp)
    with verification.merge_checkout(shared, git(shared, 'rev-parse', 'HEAD').strip()) as warm:
        assert (warm / 'pkg/calc.py').read_text() == (shared / 'pkg/calc.py').read_text()
        loaded = subprocess.run([sys.executable, '-c', 'from pkg.calc import scale; print(scale(2))'],
                                cwd=warm, capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(warm)})
        assert loaded.returncode == 0 and loaded.stdout.strip() == '6'


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit(root: Path, message: str) -> None:
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', message)


def verify(shared: Path, branch: str, *, execute: bool = True):
    """Verify BRANCH merged onto the current branch tip in the private merge checkout."""
    from lib.project_mod import landing_verification as verification
    head = git(shared, 'rev-parse', 'HEAD').strip()
    source = git(shared, 'rev-parse', branch).strip()
    with verification.merge_checkout(shared, head) as scratch:
        verification.invalidate_bytecode(scratch, source)
        git(scratch, 'merge', '-q', '--no-ff', '--no-edit', source)
        tree = git(scratch, 'rev-parse', 'HEAD^{tree}').strip()
        return verification.verify_merge(shared, head, source, scratch, tree, execute=execute)


@pytest.fixture(autouse=True)
def private_local_root(tmp_path: Path, monkeypatch):
    """The warm merge checkout and test records stay inside this test's directory."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(tmp_path / 'local'))


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    repo = tmp_path / 'shared'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'trunk')
    git(repo, 'config', 'user.name', 'Lander')
    git(repo, 'config', 'user.email', 'lander@example.invalid')
    write(repo, '.gitignore', '__pycache__/\n')
    write(repo, 'scripts/test_select.py', SELECTOR)
    write(repo, 'pkg/calc.py', 'def scale(x):\n    return x * 2\n')
    write(repo, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n')
    commit(repo, 'base')
    return repo


@pytest.fixture()
def feature(shared: Path, tmp_path: Path) -> Path:
    path = tmp_path / 'feature'
    git(shared, 'worktree', 'add', '-q', '-b', 'feature', str(path))
    return path


def test_a_concurrent_trunk_change_that_breaks_the_branch_is_a_regression(shared, feature):
    write(feature, 'pkg/report.py',
          'from pkg.calc import scale\n\ndef report(x):\n    return str(scale(x))\n')
    write(feature, 'tests/test_report.py',
          'from pkg.report import report\n\ndef test_report():\n    assert report(2) == "4"\n')
    commit(feature, 'feat(report): render the scaled value')
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    write(shared, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 6\n')
    commit(shared, 'fix(calc): triple')

    verdict = verify(shared, 'feature')

    assert verdict.status == 'regressed'
    assert verdict.regressions == ('tests/test_report.py::test_report',)


def test_a_branch_that_breaks_a_concurrent_trunk_test_is_a_regression(shared, feature):
    write(shared, 'pkg/report.py',
          'from pkg.calc import scale\n\ndef report(x):\n    return str(scale(x))\n')
    write(shared, 'tests/test_report.py',
          'from pkg.calc import scale\nfrom pkg.report import report\n\n'
          'def test_report():\n    assert report(2) == str(scale(1) * 2)\n'
          '\ndef test_report_literal():\n    assert report(2) == "4"\n')
    commit(shared, 'feat(report): render the scaled value')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    write(feature, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 6\n')
    commit(feature, 'fix(calc): triple')

    verdict = verify(shared, 'feature')
    assert verdict.status == 'regressed'
    assert verdict.regressions == ('tests/test_report.py::test_report_literal',)


def test_failures_already_on_the_current_branch_do_not_block(shared, feature):
    write(shared, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n'
          '\ndef test_known_broken():\n    assert scale(0) == 1\n')
    commit(shared, 'test(calc): a known broken case')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(feature, 'refactor(calc): add instead of multiply')

    verdict = verify(shared, 'feature')
    assert verdict.status == 'passed'
    assert verdict.reason == 'only tests already failing on the current branch failed'
    assert verdict.preexisting == ('tests/test_calc.py::test_known_broken',)


def test_a_failure_that_does_not_reproduce_is_reported_not_blocking(
        shared, feature, tmp_path, monkeypatch):
    monkeypatch.setenv('LANDING_FLAKE_MARKER', str(tmp_path / 'flaked-once'))
    write(feature, 'tests/test_flaky.py',
          'import os, pathlib\nfrom pkg.calc import scale\n\n'
          'def test_flaky():\n'
          '    marker = pathlib.Path(os.environ["LANDING_FLAKE_MARKER"])\n'
          '    first = not marker.exists()\n'
          '    marker.write_text("seen")\n'
          '    assert not first and scale(1) == 2\n')
    commit(feature, 'test(calc): flaky case')
    write(shared, 'README.md', 'trunk moved\n')
    commit(shared, 'docs: readme')

    verdict = verify(shared, 'feature')
    assert verdict.status == 'passed'
    assert verdict.flaky == ('tests/test_flaky.py::test_flaky',)


@pytest.mark.parametrize('trunk_moved', [False, True], ids=['fast-forward', 'merge'])
def test_landing_runs_no_tests_even_when_the_merged_result_fails_them(
        shared, feature, monkeypatch, trunk_moved):
    """Owner decision 2026-11-08: the lane's own verification is the landing's evidence."""
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'fix(calc): triple without updating its test')
    tip = git(feature, 'rev-parse', 'HEAD').strip()
    if trunk_moved:
        write(shared, 'README.md', 'trunk moved\n')
        commit(shared, 'docs: readme')
    assert verify(shared, 'feature').regressions == ('tests/test_calc.py::test_scale',)
    from lib.project_mod import landing_verification

    def no_test_run(*_args, **_kwargs):
        pytest.fail('landing must not select or run tests')

    monkeypatch.setattr(landing_verification, 'verify_merge', no_test_run)
    monkeypatch.setattr(landing_verification, '_run', no_test_run)
    result = land.land('feature', into=str(shared))

    assert result['status'] == 'landed'
    assert 'verification' not in result
    assert git(shared, 'rev-parse', 'HEAD^2').strip() == tip
    assert git(shared, 'show', 'HEAD:pkg/calc.py') == 'def scale(x):\n    return x * 3\n'


def test_a_repository_without_a_selector_is_skipped_with_a_reason(shared, feature):
    git(shared, 'rm', '-q', 'scripts/test_select.py')
    commit(shared, 'chore: drop the selector')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'fix(calc): triple')

    verdict = verify(shared, 'feature')
    assert verdict.status == 'skipped'
    assert 'declares no test selector' in verdict.reason


def test_terminating_a_verification_retires_its_test_session(feature, tmp_path):
    import signal
    import time

    marker = tmp_path / 'hanging-test.pid'
    write(feature, 'tests/test_hang.py',
          'import os, pathlib, time\nfrom pkg.calc import scale\n\n'
          'def test_hang():\n'
          '    pathlib.Path(os.environ["LANDING_HANG_PID"]).write_text(str(os.getpid()))\n'
          '    time.sleep(600)\n')
    commit(feature, 'test(calc): a test that never finishes')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(PYTHONPATH=str(ROOT), LANDING_HANG_PID=str(marker))
    recorder = subprocess.Popen(
        [sys.executable, '-m', 'lib.project_mod.landing_verification', 'record',
         'tests/test_hang.py', '--into', str(feature)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while not marker.exists() and recorder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), 'the selected test never started'
        test_pid = int(marker.read_text())
        recorder.send_signal(signal.SIGTERM)
        assert recorder.wait(timeout=60) == 128 + signal.SIGTERM
    finally:
        if recorder.poll() is None:
            recorder.kill()
            recorder.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(test_pid, 0)


def _branch(shared: Path, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    git(shared, 'worktree', 'add', '-q', '-b', name, str(path))
    return path


def test_a_recorded_test_file_is_skipped_until_code_it_executed_changes(shared, tmp_path):
    write(shared, 'pkg/other.py', 'def unused():\n    return 1\n\n\ndef helper():\n    return 7\n')
    write(shared, 'tests/test_other.py',
          'from pkg.other import helper\n\ndef test_helper():\n    assert helper() == 7\n')
    commit(shared, 'test(other): helper')
    from lib.project_mod.landing_verification import record_tests
    assert record_tests(shared)['recorded'] == 2

    unused = _branch(shared, tmp_path, 'unused')
    write(unused, 'pkg/other.py', 'def unused():\n    return 2\n\n\ndef helper():\n    return 7\n')
    commit(unused, 'refactor(other): a function no test executes')
    skipped = verify(shared, 'unused')

    assert (skipped.status, skipped.selected, skipped.skipped_files) == ('passed', (), 2)
    assert 'no recorded test file used code or files the merge changes' in skipped.reason

    used = _branch(shared, tmp_path, 'used')
    write(used, 'pkg/other.py', 'def unused():\n    return 2\n\n\ndef helper():\n    return 8\n')
    commit(used, 'fix(other): helper returns eight')
    regressed = verify(shared, 'used')
    assert regressed.status == 'regressed'
    assert regressed.regressions == ('tests/test_other.py::test_helper',)


def test_a_failure_recorded_at_the_tip_is_preexisting_without_a_tip_run(
        shared, tmp_path, monkeypatch, capsys):
    write(shared, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n'
          '\ndef test_known_broken():\n    assert scale(0) == 1\n')
    commit(shared, 'test(calc): a known broken case')
    from lib.project_mod import landing_verification
    assert landing_verification.record_tests(shared)['failed'] == 1
    assert 'assert scale(0) == 1' in capsys.readouterr().err, (
        'failure diagnostics must survive retirement of the temporary record log')
    lane = _branch(shared, tmp_path, 'lane')
    write(lane, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(lane, 'refactor(calc): add instead of multiply')

    def no_tip_run(*_args, **_kwargs):
        raise AssertionError('the record at the tip decides; no tip checkout is needed')

    monkeypatch.setattr(landing_verification, '_tip_failures', no_tip_run)
    verdict = verify(shared, 'lane')

    assert verdict.status == 'passed'
    assert verdict.preexisting == ('tests/test_calc.py::test_known_broken',)


def test_incomplete_evidence_names_the_files_that_lack_it(shared):
    # The record log is retired with its temporary directory, so the result
    # itself must say which file to rerun with another storage lane or engine.
    write(shared, 'tests/test_partial.py',
          'import pytest\nfrom pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n'
          '\n@pytest.mark.skip(reason="needs a native engine")\ndef test_native():\n    pass\n')
    commit(shared, 'test(calc): a file with a skipped case')
    from lib.project_mod.landing_verification import record_tests

    result = record_tests(shared)

    assert (result['recorded'], result['tests'], result['failed']) == (1, 2, 0)
    assert result['error'].endswith(': tests/test_partial.py'), result['error']


_ENGINE_FINGERPRINT = 'a' * 64
_NATIVE_TEST = '''\
import os
import pytest

def test_native():
    engine = os.environ.get('TOFU_TEST_TOFUDB_BIN')
    if not engine:
        pytest.skip('Set TOFUDB_BIN to a source-matched native engine')
    assert engine == {expected!r}
'''


def _engines(monkeypatch, *binaries, matching=None):
    """Executable engines judged at the identity port: only MATCHING has the checkout's source."""
    from serverctl_pkg import native_engine
    for binary in binaries:
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o755)
    monkeypatch.setattr(native_engine, 'source_fingerprint', lambda root: _ENGINE_FINGERPRINT)
    monkeypatch.setattr(native_engine, 'build_identity', lambda binary: {'source_sha256': (
        _ENGINE_FINGERPRINT if matching is not None and Path(binary) == matching.resolve() else 'b' * 64)})
    monkeypatch.delenv('TOFU_TEST_TOFUDB_BIN', raising=False)


@pytest.mark.parametrize('built_by', ['launcher', 'repository'])
def test_a_recorded_run_admits_the_native_engine_built_from_its_checkout(
        shared, tmp_path, monkeypatch, built_by):
    """2026-11-13: every native-authority case skipped in recorded runs and its file stayed
    unrecorded. The private checkout builds no engine and the launcher's TOFUDB_BIN never
    reached it. The recorder admits the engine the suite's fixtures would: one whose source
    identity is the checkout's, from the launcher or from the repository's own builds."""
    from lib.project_mod.landing_verification import record_tests
    stale = tmp_path / 'stale' / 'tofu-db'
    matching = (tmp_path / 'launcher' / 'tofu-db' if built_by == 'launcher'
                else shared / 'packages/tofu-db/target/certified/tofu-db')
    write(shared, 'tests/test_native.py', _NATIVE_TEST.format(expected=str(matching.resolve())))
    commit(shared, 'test: a native-authority case')
    _engines(monkeypatch, stale, matching, matching=matching)
    monkeypatch.setenv('TOFUDB_BIN', str(matching if built_by == 'launcher' else stale))

    result = record_tests(shared)

    assert (result['recorded'], result['tests'], result['failed'], result['error']) == (2, 2, 0, ''), result
    assert result['native_engine_unavailable'] is None


def test_a_record_names_the_missing_native_engine_by_source_identity(shared, tmp_path, monkeypatch):
    """Without an engine built from the checkout its native cases cannot run: the record says
    so by source identity and names them, instead of a generic incomplete-evidence line."""
    from lib.project_mod.landing_verification import record_tests
    stale = tmp_path / 'stale' / 'tofu-db'
    write(shared, 'tests/test_native.py', _NATIVE_TEST.format(expected=''))
    commit(shared, 'test: a native-authority case')
    _engines(monkeypatch, stale)
    monkeypatch.setenv('TOFUDB_BIN', str(stale))

    result = record_tests(shared)

    assert (result['recorded'], result['tests'], result['failed']) == (1, 2, 0), result
    assert result['error'] == (f'native engine unavailable for {_ENGINE_FINGERPRINT}: '
                               'tests/test_native.py (1 case)'), result['error']
    assert result['native_engine_unavailable'] == {
        'fingerprint': _ENGINE_FINGERPRINT, 'cases': ['tests/test_native.py::test_native']}


def test_pending_recording_names_the_missing_native_engine_in_its_summary(shared, tmp_path, monkeypatch):
    """``record --pending`` summarised failed batches by group name only, so a missing engine
    stayed invisible there too."""
    from lib.project_mod.landing_verification import record_pending
    stale = tmp_path / 'stale' / 'tofu-db'
    write(shared, 'tests/test_native.py', _NATIVE_TEST.format(expected=''))
    commit(shared, 'test: a native-authority case')
    _engines(monkeypatch, stale)
    monkeypatch.setenv('TOFUDB_BIN', str(stale))

    result = record_pending(shared)

    assert (result['recorded'], result['tests']) == (1, 2), result
    assert result['error'].startswith(
        f'native engine unavailable for {_ENGINE_FINGERPRINT}: 1 case in 1 file; '), result['error']
    assert result['native_engine_unavailable'] == {
        'fingerprint': _ENGINE_FINGERPRINT, 'cases': ['tests/test_native.py::test_native']}


def test_the_launchers_engine_choice_is_not_an_evidence_key_input(monkeypatch):
    """Each run resolves its engine from the checkout's source identity, so the path a
    launcher named must not split the records into one partition per launcher."""
    from lib.project_mod import landing_verification as verification
    monkeypatch.delenv('TOFU_TEST_TOFUDB_BIN', raising=False)
    first = verification._test_environment()
    monkeypatch.setenv('TOFU_TEST_TOFUDB_BIN', '/a/lane/tofu-db')
    assert verification._test_environment() == first


def test_a_module_body_change_reruns_every_test_file_that_imported_it(shared, feature):
    from lib.project_mod.landing_verification import record_tests
    record_tests(shared)
    write(feature, 'pkg/calc.py', 'FACTOR = 3\n\n\ndef scale(x):\n    return x * 2\n')
    commit(feature, 'feat(calc): a module constant')

    verdict = verify(shared, 'feature')

    assert verdict.status == 'passed'
    assert verdict.selected == ('tests/test_calc.py',)


def test_the_merge_checkout_stays_warm_between_verifications(shared, feature, tmp_path):
    from lib.project_mod.landing_verification import warm_checkout_path
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(feature, 'refactor(calc): add')
    assert verify(shared, 'feature').status == 'passed'
    warm = warm_checkout_path(shared)
    bytecode = warm / 'pkg' / '__pycache__'
    assert bytecode.is_dir(), 'bytecode compiled by the first run stays for the next one'
    (warm / 'stray.txt').write_text('left by a test\n')

    second = _branch(shared, tmp_path, 'second')
    write(second, 'pkg/calc.py', 'def scale(x):\n    return 2 * x\n')
    commit(second, 'refactor(calc): commute')
    assert verify(shared, 'second').status == 'passed'
    assert bytecode.is_dir()
    assert not (warm / 'stray.txt').exists(), 'everything else is a fresh checkout'
    assert len([line for line in git(shared, 'worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ') and f'/{warm.parent.name}/' in line]) == 1


def test_tip_recheck_executes_only_the_failed_case(shared, feature, monkeypatch):
    """Passing siblings already ran on the merge; attribution needs only the failure."""
    write(shared, 'tests/test_calc.py',
          'import pytest\nfrom pkg.calc import scale\n'
          '\n@pytest.mark.parametrize("value", range(200))\n'
          'def test_scale(value):\n    assert scale(value) == value * 2\n'
          '\ndef test_known_broken():\n    assert scale(0) == 1\n')
    commit(shared, 'test(calc): a known broken case')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(feature, 'refactor(calc): add instead of multiply')

    from lib.project_mod import landing_verification
    ran_at_tip = []
    collected = {}
    original = landing_verification._run
    def spy(python, root, selector, tests, log):
        if root.name == 'tip-checkout':
            ran_at_tip.append(list(tests))
        result = original(python, root, selector, tests, log)
        collected[log.name] = len({node for line in result.observations
                                   for node in line.get('collected', ())})
        return result
    monkeypatch.setattr(landing_verification, '_run', spy)

    verdict = verify(shared, 'feature')
    assert verdict.preexisting == ('tests/test_calc.py::test_known_broken',)
    assert ran_at_tip == [['tests/test_calc.py::test_known_broken']], ran_at_tip
    assert collected == {'merged.log': 201, 'tip.log': 1}


def test_a_change_no_recorded_test_observes_runs_no_test_file(shared, feature):
    from lib.project_mod.landing_verification import record_tests
    record_tests(shared)
    write(feature, 'pkg/unrelated.py', 'def new_helper():\n    return 1\n')
    commit(feature, 'feat(pkg): a module no test file observes')

    verdict = verify(shared, 'feature')

    assert verdict.status == 'passed'
    assert verdict.selected == ()
    assert verdict.skipped_files == 1


def test_a_blast_radius_change_reruns_recorded_files_once(shared, tmp_path):
    write(shared, 'scripts/test_select.py',
          SELECTOR + "\nBLAST_RADIUS = {'settings.toml': 'ALL'}\n")
    write(shared, 'settings.toml', 'level = 1\n')
    write(shared, 'tests/test_other.py', 'def test_other():\n    assert True\n')
    commit(shared, 'test(select): settings are a global input')
    from lib.project_mod.landing_verification import record_tests
    assert record_tests(shared)['recorded'] == 2

    tuned = _branch(shared, tmp_path, 'tuned')
    write(tuned, 'settings.toml', 'level = 2\n')
    commit(tuned, 'chore: retune')
    first = verify(shared, 'tuned')

    assert first.selected == ('tests/test_calc.py', 'tests/test_other.py')
    assert first.recorded == 2
    git(shared, 'merge', '-q', '--no-ff', '--no-edit', 'tuned')
    notes = _branch(shared, tmp_path, 'notes')
    write(notes, 'README.md', 'notes\n')
    commit(notes, 'docs: notes')
    second = verify(shared, 'notes')
    assert (second.status, second.selected, second.skipped_files) == ('passed', (), 2), (
        "the first verification's run recorded the new settings; nothing depends on the notes")


def test_a_comment_in_a_blast_radius_file_escalates_nothing(shared, tmp_path):
    write(shared, 'scripts/test_select.py',
          SELECTOR + "\nBLAST_RADIUS = {'pkg/boot.py': 'ALL'}\n")
    write(shared, 'pkg/boot.py', 'READY = True\n')
    commit(shared, 'feat(pkg): boot settings are a global input')

    explained = _branch(shared, tmp_path, 'explained')
    write(explained, 'pkg/boot.py', '# Read once at start-up.\nREADY = True\n')
    commit(explained, 'docs(pkg): explain boot')
    commented = verify(shared, 'explained')
    assert (commented.status, commented.selected) == ('skipped', ())

    recoded = _branch(shared, tmp_path, 'recoded')
    write(recoded, 'pkg/boot.py', 'READY = False\n')
    commit(recoded, 'fix(pkg): boot is not ready')
    assert verify(shared, 'recoded').selected == ('tests/test_calc.py',)


def test_selection_without_execution_plans_the_affected_tests(shared, feature, monkeypatch):
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'fix(calc): triple without updating its test')
    from lib.project_mod import landing_verification
    ran = []
    monkeypatch.setattr(landing_verification, '_run', lambda *args: ran.append(args))

    verdict = verify(shared, 'feature', execute=False)

    assert (verdict.status, verdict.selected, verdict.unrecorded_files) == (
        'planned', ('tests/test_calc.py',), 1)
    assert ran == []


def test_the_command_line_previews_and_lands_without_running_tests(shared, feature, tmp_path):
    marker = tmp_path / 'test-ran'
    write(feature, 'tests/test_calc.py',
          'from pathlib import Path\nfrom pkg.calc import scale\n\n'
          f'def test_scale():\n    Path({str(marker)!r}).touch()\n'
          '    assert scale(2) == 4\n')
    commit(feature, 'test(calc): leave a marker when the test runs')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), 'feature', '--into', str(shared), *args],
                              cwd=shared, env=env, text=True, capture_output=True, timeout=120)

    checked = run('--check')
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout == 'feature merges cleanly into trunk: 1 paths would change.\n'
    landed = run()
    assert landed.returncode == 0, landed.stdout + landed.stderr
    assert landed.stdout.startswith('Landed feature on trunk: merge ')
    assert 'Integration check' not in landed.stdout
    assert not marker.exists()


def test_recording_pending_batches_converges_instead_of_restarting(shared, tmp_path):
    from lib.project_mod import landing_verification as verification

    first = verification.record_pending(shared)
    assert first['recorded'] == 1 and first['batches'] == 1 and first['error'] == ''
    second = verification.record_pending(shared)
    assert second['tests'] == 0 and second['already_fresh'] == 1, (
        'a file with current evidence is not run again')
    assert second['batches'] == 0

    # An interrupted batch loses only its own files, not the earlier ones.
    write(shared, 'tests/test_more.py', 'def test_more():\n    assert True\n')
    commit(shared, 'test: another file')
    third = verification.record_pending(shared)
    assert third['tests'] == 1, 'only the new file lacks evidence'
    assert third['already_fresh'] == 1, 'the first batch survived'
    assert third['recorded'] == 1, third
    assert verification.record_pending(shared)['tests'] == 0

    # Editing a file's own source invalidates just that file again.
    write(shared, 'tests/test_more.py', 'def test_more():\n    assert 1 + 1 == 2\n')
    commit(shared, 'test: change the assertion')
    fourth = verification.record_pending(shared)
    assert fourth['tests'] == 1 and fourth['already_fresh'] == 1, fourth


def test_pending_recording_refuses_paths_instead_of_dropping_them(monkeypatch, capsys):
    """`record --pending` covers every stale file in the repository; named paths
    must not be silently ignored (that started a whole-repository run)."""
    from lib.project_mod import landing_verification as verification

    def unexpected(*_args, **_kwargs):
        raise AssertionError('a refused command records nothing')

    monkeypatch.setattr(verification, 'record_pending', unexpected)
    monkeypatch.setattr(verification, 'record_tests', unexpected)
    with pytest.raises(SystemExit) as refused:
        verification.main(['record', '--pending', 'tests/test_calc.py', '--into', str(ROOT)])
    assert refused.value.code == 2
    assert '--pending' in capsys.readouterr().err


def test_the_test_environment_does_not_depend_on_the_launcher(monkeypatch):
    from lib.project_mod import landing_verification as verification

    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'conversation-specific')
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/a/lane')
    monkeypatch.setenv('TMPDIR', '/tmp/a-per-conversation-directory')
    monkeypatch.setenv('TOFUDB_BIN', '/a/lane/tofudb')
    monkeypatch.setenv('HTTP_PROXY', 'http://conversation-proxy')
    monkeypatch.setenv('LANG', 'en_US.UTF-8')
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    monkeypatch.delenv('USER', raising=False)
    first = verification._test_environment()
    monkeypatch.setenv('USER', 'someone-else')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'another-conversation')
    second = verification._test_environment()

    for name, value in first.items():
        if name not in ('USER', 'UV_TOOL_BIN_DIR', 'UV_TOOL_DIR'):
            assert second.get(name) == value, f'{name} followed the launcher'
    assert 'SANDBOX_857_TOKEN' not in first, 'credentials never reach the run'
    assert not any(name.startswith(('TOFU_', 'CODEX_', 'HTTP')) and
                   name not in verification._INHERITED for name in first), sorted(first)
    assert set(first) <= {'HOME', 'USER', 'LOGNAME', 'HOSTNAME', 'PATH', 'LANG', 'LC_ALL', 'TZ',
                          'VIRTUAL_ENV', *verification._INHERITED,
                          'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                          'UV_CACHE_DIR', 'UV_LINK_MODE', 'UV_PYTHON_INSTALL_DIR',
                          'UV_TOOL_BIN_DIR', 'UV_TOOL_DIR', 'PIP_CACHE_DIR', 'npm_config_cache'}
    assert first['LANG'] == first['LC_ALL'] == 'C.UTF-8' and first['TZ'] == 'UTC'
    assert first['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
    assert os.path.dirname(sys.executable) in first['PATH'].split(os.pathsep)
    monkeypatch.setenv('LANDING_FLAKE_MARKER', '/tmp/landing-scenario')
    assert verification._test_environment()['LANDING_FLAKE_MARKER'] == '/tmp/landing-scenario'


def test_the_blackbox_target_selectors_reach_the_tests_and_nothing_else(monkeypatch):
    """A black-box server is a test target, not launcher state: the harness
    uses TOFU_BLACKBOX_URL verbatim and boots no in-repo server when it is set,
    so verification forwards exactly the two selectors that name that server."""
    from lib.project_mod import landing_verification as verification

    for name in ('TOFU_BLACKBOX_URL', 'TOFU_BLACKBOX_REFERENCE_URL',
                 'TOFU_BLACKBOX_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    without = verification._test_environment()
    assert 'TOFU_BLACKBOX_URL' not in without
    assert 'TOFU_BLACKBOX_REFERENCE_URL' not in without

    monkeypatch.setenv('TOFU_BLACKBOX_URL', 'http://127.0.0.1:18080')
    monkeypatch.setenv('TOFU_BLACKBOX_REFERENCE_URL', 'http://127.0.0.1:18081')
    monkeypatch.setenv('TOFU_BLACKBOX_TOKEN', 'must-not-reach-the-tests')
    environment = verification._test_environment()

    assert environment['TOFU_BLACKBOX_URL'] == 'http://127.0.0.1:18080'
    assert environment['TOFU_BLACKBOX_REFERENCE_URL'] == 'http://127.0.0.1:18081'
    assert set(environment) - set(without) == {'TOFU_BLACKBOX_URL',
                                               'TOFU_BLACKBOX_REFERENCE_URL'}


def test_record_refuses_paths_with_pending_instead_of_dropping_them(shared, monkeypatch, capsys):
    """``--pending`` records every stale file of the repository. Paths given
    with it were dropped silently, so a three-file recording ran the whole
    repository's warm-up instead (2026-11-12)."""
    from lib.project_mod import landing_verification as verification

    calls = []
    outcome = {'recorded': 0, 'tests': 0, 'failed': 0, 'seconds': 0, 'head': '0' * 40,
               'error': ''}
    monkeypatch.setattr(verification, 'record_pending',
                        lambda top: calls.append('pending') or outcome)
    monkeypatch.setattr(verification, 'record_tests',
                        lambda top, paths: calls.append(list(paths)) or outcome)
    with pytest.raises(SystemExit) as refused:
        verification.main(['record', '--pending', '--into', str(shared), 'tests/test_calc.py'])
    assert refused.value.code == 2
    assert '--pending' in capsys.readouterr().err
    assert calls == []
    assert verification.main(['record', '--pending', '--into', str(shared)]) == 0
    assert verification.main(['record', '--into', str(shared), 'tests/test_calc.py']) == 0
    assert calls == ['pending', ['tests/test_calc.py']]


def test_only_recording_semantics_change_the_evidence_key(monkeypatch):
    from lib.project_mod import landing_verification as verification

    key = verification._environment_key(sys.executable)
    assert verification._environment_key(sys.executable) == key

    source = Path(verification.__file__).read_text()
    helpers = {node.name: ast.dump(node) for node in ast.parse(source).body
               if isinstance(node, ast.FunctionDef)}
    assert helpers
    monkeypatch.setattr(verification, '_record_semantics',
                        lambda: helpers['_remember'] + ' changed')
    assert verification._environment_key(sys.executable) != key

    owners = {parent.name for parent in ast.parse(source).body
              if isinstance(parent, ast.FunctionDef)
              and any(isinstance(child, ast.Attribute) and child.attr == 'records_from_observations'
                      for child in ast.walk(parent))}
    assert owners == {'_remember'}, 'a new producer of records must join the evidence key'


@pytest.mark.parametrize('writer_holds_lock', [False, True])
def test_snapshot_check_never_locks_the_checked_index(tmp_path, writer_holds_lock):
    """A run's snapshot check compares its input tree with the working tree.

    Porcelain ``diff <tree> --`` rewrites a stale index under ``index.lock``
    even with ``GIT_OPTIONAL_LOCKS=0`` (Git 2.34.1), so a stopped run would
    leave the lock behind. A lock another writer holds is honored.
    """
    from lib.project_mod import landing_verification as verification
    probe = stat_dirty_checkout(tmp_path / 'checkout', writer_holds_lock=writer_holds_lock)
    tree = git(probe.root, 'rev-parse', 'HEAD^{tree}').strip()
    run = verification._Run(True, (), 'ran\n')

    assert verification._check_snapshot(probe.root, tree, run) is run
    assert probe.unchanged()
    assert probe.lock.exists() is writer_holds_lock
