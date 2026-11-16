"""Preparation reports live work and owns compiler descendants through failure."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from serverctl_pkg import native_engine as engines
from tests.support.owned_processes import wait_until_exited

pytestmark = pytest.mark.unit

# A child whose deadline is not under test gets the whole preparation budget,
# leaving a hang to the suite timeout: few-second private caps raced interpreter
# startup on a loaded host and decided outcomes. When a deadline is the subject,
# the spec expires the owner clock once its precondition is observable, and
# children publish readiness files atomically for that check.


def test_output_is_forwarded_before_child_exit(tmp_path, monkeypatch):
    acknowledgement = tmp_path / 'progress-seen'
    chunks = []

    class Output:
        def write(self, text):
            chunks.append(text)
            if 'compiling-marker' in ''.join(chunks):
                acknowledgement.touch()

        def flush(self):
            pass

    monkeypatch.setattr(sys, 'stderr', Output())
    program = (
        'import time; from pathlib import Path; '
        'print("compiling-marker", flush=True); '
        f'p = Path({str(acknowledgement)!r})\n'
        'while not p.exists(): time.sleep(0.01)\n'
        'print("build complete", flush=True)')
    result = engines._run([sys.executable, '-c', program], cwd=tmp_path,
                          environment=dict(os.environ), timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS,
                          progress='Compiling storage engine')
    assert 'build complete' in result
    assert 'build complete' in ''.join(chunks)


def test_silent_work_reports_elapsed_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(engines, 'PROGRESS_INTERVAL_SECONDS', 0.05)
    engines._run([sys.executable, '-c', 'import time; time.sleep(0.2)'],
                 cwd=tmp_path, environment=dict(os.environ), timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS,
                 progress='Checking storage engine')
    assert 'Checking storage engine' in capsys.readouterr().err


def test_timeout_keeps_last_diagnostic(tmp_path, monkeypatch):
    # Expire the owner clock after the diagnostic is observable; interpreter
    # startup under concurrent test load is not part of this contract.
    now = [0.0]

    def progress(text, **_kwargs):
        if 'waiting-for-linker' in text:
            now[0] = 1.0

    monkeypatch.setattr(engines, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(engines, '_report_progress', progress)
    program = 'import time; print("waiting-for-linker", flush=True); time.sleep(60)'
    with pytest.raises(engines.EnginePreparationError, match='timed out.*') as error:
        engines._run([sys.executable, '-c', program], cwd=tmp_path,
                     environment=dict(os.environ), timeout=1, progress='Preparing engine')
    assert 'waiting-for-linker' in str(error.value)


def test_successful_exit_cannot_bypass_log_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, 'MAX_LOG_BYTES', 128)
    with pytest.raises(engines.EnginePreparationError, match='log budget'):
        engines._run([sys.executable, '-c', 'print("x" * 1024)'], cwd=tmp_path,
                     environment=dict(os.environ), timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS)


def test_identity_output_stays_capture_only(tmp_path, capsys):
    result = engines._run([sys.executable, '-c', 'print("machine-readable")'],
                          cwd=tmp_path, environment=dict(os.environ),
                          timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS)
    assert result == 'machine-readable'
    assert capsys.readouterr().err == ''


def test_a_result_is_stdout_and_stderr_stays_diagnostics(tmp_path):
    program = ('import sys; print("warning: a library notice", file=sys.stderr, flush=True); '
               'print("machine-readable", flush=True); print("late notice", file=sys.stderr)')
    command = [sys.executable, '-c', program]
    options = dict(cwd=tmp_path, environment=dict(os.environ),
                   timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS)
    assert engines._run(command, **options) == 'machine-readable'
    output = engines._run_streams(command, **options)
    assert output.stdout == 'machine-readable'
    assert 'warning: a library notice\nlate notice' in output.stderr


def test_a_failure_report_keeps_both_streams(tmp_path):
    program = 'import sys; print("partial result"); print("error: denied lint", file=sys.stderr); sys.exit(1)'
    with pytest.raises(engines.EngineCommandFailedError, match='exit 1') as failed:
        engines._run([sys.executable, '-c', program], cwd=tmp_path, environment=dict(os.environ),
                     timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS)
    assert 'partial result' in str(failed.value)
    assert 'error: denied lint' in str(failed.value)


def test_relay_keeps_a_stderr_progress_frame_whole_beside_a_partial_stdout_line(tmp_path):
    frames = []
    frame = engines.PROGRESS_PREFIX + json.dumps({'phase': 'validating'}) + '\n'
    program = ('import sys; sys.stdout.write("partial "); sys.stdout.flush(); '
               f'sys.stderr.write({frame!r}); sys.stderr.flush(); print("line", flush=True)')
    with engines.observe_preparation(frames.append):
        result = engines._run([sys.executable, '-c', program], cwd=tmp_path, environment=dict(os.environ),
                              timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS,
                              progress='Validating storage engine')
    assert frames == [{'phase': 'validating'}]
    assert result == 'partial line'


def test_broken_progress_sink_does_not_break_preparation(tmp_path, monkeypatch):
    class ClosedOutput:
        def write(self, _):
            raise BrokenPipeError('consumer closed')

    monkeypatch.setattr(sys, 'stderr', ClosedOutput())
    result = engines._run([sys.executable, '-c', 'print("complete")'],
                          cwd=tmp_path, environment=dict(os.environ),
                          timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS,
                          progress='Compiling storage engine')
    assert result == 'complete'


def test_expired_preparation_does_not_spawn_more_work(tmp_path, monkeypatch):
    token = engines._deadline.set(time.monotonic() - 1)
    monkeypatch.setattr(engines.subprocess, 'Popen', lambda *a, **k: pytest.fail('deadline already expired'))
    try:
        with pytest.raises(engines.EnginePreparationError, match='timed out'):
            engines._run(['unused'], cwd=tmp_path, environment={}, timeout=3)
    finally:
        engines._deadline.reset(token)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file lock')
def test_contended_lock_reports_wait_and_honors_shared_deadline(tmp_path, capsys):
    path = tmp_path / 'prepare.lock'
    with engines._prepare_lock(path):
        token = engines._deadline.set(time.monotonic() + 0.1)
        try:
            with pytest.raises(engines.EnginePreparationError, match='Another storage engine update'):
                with engines._prepare_lock(path):
                    pytest.fail('must not acquire the competing lock')
        finally:
            engines._deadline.reset(token)
    assert 'Waiting for another storage engine preparation' in capsys.readouterr().err


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal cancellation')
@pytest.mark.parametrize('cancel_signal, expected_status', [(signal.SIGTERM, 1), (signal.SIGINT, 130)])
def test_worker_cancellation_reaps_compiler_and_returns_without_traceback(tmp_path, cancel_signal, expected_status):
    pid_file = tmp_path / 'compiler.pid'
    compiler = (
        'import os,signal,time; from pathlib import Path; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        f'p = Path({str(pid_file)!r}); t = p.with_name("compiler.tmp"); '
        't.write_text(str(os.getpid())); os.replace(t, p); time.sleep(3600)')
    worker = (
        'import os,sys; from pathlib import Path; '
        'from serverctl_pkg import native_engine as e; '
        f'e.prepare = lambda _, **_options: e._run([sys.executable, "-c", {compiler!r}], '
        f'cwd=Path({str(tmp_path)!r}), environment=dict(os.environ), '
        'timeout=e.PREPARATION_TOTAL_TIMEOUT_SECONDS, termination_grace=0.05); '
        f'sys.argv = ["prepare", "--project", {str(tmp_path)!r}]; sys.exit(e.main())')
    process = subprocess.Popen([sys.executable, '-c', worker],
                               cwd=Path(__file__).resolve().parents[2],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while not pid_file.exists() and process.poll() is None:
            time.sleep(0.01)
        assert pid_file.exists(), process.communicate()[1]
        process.send_signal(cancel_signal)
        _, stderr = process.communicate()
        assert process.returncode == expected_status
        assert b'cancelled' in stderr and b'Traceback' not in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass  # The worker has already reaped its compiler.


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group lifecycle')
def test_timeout_kills_descendant_even_when_group_leader_exits(tmp_path, monkeypatch):
    pid_file = tmp_path / 'stubborn.pid'
    descendant = (
        'import os,signal,time; from pathlib import Path; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        f'p = Path({str(pid_file)!r}); t = p.with_name("stubborn.tmp"); '
        't.write_text(str(os.getpid())); os.replace(t, p); time.sleep(3600)')
    parent = (
        'import subprocess,sys,time; '
        f'subprocess.Popen([sys.executable, "-c", {descendant!r}]); time.sleep(3600)')
    # The stage expires only once the stubborn descendant runs.
    monkeypatch.setattr(engines, 'time', SimpleNamespace(
        monotonic=lambda: 1.0 if pid_file.exists() else 0.0))
    try:
        with pytest.raises(engines.EnginePreparationError, match='timed out'):
            engines._run([sys.executable, '-c', parent], cwd=tmp_path,
                         environment=dict(os.environ), timeout=0.5)
        wait_until_exited(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass  # Already reaped by the operating system.


def test_structured_progress_crosses_child_boundary_without_raw_logs(tmp_path):
    frames = []
    previous_phase = engines._progress_phase.get()
    program = ('from serverctl_pkg.native_engine import _report_stage; '
               '_report_stage("compiling", "private compiler log")')
    with engines.observe_preparation(frames.append):
        engines._run([sys.executable, '-c', program], cwd=Path.cwd(),
                     environment=dict(os.environ), timeout=engines.PREPARATION_TOTAL_TIMEOUT_SECONDS,
                     progress='Preparing storage engine')
    assert frames == [{'phase': 'compiling'}]
    assert 'private' not in str(frames)
    assert engines._progress_observer.get() is None
    assert engines._progress_phase.get() == previous_phase


def test_progress_parser_accepts_split_frames_and_ignores_unstructured_output():
    frames = []
    with engines.observe_preparation(frames.append):
        reader = engines._progress_observer.get()
        reader('compiler details\nTOFU_ENGINE_PRO')
        reader('GRESS {"phase": "validating"}\n')
        reader('TOFU_ENGINE_PROGRESS malformed\n')
    assert frames == [{'phase': 'validating'}]


@pytest.mark.parametrize('reported, reason', [
    ('b' * 64 + ':1', 'source identity differs'),
    ('a' * 64 + ':0', 'allocator is incompatible'),
    ('invalid', 'invalid build metadata'),
])
def test_candidate_attestation_explains_metadata_refusal(tmp_path, monkeypatch, reported, reason):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    monkeypatch.setattr(engines.sys, 'platform', 'linux')
    monkeypatch.setattr(engines, 'build_identity', lambda _: {'source_sha256': 'a' * 64})
    monkeypatch.setattr(engines, '_run', lambda *_a, **_kw: reported)
    with pytest.raises(engines.EnginePreparationError, match=reason):
        engines._validate_candidate(candidate, tmp_path, {})


def test_candidate_attestation_preserves_loader_failure(tmp_path, monkeypatch):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    monkeypatch.setattr(engines, 'build_identity', lambda _: {'source_sha256': 'a' * 64})

    def load(*_args, **_kwargs):
        raise engines.EnginePreparationError('loader failed: cannot allocate memory in static TLS block')

    monkeypatch.setattr(engines, '_run', load)
    with pytest.raises(engines.EnginePreparationError, match='cannot allocate memory in static TLS block'):
        engines._validate_candidate(candidate, tmp_path, {})
    assert not engines._cached_library_matches_source(candidate, 'a' * 64)


def test_candidate_attestation_runs_semantic_probe_only_after_matching_pair(tmp_path, monkeypatch):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    monkeypatch.setattr(engines.sys, 'platform', 'linux')
    monkeypatch.setattr(engines, 'build_identity', lambda _: {'source_sha256': 'a' * 64})
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return 'a' * 64 + ':1' if len(calls) == 1 else ''

    monkeypatch.setattr(engines, '_run', run)
    engines._validate_candidate(candidate, tmp_path, {})
    assert len(calls) == 2
    assert calls[1][1:] == [str(tmp_path / 'scripts/check_tofudb_engine_drift.py'), '--binary', str(candidate), '--json']


def test_candidate_semantic_probe_spends_the_composed_preparation_budget(tmp_path, monkeypatch):
    # The probe cold-imports the storage client and rehashes source on the
    # source volume; a private per-stage cap refused healthy engines on a
    # loaded host while most of the composed restart budget remained.
    candidate = tmp_path / 'tofu-db'
    budgets = []

    def run(command, *, timeout, **_kwargs):
        budgets.append(timeout)
        return ''

    monkeypatch.setattr(engines, '_run', run)
    engines._validate_candidate(candidate, tmp_path, {})
    assert budgets == [engines.PREPARATION_TOTAL_TIMEOUT_SECONDS]


def _record_run_budgets(monkeypatch, output):
    budgets = []

    def run(command, *, timeout, **_kwargs):
        budgets.append(timeout)
        return output

    monkeypatch.setattr(engines, '_run', run)
    return budgets


def _attest(tmp_path):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    engines._verify_library_source(candidate, 'a' * 64)


def test_identity_and_attestation_stages_spend_the_composed_preparation_budget(tmp_path, monkeypatch):
    # Interpreter and engine start-up scale with source-volume load; a private
    # 10s stage cap refused a healthy engine pair with ~800s of budget left.
    monkeypatch.setattr(engines.sys, 'platform', 'linux')
    budgets = _record_run_budgets(monkeypatch, 'a' * 64 + ':1')
    with engines._preparation_budget(600):
        _attest(tmp_path)
    assert len(budgets) == 1 and 590 < budgets[0] <= 600


def test_identity_and_attestation_stages_keep_their_bound_outside_a_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(engines.sys, 'platform', 'linux')
    budgets = _record_run_budgets(monkeypatch, 'a' * 64 + ':1')
    _attest(tmp_path)
    assert budgets == [10]


def test_stage_inside_an_exhausted_preparation_is_refused(monkeypatch):
    with engines._preparation_budget(0):
        with pytest.raises(engines.EnginePreparationError, match='timed out'):
            engines.stage_seconds(10)


def test_publication_smoke_run_spends_the_composed_preparation_budget(tmp_path, monkeypatch):
    from scripts import deploy_tofudb_engine as deploy
    budgets = []

    def run(command, *, timeout, **_kwargs):
        budgets.append(timeout)
        return subprocess.CompletedProcess(command, 2, b'tofu-db usage', b'')

    monkeypatch.setattr(deploy.subprocess, 'run', run)
    deploy._smoke_check(tmp_path / 'tofu-db')
    with engines._preparation_budget(600):
        deploy._smoke_check(tmp_path / 'tofu-db')
    assert budgets[0] == deploy.SMOKE_TIMEOUT_S
    assert 590 < budgets[1] <= 600
