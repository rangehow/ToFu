"""Certify one commit's storage engine: GATE-PUBLISH-CARRY's engine gate as one command.

Entry: ``scripts/engine_certify.py`` (``main`` below). For one commit, in a
private detached checkout, it runs every check of
``serverctl_pkg.engine_certification.REQUIRED_CHECKS``:

  build                          the checkout's own restart build, persistent private build root
  certify                        ``native_engine --prepare-only --candidate``: identity, library, probes
  certify-no-online-background   the same with TOFU_DB_DISABLE_ONLINE_BACKGROUND=1
  cargo-lib / cargo-fastpath     ``cargo test --lib`` without and with ``fastpath::``
  ff-native                      the native pytest subset against the certified engine; one
                                 serial rerun of failed files only, both results kept
  load                           production-shaped load on a hard-link snapshot of --fixture,
                                 ended by a demonstrably clean stop
  rollback                       clean / crash during build / kill during shutdown sources,
                                 each served and verified by the candidate and every previous engine

and only when all passed publishes the write-once record restart preparation
deploys (``packages/tofu-db/target/engine-certifications/<source_sha256>/``).
Every run keeps its logs and verdicts in ``engine-certifications/runs/<run>/``.
The check set and criteria are GATE-PUBLISH-CARRY's, confirmed 2026-11-12;
docs/NATIVE_STORAGE_LIFECYCLE.md "Engine certification gate" owns the contract.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from serverctl_pkg import engine_certification as certification  # noqa: E402
from serverctl_pkg import native_engine  # noqa: E402
from serverctl_pkg.restart_budget import NATIVE_DAEMON_STOP_SECONDS  # noqa: E402

PACKAGE = Path(__file__).resolve().parent
# The gate's native pytest subset (GATE-PUBLISH-CARRY next_engine.sh, 2026-11-12).
FF_NATIVE_TESTS = (
    'tests/conversation/test_nontail_resume_no_loss.py', 'tests/conversation/test_conversation_commands_v3.py',
    'tests/turn_lifecycle', 'tests/storage_authority_sql/test_turns_delta_sync.py',
    'tests/project_mod/test_coordinator_session_control.py', 'tests/conversation/test_coordinator_session_control.py',
    'tests/api/test_api_v1_client_crash.py', 'tests/router', 'tests/model_routing', 'tests/api/test_api_v4_router.py',
    'tests/api/test_project_creation_router.py', 'tests/api/test_api_v4_session_model.py',
    'tests/tools/test_tool_exposure_policy.py', 'tests/tasks_pkg/test_approval_dialog_renders_risk.py',
    'tests/llm_dispatch', 'tests/error_envelope', 'tests/key_stats', 'tests/conversation/test_restart_resume.py',
    'tests/server/test_execution_lifespan.py', 'tests/project_mod/test_coordinator_restart_recovery.py',
    'tests/project_mod/test_coordinator_wake_durability.py', 'tests/project_mod/test_project_dispatch_recovery.py',
    'tests/project_mod/test_project_dispatch_wake.py', 'tests/project_mod/test_project_dispatch_repository.py',
    'tests/storage/test_project_dispatch_repository_segments.py',
    'tests/storage/test_project_dispatch_wake_contention.py', 'tests/storage/test_project_dispatch_segments.py',
    'tests/conversation/test_worker_exit_recovery.py', 'tests/project_mod/test_landing_verification.py',
    'tests/channels', 'tests/server/test_server_shutdown_lifecycle.py', 'tests/server/test_execution_composition.py',
    'tests/server/test_native_storage_service.py', 'tests/server/test_application_restart_control.py',
    'tests/storage/test_native_storage_connection.py', 'tests/server/test_server_manager.py',
    'tests/scheduler/test_scheduler_storage_backup.py', 'tests/storage/test_native_fastpath_auto.py',
    'tests/storage/test_storage_v2_client.py', 'tests/server/test_native_storage_host_plan.py',
)
FF_NATIVE_PYTEST_ARGS = ('-p', 'no:napari', '--timeout=900', '--tb=short', '-q', '-k', 'tofudb or native')
FF_NATIVE_WORKERS = '6'  # The gate's lane width; the serial rerun uses none.
# The gate's workload: seed 10% of the production census, then 1x and 3x peak, 300 s each.
LOAD_ARGS = ('--scales', '1,3', '--duration', '300', '--seed-scale', '0.1', '--run-id', 'certify')
SECRET_ENGINE_KEYS = ('TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256')


class CertificationError(RuntimeError):
    """The command cannot run as asked; nothing was certified."""


@dataclass
class Check:
    id: str
    verdict: str
    started_utc: str
    seconds: float
    argv: list = field(default_factory=list)
    rc: int | None = None
    log: dict | None = None
    summary: dict = field(default_factory=dict)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def snapshot_authority(source: Path, target: Path) -> None:
    """Copy an authority directory: hard-link immutable segments and blocks, copy what the engine rewrites."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in ('payload-segments', 'blocks'):
        if (source / name).is_dir():
            shutil.copytree(source / name, target / name, copy_function=os.link)
    for name in ('CONTROL', 'LOCK', 'payload-read-hints.cache'):
        if (source / name).exists():
            shutil.copy2(source / name, target / name)
    for wal in source.glob('*.wal'):
        shutil.copy2(wal, target / wal.name)


def clean_stop_verdict(log_text: str, wal_sizes: dict[str, int], seed_present: bool) -> tuple[bool, dict]:
    """The rollback drill's clean source must come from a completed ordered shutdown."""
    summary = {
        'stop_timed_out': 'child_exit state=timeout' in log_text,
        'final_checkpoint_done': any('"target":"shutdown_timing"' in line
                                     and 'step=authority_checkpoint state=done' in line
                                     for line in log_text.splitlines()),
        'wal_bytes': wal_sizes, 'seed_present': seed_present,
    }
    ok = (not summary['stop_timed_out'] and summary['final_checkpoint_done']
          and not any(wal_sizes.values()) and seed_present)
    return ok, summary


def _expected_rows(source: str) -> tuple[list, int]:
    """(December 2099 dates, November 2099 rows) a source's probe acknowledged."""
    if source == 'crash':
        return [f'2099-12-{index:02d}' for index in range(1, 4)], 0
    if source.startswith('killstop'):
        return [], 30
    return [], 0


def drill_verdict(probes: dict[str, dict], legs: dict[tuple[str, str], dict]) -> tuple[bool, dict]:
    """Every leg reads the same data and every acknowledged write; every probe landed where intended."""
    problems = []
    for name, result in probes.items():
        if result.get('rc') != 0:
            problems.append(f'probe {name} exited {result.get("rc")}')
        elif name == 'crash':
            if result.get('built_before_kill') is not False or [
                    commit.get('label') for commit in result.get('commits', [])] != [
                    'before-first-table', 'during-build-1', 'during-build-2']:
                problems.append('crash probe did not kill inside the packed-filter build after its 3 commits')
        elif (result.get('kill_step_never_reached') or len(result.get('commits', [])) != 30
                or not str(result.get('last_step') or '').startswith('step=authority_checkpoint state=start')):
            problems.append(f'probe {name} did not kill at step=authority_checkpoint after 30 commits')
    digests = set()
    for (source, engine), leg in sorted(legs.items()):
        label = f'{source}/{engine}'
        serve, verify = leg.get('serve') or {}, leg.get('verify') or {}
        december, november = _expected_rows(source)
        if serve.get('serve_error') or serve.get('exit') != 0 or leg.get('serve_rc') != 0:
            problems.append(f'{label}: serve failed ({serve.get("serve_error") or serve.get("exit")})')
        if serve.get('refusal_count') != 0 or not serve.get('write'):
            problems.append(f'{label}: {serve.get("refusal_count")} refused reads, write saved: {bool(serve.get("write"))}')
        if serve.get('december_2099') != december or serve.get('november_2099_rows') != november:
            problems.append(f'{label}: acknowledged writes differ (December {serve.get("december_2099")}, '
                            f'November rows {serve.get("november_2099_rows")})')
        if verify.get('rc') != 0 or verify.get('verdict') != 'ok' or verify.get('missing_blocks') != 0:
            problems.append(f'{label}: offline verify rc={verify.get("rc")} verdict={verify.get("verdict")} '
                            f'missing_blocks={verify.get("missing_blocks")}')
        digests.add(serve.get('sha256'))
    if len(digests) != 1 or None in digests:
        problems.append(f'legs disagree on the conversation digest: {sorted(map(str, digests))}')
    return not problems, {'problems': problems, 'legs': len(legs), 'sha256': sorted(map(str, digests))}


def parse_verify(text: str) -> dict:
    fields = {}
    for token in text.split():
        if '=' in token:
            key, value = token.split('=', 1)
            fields.setdefault(key, value)
    missing = fields.get('missing_blocks')
    return {'verdict': fields.get('verdict'),
            'missing_blocks': int(missing) if missing and missing.isdigit() else None}


def failed_test_files(junit: Path, checkout: Path) -> list[str]:
    """Test files with a failure or error in a pytest junit report."""
    files = set()
    for case in ElementTree.parse(junit).getroot().iter('testcase'):
        if case.find('failure') is None and case.find('error') is None:
            continue
        parts = (case.get('classname') or case.get('name') or '').split('.')
        while parts and not (checkout / ('/'.join(parts) + '.py')).is_file():
            parts.pop()
        if parts:
            files.add('/'.join(parts) + '.py')
    return sorted(files)


class Certification:
    def __init__(self, repo: Path, commit: str, fixture: Path, work_root: Path,
                 previous: list[Path], note: str, argv: list[str], *, cache_root: Path | None = None):
        self.repo, self.commit, self.fixture = repo, commit, fixture
        self.run_id = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{commit[:12]}'
        self.work = work_root / 'runs' / self.run_id
        # Compiler inputs need no hard links to the fixture. Keep their incremental
        # cache stable even when automatic certification selects a temporary work root.
        self.cache = cache_root if cache_root is not None else work_root / 'cache'
        self.evidence = certification.certifications_root(repo) / 'runs' / self.run_id
        self.checkout = self.work / 'checkout'
        self.previous_paths, self.note, self.argv = previous, note, argv
        self.checks: list[Check] = []
        # The detached checkout has no .env; resolve declared toolchain roots before spawning children.
        environment = {key: value for key, value in os.environ.items() if key not in SECRET_ENGINE_KEYS}
        self.environment = native_engine.resolve_cargo_source(environment, root=repo).environment

    # ── process plumbing ──────────────────────────────────────────────────
    def _run(self, check_id: str, argv: list, *, log_name: str | None = None, cwd: Path | None = None,
             environment: dict | None = None) -> tuple[int, Path]:
        log = self.evidence / (log_name or f'{check_id}.log')
        with log.open('ab') as sink:
            sink.write(f'# {_utc()} {json.dumps([str(arg) for arg in argv])}\n'.encode())
            sink.flush()
            child = subprocess.Popen([str(arg) for arg in argv], cwd=str(cwd or self.checkout),
                                     env=environment or self.environment, stdin=subprocess.DEVNULL,
                                     stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                return child.wait(), log
            except BaseException:  # Interrupted: retire the check's whole process group.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The group already exited.
                child.wait()
                raise

    def _record(self, check_id: str, started: float, started_utc: str, ok: bool, *, argv=(), rc=None,
                log: Path | None = None, **summary) -> Check:
        check = Check(check_id, 'pass' if ok else 'fail', started_utc, round(time.monotonic() - started, 1),
                      [str(arg) for arg in argv], rc,
                      {'path': str(log), 'sha256': certification.sha256_file(log)} if log and log.exists() else None,
                      summary)
        self.checks.append(check)
        print(f'[{check.verdict}] {check_id} ({check.seconds:.0f}s) {json.dumps(summary, default=str)[:400]}',
              flush=True)
        return check

    @property
    def python(self) -> Path:
        return self.checkout / '.venv/bin/python3'

    # ── checks ────────────────────────────────────────────────────────────
    def build(self) -> Path | None:
        started, at = time.monotonic(), _utc()
        out = self.work / 'build.json'
        argv = [self.python, PACKAGE / 'checkout_tasks.py', 'build', self.checkout, self.cache / 'build', out]
        rc, log = self._run('build', argv)
        result = json.loads(out.read_text()) if rc == 0 and out.exists() else {}
        ok = rc == 0 and result.get('build_info', {}).get('source_sha256') == result.get('source_sha256')
        self._record('build', started, at, ok, argv=argv, rc=rc, log=log,
                     compile_seconds=result.get('compile_seconds'), source_sha256=result.get('source_sha256'))
        return Path(result['binary']) if ok else None

    def certify(self, check_id: str, candidate: Path, output: Path, extra: dict) -> bool:
        started, at = time.monotonic(), _utc()
        argv = [self.python, '-m', 'serverctl_pkg.native_engine', '--project', self.checkout,
                '--prepare-only', output, '--candidate', candidate]
        rc, log = self._run(check_id, argv, environment={
            **self.environment, 'PYTHONPATH': str(self.checkout), **extra})
        return self._record(check_id, started, at, rc == 0, argv=argv, rc=rc, log=log, environment=extra,
                            output=str(output)).verdict == 'pass'

    def cargo(self) -> bool:
        cargo = native_engine.resolve_cargo_source(self.environment).cargo
        manifest = ['--manifest-path', 'packages/tofu-db/Cargo.toml', '--lib', '--locked']
        environment = {**self.environment, 'CARGO_TARGET_DIR': str(self.cache / 'cargo-test-target')}
        results = []
        for check_id, selection, private_tmp in (('cargo-lib', ['--skip', 'fastpath::'], True),
                                                 ('cargo-fastpath', ['fastpath::'], False)):
            started, at = time.monotonic(), _utc()
            if not cargo:
                results.append(self._record(check_id, started, at, False, missing='cargo').verdict)
                continue
            # The fsync-heavy specs get a private temporary directory on the work root (the fixture's local
            # filesystem), never tmpfs: in a cgroup without swap tmpfs pages are unreclaimable memory charged
            # to the container production runs in. fastpath:: keeps the default TMPDIR.
            scratch = self.work / f'tmp-{check_id}' if private_tmp else None
            argv = [cargo, 'test', *manifest, '--', *selection]
            try:
                if scratch:
                    scratch.mkdir(parents=True, exist_ok=True)
                rc, log = self._run(check_id, argv, environment={
                    **environment, **({'TMPDIR': str(scratch)} if scratch else {})})
            finally:
                if scratch:
                    shutil.rmtree(scratch, ignore_errors=True)
            results.append(self._record(check_id, started, at, rc == 0, argv=argv, rc=rc, log=log).verdict)
        return results == ['pass', 'pass']

    def ff_native(self, engine: Path) -> bool:
        started, at = time.monotonic(), _utc()
        present = [path for path in FF_NATIVE_TESTS if (self.checkout / path).exists()]
        missing = sorted(set(FF_NATIVE_TESTS) - set(present))
        first_junit = self.evidence / 'ff-native.xml'
        argv = [self.python, PACKAGE / 'checkout_tasks.py', 'lane', self.checkout, engine, first_junit,
                FF_NATIVE_WORKERS, *FF_NATIVE_PYTEST_ARGS, *present]
        rc, log = self._run('ff-native', argv)
        summary = {'missing_in_checkout': missing, 'first_rc': rc}
        if rc == 0:
            return self._record('ff-native', started, at, True, argv=argv, rc=rc, log=log, **summary).verdict == 'pass'
        failed = failed_test_files(first_junit, self.checkout) if first_junit.exists() else []
        summary['failed_files'] = failed
        if rc == 5 or not failed:  # nothing collected, or a failure no file owns: never rerun blindly
            return self._record('ff-native', started, at, False, argv=argv, rc=rc, log=log, **summary).verdict == 'pass'
        rerun_junit = self.evidence / 'ff-native-serial-rerun.xml'
        rerun_argv = [self.python, PACKAGE / 'checkout_tasks.py', 'lane', self.checkout, engine, rerun_junit,
                      '0', *FF_NATIVE_PYTEST_ARGS, *failed]
        rerun_rc, rerun_log = self._run('ff-native', rerun_argv, log_name='ff-native-serial-rerun.log')
        summary.update(rerun_rc=rerun_rc, rerun_log={'path': str(rerun_log),
                                                     'sha256': certification.sha256_file(rerun_log)},
                       classification=('load/timing flake: passed serially' if rerun_rc == 0
                                       else 'failed serially: a real failure'))
        return self._record('ff-native', started, at, rerun_rc == 0, argv=argv, rc=rc, log=log,
                            **summary).verdict == 'pass'

    def load(self, engine: Path) -> Path | None:
        started, at = time.monotonic(), _utc()
        root = self.work / 'load'
        authority = root / 'project/data/tofudb-authority'
        root.mkdir(mode=0o700, parents=True)
        (root / '.tofudb-load-test').touch()
        snapshot_authority(self.fixture, authority)
        argv = [self.python, PACKAGE / 'load_run.py', 'parent', str(NATIVE_DAEMON_STOP_SECONDS), self.checkout,
                '--root', root, '--engine', engine, *LOAD_ARGS]
        rc, log = self._run('load', argv, environment={**self.environment, 'PYTHONPATH': str(self.checkout)})
        wal = {path.name: path.stat().st_size for path in authority.glob('*.wal')}
        ok, summary = clean_stop_verdict(log.read_text(errors='replace'), wal, (root / 'seed.json').is_file())
        self._record('load', started, at, rc == 0 and ok, argv=argv, rc=rc, log=log, **summary)
        return root if rc == 0 and ok else None

    def rollback(self, candidate: Path, previous: dict[str, Path], load_root: Path) -> bool:
        started, at = time.monotonic(), _utc()
        drill = self.work / 'drill'
        drill.mkdir()
        engines = {'candidate': candidate, **previous}
        seed, clean = load_root / 'seed.json', load_root / 'project/data'
        probe_specs = [('crash', 'crash', candidate), ('killstop-candidate', 'killstop', candidate),
                       *((f'killstop-{label}', 'killstop', path) for label, path in previous.items())]

        def probe(spec):
            name, kind, engine = spec
            source = drill / f'source-{name}'
            snapshot_authority(clean / 'tofudb-authority', source / 'tofudb-authority')
            out = drill / f'probe-{name}.json'
            rc, _ = self._run('rollback', [self.python, PACKAGE / 'probes.py', kind, '--checkout', self.checkout,
                                           '--engine', engine, '--root', source, '--out', out],
                              log_name=f'rollback-probe-{name}.log')
            return name, {**(json.loads(out.read_text()) if out.exists() else {}), 'rc': rc}
        with ThreadPoolExecutor(max_workers=len(probe_specs)) as pool:
            probes = dict(pool.map(probe, probe_specs))
        sources = {'clean': clean, **{name: drill / f'source-{name}' for name in probes}}

        def leg(pair):
            source, label = pair
            root = drill / f'leg-{source}-{label}'
            snapshot_authority(sources[source] / 'tofudb-authority', root / 'tofudb-authority')
            out = drill / f'leg-{source}-{label}.json'
            serve_rc, _ = self._run('rollback', [
                self.python, PACKAGE / 'probes.py', 'serve', '--checkout', self.checkout, '--engine',
                engines[label], '--root', root, '--out', out, '--seed', seed],
                log_name=f'rollback-leg-{source}-{label}.log')
            verify_log = self.evidence / f'rollback-verify-{source}-{label}.txt'
            verify_rc, _ = self._run('rollback', [engines[label], 'verify', '--data-dir', root / 'tofudb-authority'],
                                     log_name=verify_log.name, cwd=root,
                                     environment={'PATH': '/usr/bin:/bin', 'HOME': str(root),
                                                  'TOFU_DB_WRITE_OVERLAY': '1'})
            shutil.rmtree(root / 'tofudb-authority', ignore_errors=True)
            return pair, {'serve_rc': serve_rc, 'serve': json.loads(out.read_text()) if out.exists() else {},
                          'verify': {'rc': verify_rc, **parse_verify(verify_log.read_text(errors='replace'))}}
        pairs = [(source, label) for source in sources for label in engines]
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            legs = dict(pool.map(leg, pairs))
        ok, summary = drill_verdict(probes, legs)
        summary['previous_engines'] = sorted(previous)
        (self.evidence / 'rollback.json').write_text(json.dumps(
            {'probes': probes, 'legs': {f'{s}/{e}': v for (s, e), v in legs.items()}}, indent=1, default=str))
        return self._record('rollback', started, at, ok, **summary).verdict == 'pass'

    # ── orchestration ─────────────────────────────────────────────────────
    def previous_engines(self, candidate_source: str) -> dict[str, Path]:
        """Private copies of the deployed engine, the last certified one and any explicit ones."""
        from tofu_dotenv import read_dotenv_values
        candidates = []
        deployed = native_engine.managed_engine_destination(
            self.repo, {**read_dotenv_values(self.repo / '.env'), **os.environ})
        if deployed.is_file():
            candidates.append(('deployed', deployed))
        records = []
        for directory in certification.record_directories(self.repo):
            try:
                records.append(certification.read_record(directory))
            except certification.CertificationRecordInvalid:
                continue
        records = [record for record in records if record.source_sha256 != candidate_source]
        if records:
            candidates.append(('last-certified', max(records, key=lambda record: record.certified_utc).binary))
        candidates.extend((f'previous-{index}', path) for index, path in enumerate(self.previous_paths, 1))
        copies, seen = {}, set()
        for label, path in candidates:
            digest = certification.sha256_file(path)
            if digest in seen:
                continue
            seen.add(digest)
            target = self.work / 'engines' / label / 'tofu-db'
            target.parent.mkdir(parents=True)
            shutil.copy2(path, target)
            if (path.parent / certification.LIBRARY_NAME).is_file():
                shutil.copy2(path.parent / certification.LIBRARY_NAME, target.parent / certification.LIBRARY_NAME)
            copies[label] = target
        return copies

    def prepare_checkout(self) -> None:
        self.work.mkdir(parents=True)
        self.evidence.mkdir(parents=True)
        added = subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(self.checkout),
                                self.commit], capture_output=True, text=True)
        if added.returncode:
            raise CertificationError(f'git worktree add failed: {added.stderr.strip()[:600]}')
        for name in ('.venv', 'node_modules'):  # Installed dependencies, as lanes link them.
            if (self.repo / name).exists():
                os.symlink(self.repo / name, self.checkout / name)

    def remove_checkout(self) -> None:
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'remove', '--force', str(self.checkout)],
                       capture_output=True, text=True)

    def run(self, source: str) -> Path | None:
        """Run every check; publish and return the record only when all passed."""
        try:
            contract = native_engine.storage_contract_sha256(self.checkout)
        except native_engine.EnginePreparationError as exc:
            raise CertificationError(f'commit {self.commit[:12]} has no storage contract identity: {exc}') from exc
        candidate = self.build()
        engine_dir = self.work / 'engine'
        passed = candidate is not None and self.certify('certify', candidate, engine_dir, {}) and self.certify(
            'certify-no-online-background', candidate, self.work / 'engine-no-online-background',
            {'TOFU_DB_DISABLE_ONLINE_BACKGROUND': '1'})
        if passed:
            engine = engine_dir / certification.BINARY_NAME
            previous = self.previous_engines(source)

            def load_and_drill():
                load_root = self.load(engine)
                return load_root is not None and self.rollback(engine, previous, load_root)
            with ThreadPoolExecutor(max_workers=3) as pool:  # three independent branches of the gate
                branches = [pool.submit(self.cargo), pool.submit(self.ff_native, engine),
                            pool.submit(load_and_drill)]
                passed = all([branch.result() for branch in branches])
        verdicts = {check.id: check.verdict for check in self.checks}
        passed = passed and all(verdicts.get(check) == 'pass' for check in certification.REQUIRED_CHECKS)
        document = self.document(source, contract, passed, engine_dir)
        (self.evidence / 'result.json').write_text(json.dumps(document, indent=1, sort_keys=True))
        return publish_record(self.repo, engine_dir, document, self.run_id) if passed else None

    def document(self, source: str, contract: str, passed: bool, engine_dir: Path) -> dict:
        return {
            'format': certification.RECORD_FORMAT, 'verdict': 'pass' if passed else 'fail',
            'source_sha256': source, 'commit': self.commit, 'storage_contract_sha256': contract,
            'build_info': (native_engine.build_identity(engine_dir / certification.BINARY_NAME)
                           if (engine_dir / certification.BINARY_NAME).is_file() else None),
            'previous_engines': {path.parent.name: {'path': str(path),
                                                    'binary_sha256': certification.sha256_file(path)}
                                 for path in sorted((self.work / 'engines').glob('*/tofu-db'))},
            'fixture': {'path': str(self.fixture),
                        'control_sha256': certification.sha256_file(self.fixture / 'CONTROL')},
            'checks': [asdict(check) for check in self.checks],
            'certified_utc': _utc(),
            'operator': {'user': getpass.getuser(), 'host': socket.gethostname(), 'pid': os.getpid(),
                         'argv': self.argv, 'note': self.note},
            'run': {'id': self.run_id, 'evidence': str(self.evidence), 'work': str(self.work)},
        }


def publish_record(repo: Path, engine_dir: Path, document: dict, run_id: str) -> Path:
    """Publish the write-once record by one atomic rename; the reader must accept it first."""
    final = certification.record_directory(
        repo, document['source_sha256'], document['storage_contract_sha256'])
    if final.exists():
        raise CertificationError(f'a certification record already exists at {final}; records are never replaced')
    staging_root = certification.certifications_root(repo) / f'.staging-{run_id}'
    staging = staging_root / final.parent.name / final.name
    staging.mkdir(parents=True)
    for name in (certification.BINARY_NAME, certification.LIBRARY_NAME, 'build.json',
                 certification.BINARY_NAME + '.sha256'):
        if (engine_dir / name).is_file():
            shutil.copy2(engine_dir / name, staging / name)
    for name in (certification.BINARY_NAME, certification.LIBRARY_NAME):
        if (staging / name).is_file():
            (staging / name).chmod(0o555)
    library = staging / certification.LIBRARY_NAME
    document = {**document,
                'binary_sha256': certification.sha256_file(staging / certification.BINARY_NAME),
                'library_sha256': certification.sha256_file(library) if library.is_file() else None}
    (staging / certification.RECORD_FILE).write_text(json.dumps(document, indent=1, sort_keys=True))
    certification.read_record(staging)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.rename(staging, final)
    staging.parent.rmdir()
    staging_root.rmdir()
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Certify one commit\'s storage engine (the engine gate) and '
                                                 'publish the record restart preparation deploys.')
    parser.add_argument('--fixture', required=True, type=Path,
                        help='settled TofuDB authority directory to load-test on (read only; hard-link snapshots)')
    parser.add_argument('--commit', default='HEAD', help='commit to certify (default: HEAD)')
    parser.add_argument('--previous-engine', action='append', type=Path, default=[],
                        help='also drill rollback against this engine (repeatable); the deployed and the last '
                             'certified engines are always included')
    parser.add_argument('--work-root', type=Path,
                        help="private work directory on the fixture's filesystem "
                             "(default: this repository's lane root)")
    parser.add_argument('--note', default='', help='operator note recorded with the certification')
    args = parser.parse_args(argv)
    try:
        certification_run = _prepare(args, argv if argv is not None else sys.argv[1:])
    except CertificationError as exc:
        print(f'engine_certify: refused: {exc}', file=sys.stderr)
        return 2
    try:
        certification_run.prepare_checkout()
        source = native_engine.source_fingerprint(certification_run.checkout)
        contract = native_engine.storage_contract_sha256(certification_run.checkout)
        existing, problem = certification.passing_record(certification_run.repo, source, contract)
        if existing is not None:
            print(f'engine_certify: {existing.summary()} already exists: {existing.directory}')
            return 0
        if certification.record_directory(certification_run.repo, source, contract).exists():
            raise CertificationError(f'{problem}; move that record aside to certify this source again')
        record = certification_run.run(source)
    except (CertificationError, native_engine.EnginePreparationError) as exc:
        print(f'engine_certify: refused: {exc}', file=sys.stderr)
        return 2
    finally:
        certification_run.remove_checkout()
    if record is None:
        failed = [check.id for check in certification_run.checks if check.verdict != 'pass']
        print(f'engine_certify: NOT certified (failed: {", ".join(failed) or "incomplete"}); nothing was '
              f'recorded. Evidence: {certification_run.evidence}; work data: {certification_run.work}',
              file=sys.stderr)
        return 1
    shutil.rmtree(certification_run.work, ignore_errors=True)
    print(f'engine_certify: certified engine {record.name[:12]} (commit {certification_run.commit[:12]}): {record}')
    return 0


def _prepare(args, argv: list[str]) -> Certification:
    from lib.project_mod import lanes
    repo = lanes.repository(REPO_ROOT).top
    resolved = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', f'{args.commit}^{{commit}}'],
                              capture_output=True, text=True)
    if resolved.returncode:
        raise CertificationError(f'unknown commit {args.commit!r}: {resolved.stderr.strip()[:300]}')
    fixture = args.fixture.expanduser().resolve()
    if not (fixture / 'CONTROL').is_file() or not (fixture / 'payload-segments').is_dir():
        raise CertificationError(f'{fixture} is not a settled TofuDB authority (CONTROL, payload-segments/)')
    local = lanes.local_root(lanes.repository(repo))
    work_root = (args.work_root or (local / 'engine-certify' if local else None))
    if work_root is None:
        raise CertificationError('no local work root is available; pass --work-root on the fixture\'s filesystem')
    work_root = work_root.expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    if work_root.stat().st_dev != fixture.stat().st_dev:
        raise CertificationError(
            f'{work_root} and the fixture {fixture} are on different filesystems; snapshots hard-link the '
            'fixture instead of copying it, so pass --work-root on the fixture\'s filesystem')
    for path in args.previous_engine:
        if not path.expanduser().is_file():
            raise CertificationError(f'--previous-engine {path} is not a file')
    return Certification(repo, resolved.stdout.strip(), fixture, work_root,
                         [path.expanduser().resolve() for path in args.previous_engine], args.note, argv,
                         cache_root=local / 'engine-certify/cache' if local else None)
