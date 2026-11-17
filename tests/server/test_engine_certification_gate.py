"""Restarts deploy only certified storage engines.

Executable specification of the engine certification gate
(docs/NATIVE_STORAGE_LIFECYCLE.md, "Engine certification gate"):

- HEAD's engine has a passing certification record -> exactly those bytes deploy;
- no record, unchanged storage contract -> the deployed engine is kept, plainly;
- no record, changed or unknown contract -> refused, nothing stopped or swapped;
- only an explicit, interactive, audited operator override builds and deploys
  an uncertified engine, for that one restart;
- automatic spawns (crash recovery, planned recovery, released leases) never
  prepare the engine at all, and preparation and admission share one rule.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from serverctl_pkg import engine_certification as certification
from serverctl_pkg import native_engine as engines
from tests.server import test_server_manager as existing
from tests.server.test_native_engine_update import binary

pytestmark = pytest.mark.unit
_scrub_ambient_server_env = existing._scrub_ambient_server_env
sm = existing.sm

DEST = 'packages/tofu-db/target/certified/tofu-db'
OVERRIDE = engines.UncertifiedEngineOverride(
    decision_id='d' * 32, operator='owner', reason='incident hotfix')
OVERRIDE_PAYLOAD = {'decisionId': 'd' * 32, 'operator': 'owner', 'reason': 'incident hotfix'}


def make_engine_tree(root: Path) -> Path:
    """A synthetic checkout: engine crate, storage contracts, manager files."""
    crate = root / 'packages/tofu-db'
    (crate / 'src').mkdir(parents=True)
    (crate / 'Cargo.toml').write_text(
        '[package]\nname = "tofu-db"\nversion = "0.2.0"\n\n'
        '[package.metadata.tofu]\nexternal-inputs = ["contracts/storage_operations_v1.json"]\n')
    for name in ('Cargo.lock', 'build.rs', 'src/lib.rs'):
        (crate / name).write_text(name + '\n')
    (root / 'contracts').mkdir(exist_ok=True)
    for name in ('storage_v2.json', 'native_storage_connection_v2.json', 'tofudb_ir_v1.json',
                 'storage_operations_v1.json'):
        (root / 'contracts' / name).write_text(json.dumps({'contract': name}))
    (root / '.env').write_text('TOFU_PERSONAL_STORAGE_BACKEND=tofudb\n')
    return root


def deploy(root: Path, candidate: Path, *, contract: str | None = None) -> Path:
    """An earlier deployment through the one deployment owner."""
    from scripts import deploy_tofudb_engine
    deploy_tofudb_engine.deploy(argparse.Namespace(
        binary=str(candidate), dest=str(root / DEST), evidence=None, note='earlier deployment',
        env_file=None, no_env_sync=True, no_smoke=False, dry_run=False),
        storage_contract_sha256=contract)
    return root / DEST


def certify(root: Path, candidate: Path, *, contract: str, failed_check: str | None = None) -> Path:
    """Lay out a record exactly as scripts/engine_certify.py publishes one."""
    identity = engines.build_identity(candidate)['source_sha256']
    directory = certification.record_directory(root, identity, contract)
    directory.mkdir(parents=True)
    shutil.copy2(candidate, directory / 'tofu-db')
    (directory / 'tofu-db').chmod(0o555)
    checks = [{'id': check, 'verdict': 'fail' if check == failed_check else 'pass'}
              for check in certification.REQUIRED_CHECKS]
    (directory / certification.RECORD_FILE).write_text(json.dumps({
        'format': certification.RECORD_FORMAT,
        'verdict': 'pass',
        'source_sha256': identity,
        'binary_sha256': hashlib.sha256((directory / 'tofu-db').read_bytes()).hexdigest(),
        'library_sha256': None,
        'storage_contract_sha256': contract,
        'commit': 'c0ffee' * 6 + 'c0ff',
        'certified_utc': '2026-11-12T03:00:00+00:00',
        'operator': {'user': 'owner'},
        'checks': checks,
    }))
    return directory


def change_engine_source(root: Path, text: str = 'uncertified engine change') -> str:
    (root / 'packages/tofu-db/src/lib.rs').write_text(text)
    return engines.source_fingerprint(root)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, '_validate_candidate', lambda *_: None)
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('a restart never downloads an uncertified engine'))
    monkeypatch.setattr(engines, '_build', lambda *_a, **_k: pytest.fail('a restart never compiles an uncertified engine'))
    root = existing._project(tmp_path)
    return make_engine_tree(root)


@pytest.fixture
def audit_records(tmp_path, monkeypatch):
    import lib.log
    directory = tmp_path / 'audit-logs'
    monkeypatch.setattr(lib.log, 'LOG_DIR', str(directory))
    monkeypatch.setattr(lib.log, 'AUDIT_LOG_FILE', str(directory / 'audit.log'))

    def records(event):
        path = directory / 'audit.log'
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row.get('event') == event]
    return records


def admit(root, dest, monkeypatch):
    import lib.storage.supervisor as supervisor
    monkeypatch.setattr(supervisor, '_tofudb_checkout_root', lambda: root)
    supervisor._advise_tofudb_engine_staleness(str(dest))


# ── 1. an uncertified HEAD keeps the deployed engine ──────────────────────────

@pytest.mark.parametrize('binding', ['deployment-receipt', 'certification-record'])
def test_uncertified_head_keeps_the_deployed_engine(project, binding, capsys, monkeypatch):
    deployed = engines.source_fingerprint(project)
    contract = engines.storage_contract_sha256(project)
    engine = binary(project / 'staging/tofu-db', deployed)
    if binding == 'deployment-receipt':
        dest = deploy(project, engine, contract=contract)
    else:
        dest = deploy(project, engine)  # a legacy receipt, certified afterwards
        certify(project, engine, contract=contract)
    before = dest.read_bytes()
    head = change_engine_source(project)

    assert engines.prepare(project, {'TOFUDB_BIN': str(dest)}) is False

    assert dest.read_bytes() == before
    output = capsys.readouterr().err
    assert f'engine at HEAD is uncertified: {head[:12]}' in output
    assert f'kept {deployed[:12]}' in output
    decision = engines.parse_engine_decision(output)
    assert decision['decision'] == 'kept'
    assert (decision['headSourceSha256'], decision['deployedSourceSha256']) == (head, deployed)
    assert decision['deployedStorageContractSha256'] == contract
    admit(project, dest, monkeypatch)  # one rule decides preparation and admission


# ── 2. a changed or unknown storage contract refuses ──────────────────────────

@pytest.mark.parametrize('deployed', ['changed-contract', 'changed-ir', 'legacy-receipt', 'nothing-deployed'])
def test_uncertified_head_is_refused_when_the_deployed_engine_cannot_serve_it(project, deployed, monkeypatch):
    engine = binary(project / 'staging/tofu-db', engines.source_fingerprint(project))
    dest = project / DEST
    if deployed == 'changed-contract':
        deploy(project, engine, contract=engines.storage_contract_sha256(project))
        (project / 'contracts/storage_v2.json').write_text('{"contract": "a new operation"}')
    elif deployed == 'changed-ir':
        deploy(project, engine, contract=engines.storage_contract_sha256(project))
        (project / 'contracts/tofudb_ir_v1.json').write_text('{"turn.get": "content_preview_chars"}')
    elif deployed == 'legacy-receipt':
        deploy(project, engine)
    before = dest.read_bytes() if dest.exists() else None
    head = change_engine_source(project)

    with pytest.raises(engines.UncertifiedEngineRefusedError) as refused:
        engines.prepare(project, {'TOFUDB_BIN': str(dest)})

    message = str(refused.value)
    assert f'engine at HEAD is uncertified: {head[:12]}' in message
    assert 'scripts/engine_certify.py --fixture' in message
    assert 'serverctl.py restart --deploy-uncertified-engine' in message
    assert refused.value.decision['decision'] == 'refused'
    assert (dest.read_bytes() if dest.exists() else None) == before
    if dest.exists():
        with pytest.raises(engines.EngineAdmissionRefusedError):
            admit(project, dest, monkeypatch)


def test_refusal_reaches_the_manager_as_a_typed_decision(project, monkeypatch):
    """The decision line survives the preflight child's failure report."""
    from serverctl_pkg import native_engine
    (project / 'serverctl.py').write_text('# preflight command\n')
    refusal = {'decision': 'refused', 'message': 'engine at HEAD is uncertified: abc; refused'}

    def failing_child(command, **_kwargs):
        raise native_engine.EngineCommandFailedError(
            'Worker artifact preparation failed (exit 1): progress noise\n'
            + native_engine.DECISION_PREFIX + json.dumps(refusal) + '\nFrontend preflight failed: ...')
    monkeypatch.setattr(native_engine, '_run_streams', failing_child)
    decision = {}
    ready, _error = existing.REAL_RUN_FRONTEND_PREFLIGHT(
        str(project), sys.executable, {}, 'test', parts='storage-engine', engine_decision=decision)
    assert ready is False
    assert decision == refusal


def test_a_kept_decision_reaches_the_manager_from_the_preflight_stderr(project):
    """A passing preflight reports its decision with progress on stderr, beside other notices."""
    kept = {'decision': 'kept', 'message': 'engine at HEAD is uncertified: abc; kept def'}
    (project / 'serverctl.py').write_text(
        'import sys\n'
        "print('/bin/sh: warning: setlocale: LC_ALL: cannot change locale (C.UTF-8)', file=sys.stderr)\n"
        f'print({engines.DECISION_PREFIX + json.dumps(kept)!r}, file=sys.stderr)\n'
        "print('Frontend artifact is ready.')\n")
    decision = {}
    assert existing.REAL_RUN_FRONTEND_PREFLIGHT(
        str(project), sys.executable, {}, 'test', parts='storage-engine', engine_decision=decision) == (True, '')
    assert decision == kept


def test_refused_engine_refuses_the_restart_and_never_retains_storage(project, monkeypatch):
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager._native_storage, 'retained_plan',
                        lambda: {'binary': str(project / DEST), 'binarySha256': 'e' * 64})
    monkeypatch.setattr(manager, '_retained_engine_refusal', lambda plan: '')
    refusal = {'decision': 'refused', 'message': 'engine at HEAD is uncertified: abc; next: certify'}

    def preflight(*_args, parts='all', engine_decision=None, **_kwargs):
        if parts == 'storage-engine':
            engine_decision.update(refusal)
            return False, 'Worker artifact preparation failed (exit 1): noise'
        return True, ''
    monkeypatch.setattr(sm, 'run_frontend_preflight', preflight)
    monkeypatch.setattr(manager, 'stop', lambda **_: pytest.fail('a refused engine keeps the serving worker'))

    result = manager.restart()

    assert result['ok'] is False
    assert result['message'] == refusal['message']
    assert result['engineDecision'] == refusal
    assert manager._state['storageRetention'] == {}


# ── 3. the operator override deploys and audits ───────────────────────────────

def test_operator_override_builds_deploys_and_audits_an_uncertified_engine(project, monkeypatch, audit_records):
    dest = deploy(project, binary(project / 'staging/tofu-db', engines.source_fingerprint(project)))
    head = change_engine_source(project)
    built = []

    def build(root, staging, environment, **_):
        built.append(root)
        return binary(staging / 'tofu-db', engines.source_fingerprint(root))
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: None)
    monkeypatch.setattr(engines, '_build', build)

    assert engines.prepare(project, {'TOFUDB_BIN': str(dest)}, uncertified_override=OVERRIDE) is True

    assert built == [project.resolve()]
    engines.verify_current(dest, project)
    receipt = json.loads((dest.parent / 'build.json').read_text())
    assert receipt['certification'] == (
        f'UNCERTIFIED operator override {OVERRIDE.decision_id} by owner: incident hotfix')
    assert receipt['storage_contract_sha256'] == engines.storage_contract_sha256(project)
    assert 'evidence' not in receipt
    [record] = audit_records('storage_engine.uncertified_deployed')
    assert record['decision_id'] == OVERRIDE.decision_id
    assert record['source_sha256'] == head
    assert record['binary_sha256'] == receipt['binary_sha256']


class _Terminal:
    def isatty(self):
        return True

    def fileno(self):
        return 0


def _restart_cli(monkeypatch, *argv):
    import serverctl
    monkeypatch.setattr(serverctl, '_remote_status', lambda: {'running': True})
    monkeypatch.setattr(serverctl, '_approve_restart_if_needed', lambda _args: True)
    monkeypatch.setattr(serverctl, 'prepare_source_frontend_artifact', lambda *_a, **_k: '')
    posted = []
    monkeypatch.setattr(serverctl, '_post', lambda action, **body: posted.append((action, body)) or {
        'ok': False, 'message': 'halted after the request was recorded'})
    args = serverctl.build_parser().parse_args(['restart', *argv])
    return serverctl.cmd_restart(args), posted


@pytest.mark.parametrize('token', [None, ('TOFU_RESTART_GATE_PASSED', '1'),
                                   ('TOFU_LIFECYCLE_GATE_PASSED', 'restart')])
def test_override_is_refused_without_an_interactive_terminal(monkeypatch, capsys, audit_records, token):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    if token:
        monkeypatch.setenv(*token)
    code, posted = _restart_cli(monkeypatch, '--deploy-uncertified-engine', 'incident hotfix')
    assert code == 2
    assert posted == []
    assert 'interactive terminal' in capsys.readouterr().err
    assert audit_records('storage_engine.uncertified_override') == []


def test_interactive_override_is_audited_and_forwarded_once(monkeypatch, audit_records):
    monkeypatch.setattr(sys, 'stdin', _Terminal())
    code, posted = _restart_cli(monkeypatch, '--deploy-uncertified-engine', 'incident hotfix')
    assert code == 1  # the stubbed manager refused; the request itself was well formed
    [(action, body)] = posted
    override = body['engineOverride']
    assert action == 'restart' and override['reason'] == 'incident hotfix'
    [record] = audit_records('storage_engine.uncertified_override')
    assert record['decision_id'] == override['decisionId']
    assert record['reason'] == 'incident hotfix'
    assert record['operator'] == override['operator']


def test_override_reaches_only_the_restart_it_was_given_to(project, monkeypatch):
    manager = sm.LifecycleManager(str(project))
    seen = []

    def preflight(*_args, parts='all', engine_override=None, engine_decision=None):
        seen.append((parts, engine_override))
        return False, 'halt before any stop'
    monkeypatch.setattr(sm, 'run_frontend_preflight', preflight)
    monkeypatch.setattr(manager._native_storage, 'retained_plan', lambda: {'binarySha256': 'e' * 64})
    monkeypatch.setattr(manager, '_retained_engine_refusal', lambda plan: '')
    monkeypatch.setattr(manager, 'stop', lambda **_: pytest.fail('a failed override never retains storage'))

    result = manager.restart(engine_override=OVERRIDE_PAYLOAD)
    manager._spawn('automatic-recovery')

    assert result['ok'] is False
    assert seen == [('storage-engine', OVERRIDE_PAYLOAD), ('frontend', None)]
    assert 'incident hotfix' not in json.dumps(manager._state)


@pytest.mark.parametrize('payload', ['deploy please', {'reason': ''}, {'decisionId': 'x', 'operator': 'o'}])
def test_malformed_override_is_refused_before_preparation(project, monkeypatch, payload):
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(sm, 'run_frontend_preflight', lambda *_a, **_k: pytest.fail('no preparation'))
    result = manager.restart(engine_override=payload)
    assert result['ok'] is False and 'override' in result['message']


# ── 4. a certified HEAD deploys exactly its recorded bytes ────────────────────

def test_certified_head_deploys_exactly_the_recorded_engine(project, capsys):
    dest = deploy(project, binary(project / 'staging/old/tofu-db', engines.source_fingerprint(project)))
    head = change_engine_source(project, 'certified engine change')
    record = certify(project, binary(project / 'staging/new/tofu-db', head),
                     contract=engines.storage_contract_sha256(project))

    assert engines.prepare(project, {'TOFUDB_BIN': str(dest)}) is True

    assert dest.read_bytes() == (record / 'tofu-db').read_bytes()
    receipt = json.loads((dest.parent / 'build.json').read_text())
    assert receipt['evidence'] == str(record / certification.RECORD_FILE)
    assert receipt['storage_contract_sha256'] == engines.storage_contract_sha256(project)
    assert receipt['certification'].startswith(f'certified engine {head[:12]}')
    assert engines.parse_engine_decision(capsys.readouterr().err)['decision'] == 'deployed-certified'
    assert engines.prepare(project, {'TOFUDB_BIN': str(dest)}) is False


@pytest.mark.parametrize('defect', ['tampered-binary', 'other-storage-contract', 'failed-check'])
def test_a_record_that_does_not_certify_head_is_never_deployed(project, defect):
    dest = deploy(project, binary(project / 'staging/old/tofu-db', engines.source_fingerprint(project)))
    before = dest.read_bytes()
    head = change_engine_source(project)
    contract = engines.storage_contract_sha256(project)
    record = certify(project, binary(project / 'staging/new/tofu-db', head),
                     contract='0' * 64 if defect == 'other-storage-contract' else contract,
                     failed_check='ff-native' if defect == 'failed-check' else None)
    if defect == 'tampered-binary':
        (record / 'tofu-db').chmod(0o755)
        (record / 'tofu-db').write_text((record / 'tofu-db').read_text() + '# changed\n')

    with pytest.raises(engines.UncertifiedEngineRefusedError):
        engines.prepare(project, {'TOFUDB_BIN': str(dest)})
    assert dest.read_bytes() == before


# ── 5. automatic restarts never prepare (build, download or swap) the engine ──

@pytest.mark.parametrize('source', ['automatic-recovery', 'storage-lease-released',
                                    'storage-maintenance-complete', 'planned-recovery'])
def test_automatic_spawns_never_prepare_the_storage_engine(project, monkeypatch, source):
    manager = sm.LifecycleManager(str(project))
    seen = []

    def preflight(*_args, parts='all', **_kwargs):
        seen.append(parts)
        return False, 'stop before spawning'
    monkeypatch.setattr(sm, 'run_frontend_preflight', preflight)
    manager._spawn(source)
    assert seen == ['frontend']


def test_explicit_start_prepares_the_storage_engine(project, monkeypatch):
    manager = sm.LifecycleManager(str(project))
    spawned = []
    monkeypatch.setattr(manager, '_port_conflict', lambda *_: (False, None))
    monkeypatch.setattr(manager, '_wait_for_stopped_sidecar_release', lambda: {'held': False})
    monkeypatch.setattr(manager, '_spawn', lambda source, **options: spawned.append(options) or {'ok': True})
    manager.start(source='cli')
    assert spawned == [{'reset_failed_storage': True, 'prepare_engine': True}]


# ── 5. a normal start certifies HEAD itself, or refuses before any stop ───────

def _git(root: Path, *argv: str) -> None:
    import subprocess
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                    '-c', 'commit.gpgsign=false', *argv], check=True, capture_output=True)


def _ir_only_change_after_deployment(project: Path, monkeypatch) -> tuple[Path, Path]:
    """The 2026-11-16 shape: deployed engine bound to the old contract, then an IR change lands."""
    (project / '.gitignore').write_text('packages/tofu-db/target/\nstaging/\ndata/\nlogs/\n')
    _git(project, 'init', '-q')
    _git(project, 'add', '-A')
    _git(project, 'commit', '-q', '-m', 'deployed generation')
    dest = deploy(project, binary(project / 'staging/old/tofu-db', engines.source_fingerprint(project)),
                  contract=engines.storage_contract_sha256(project))
    (project / 'contracts/tofudb_ir_v1.json').write_text('{"turn.get": "content_preview_chars"}')
    change_engine_source(project, 'regenerated IR operations')  # f87e54390c also changed the engine
    _git(project, 'commit', '-q', '-am', 'IR: turn.get content_preview_chars')
    authority = project / 'data/tofudb-authority'
    (authority / 'payload-segments').mkdir(parents=True)
    (authority / 'CONTROL').write_text('control')
    (authority / 'LOCK').write_text('')
    monkeypatch.setenv('TOFUDB_BIN', str(dest))
    monkeypatch.delenv('TOFU_PERSONAL_STORAGE_BACKEND', raising=False)
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    return dest, authority


def _gate(project: Path, ran: list):
    """scripts/engine_certify.py's effect: it publishes HEAD's record for the checkout's contract."""
    def run(root, commit, fixture, work_root):
        ran.append((commit, fixture))
        head = engines.source_fingerprint(root)
        certify(root, binary(root / 'staging/gate/tofu-db', head), contract=engines.storage_contract_sha256(root))
        return 0
    return run


def _serverctl(project: Path, monkeypatch, *, running: bool):
    import serverctl
    monkeypatch.setattr(serverctl, 'PROJECT', str(project))
    monkeypatch.setattr(serverctl, '_remote_status', lambda: {'running': running})
    monkeypatch.setattr(serverctl, '_approve_restart_if_needed', lambda _args: True)
    # The real preparation, in process instead of its fresh child interpreter.
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact',
                        lambda *_a, **_k: engines.prepare(project) and '')
    monkeypatch.setattr(serverctl, 'prepare_source_frontend_artifact', lambda *_a, **_k: '')
    monkeypatch.setattr(serverctl, '_StartupProgress', lambda *_a: argparse.Namespace(
        start=lambda: None, finish=lambda **_k: None))
    posted = []
    monkeypatch.setattr(serverctl, '_post', lambda action, **body: posted.append(action) or {
        'ok': False, 'message': 'stubbed manager'})
    return serverctl, posted


@pytest.mark.parametrize('door', ['start', 'restart'])
@pytest.mark.parametrize('cross_filesystem', [False, True])
def test_a_normal_start_certifies_and_deploys_heads_engine_after_an_ir_change(
        project, monkeypatch, door, cross_filesystem):
    dest, authority = _ir_only_change_after_deployment(project, monkeypatch)
    if door == 'restart':
        # The running server holds its store; the last record's intact fixture is used instead.
        import fcntl
        lease = (authority / 'LOCK').open('rb')
        fcntl.flock(lease, fcntl.LOCK_EX)
        fixture = project / 'staging/fixture'
        (fixture / 'payload-segments').mkdir(parents=True)
        (fixture / 'CONTROL').write_text('fixture control')
        old = certify(project, dest, contract='0' * 64)
        document = json.loads((old / certification.RECORD_FILE).read_text())
        document['fixture'] = {'path': str(fixture), 'control_sha256': certification.sha256_file(fixture / 'CONTROL')}
        (old / certification.RECORD_FILE).write_text(json.dumps(document))
    else:
        fixture = authority
    with pytest.raises(engines.UncertifiedEngineRefusedError):
        engines.prepare(project)  # what the start hit before: no record for HEAD's new contract
    ran = []
    managed_work = project / engines.MANAGED_ENGINE_ROOT / 'engine-certify-work'
    if cross_filesystem:
        # Model the production checkout mount separately from the recorded fixture.
        from types import SimpleNamespace
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            return SimpleNamespace(st_dev=result.st_dev + 1) if path == managed_work else result

        monkeypatch.setattr(Path, 'stat', stat)
    work_roots = []
    gate = _gate(project, ran)

    def run_gate(root, commit, selected_fixture, work_root):
        work_roots.append(work_root)
        assert selected_fixture == fixture
        assert work_root.stat().st_dev == fixture.stat().st_dev
        if cross_filesystem:
            assert work_root.parent == fixture.parent
            assert not work_root.is_relative_to(fixture)
        else:
            assert work_root == managed_work
        # Exercise the gate's actual filesystem requirement, not just its device check.
        link = work_root / 'control-link'
        link.hardlink_to(fixture / 'CONTROL')
        link.unlink()
        return gate(root, commit, fixture, work_root)

    monkeypatch.setattr(engines, '_run_engine_gate', run_gate)
    serverctl, posted = _serverctl(project, monkeypatch, running=door == 'restart')

    if door == 'start':
        serverctl.managed_start([])
    else:
        serverctl.cmd_restart(serverctl.build_parser().parse_args(['restart']))

    head = engines.source_fingerprint(project)
    contract = engines.storage_contract_sha256(project)
    assert ran == [(subprocess_head(project), fixture)]
    if cross_filesystem:
        assert not work_roots[0].exists()
    assert posted == [door]  # the manager is asked only after the engine is certified
    record, _problem = certification.passing_record(project, head, contract)
    assert record is not None
    if door == 'start':  # start prepares here; a restart's manager prepares the same record
        assert engines.build_identity(dest)['source_sha256'] == head
        assert json.loads((dest.parent / 'build.json').read_text())['storage_contract_sha256'] == contract
    assert engines.prepare(project) is (door == 'restart')
    engines.verify_admissible(dest, project)


@pytest.mark.parametrize('failure', ['gate', 'work-root'])
def test_cross_filesystem_certification_preserves_failure_and_fixture(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    root = tmp_path / 'checkout'
    fixture = tmp_path / 'snapshot' / 'fixture'
    fixture.mkdir(parents=True)
    control = fixture / 'CONTROL'
    control.write_text('unchanged fixture')
    managed = root / engines.MANAGED_ENGINE_ROOT / 'engine-certify-work'
    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        return SimpleNamespace(st_dev=result.st_dev + 1) if path == managed else result

    monkeypatch.setattr(Path, 'stat', stat)
    if failure == 'work-root':
        def denied(**kwargs):
            raise PermissionError('fixture parent is read-only')
        monkeypatch.setattr(engines.tempfile, 'mkdtemp', denied)
        with pytest.raises(engines.EnginePreparationError, match='nothing was stopped.*read-only'):
            with engines._certification_work_root(root, fixture):
                pytest.fail('no gate may run without a work root')
    else:
        with pytest.raises(RuntimeError, match='gate failed'):
            with engines._certification_work_root(root, fixture) as work:
                evidence = work / 'failed-run'
                evidence.mkdir()
                (evidence / 'report').write_text('failure evidence')
                raise RuntimeError('gate failed')
        assert (evidence / 'report').read_text() == 'failure evidence'
    assert control.read_text() == 'unchanged fixture'


def subprocess_head(root: Path) -> str:
    import subprocess
    return subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.parametrize('obstacle', ['store-in-use', 'uncommitted-ir', 'gate-fails'])
def test_impossible_automatic_certification_refuses_before_anything_is_stopped(
        project, monkeypatch, capsys, obstacle):
    dest, authority = _ir_only_change_after_deployment(project, monkeypatch)
    before = dest.read_bytes()
    ran = []
    if obstacle == 'store-in-use':
        import fcntl
        lease = (authority / 'LOCK').open('rb')
        fcntl.flock(lease, fcntl.LOCK_EX)
        assert lease  # held until the test ends
    elif obstacle == 'uncommitted-ir':
        (project / 'contracts/tofudb_ir_v1.json').write_text('{"turn.get": "uncommitted"}')
    monkeypatch.setattr(engines, '_run_engine_gate',
                        lambda *args: ran.append(args) or 1 if obstacle == 'gate-fails'
                        else pytest.fail('the gate never runs when its result could not certify HEAD'))
    serverctl, posted = _serverctl(project, monkeypatch, running=True)

    code = serverctl.cmd_restart(serverctl.build_parser().parse_args(['restart']))

    assert code == 1
    assert posted == []  # the manager was never asked, so the running server was not stopped
    assert dest.read_bytes() == before
    output = capsys.readouterr().err.splitlines()
    [line] = [line for line in output if line.startswith('Cannot restart Tofu')]
    assert output[-1] == line  # the refusal is one line and nothing follows it
    assert line.startswith("Cannot restart Tofu: Cannot certify HEAD's storage engine")
    assert 'nothing was stopped' in line
    expected = {'store-in-use': 'serverctl.py stop', 'uncommitted-ir': 'contracts/tofudb_ir_v1.json',
                'gate-fails': '--deploy-uncertified-engine'}[obstacle]
    assert expected in line
    assert len(ran) == (obstacle == 'gate-fails')


# ── shared contract identity and deployment receipts ──────────────────────────

def test_storage_contract_identity_covers_application_and_compiled_contracts(project):
    before = engines.storage_contract_sha256(project)
    (project / 'VERSION').write_text('9.9.9')
    (project / 'contracts/unrelated_v1.json').write_text('{}')
    assert engines.storage_contract_sha256(project) == before
    # tofudb_ir_v1.json: an engine kept across the turn.get content_preview_chars
    # IR change ignored the field and the assignment list failed (500, 2026-11-16).
    for name in ('storage_v2.json', 'native_storage_connection_v2.json', 'tofudb_ir_v1.json',
                 'storage_operations_v1.json'):
        (project / 'contracts' / name).write_text('{"changed": true}')
        assert engines.storage_contract_sha256(project) != before
        before = engines.storage_contract_sha256(project)


def test_storage_contract_identity_covers_engine_sources_generated_from_shared_contracts(project):
    """A contract the engine's generators read (Transaction IR, search, sync
    vocabulary, ...) is shared through the sources generated from it: what
    the engine compiles from it is part of the identity, its prose is not."""
    shared = project / 'contracts/shared_v1.json'
    shared.write_text('{"bound": 1, "description": "before"}')
    generated = project / 'packages/tofu-db/src/generated_shared.rs'
    generated.write_text('pub const BOUND: u64 = 1;\n')
    nested = project / 'packages/tofu-db/src/turn/tokens.generated.rs'
    nested.parent.mkdir()
    nested.write_text('pub const TOKENS: &[&str] = &["a"];\n')
    before = engines.storage_contract_sha256(project)

    shared.write_text('{"bound": 1, "description": "prose only"}')
    (project / 'packages/tofu-db/src/handwritten.rs').write_text('fn engine_only() {}\n')
    assert engines.storage_contract_sha256(project) == before

    for source, changed in ((generated, 'pub const BOUND: u64 = 2;\n'),
                            (nested, 'pub const TOKENS: &[&str] = &["a", "b"];\n')):
        source.write_text(changed)
        assert engines.storage_contract_sha256(project) != before
        before = engines.storage_contract_sha256(project)


def test_new_engine_bytes_never_inherit_the_previous_deployments_evidence(project):
    from scripts import deploy_tofudb_engine
    dest = project / DEST

    def install(candidate, **fields):
        namespace = argparse.Namespace(
            binary=str(candidate), dest=str(dest), evidence=fields.get('evidence'), note=fields.get('note'),
            env_file=None, no_env_sync=True, no_smoke=False, dry_run=False)
        deploy_tofudb_engine.deploy(namespace, storage_contract_sha256=fields.get('contract'))
        return json.loads((dest.parent / 'build.json').read_text())

    first = install(binary(project / 'staging/a/tofu-db', '1' * 64),
                    evidence='/tmp/evidence-for-a.json', note='certified a', contract='c' * 64)
    assert (first['evidence'], first['certification'], first['storage_contract_sha256']) == (
        '/tmp/evidence-for-a.json', 'certified a', 'c' * 64)
    repinned = install(dest)  # pin-only redeploy of the same bytes keeps their provenance
    assert repinned['evidence'] == '/tmp/evidence-for-a.json'
    assert repinned['storage_contract_sha256'] == 'c' * 64
    second = install(binary(project / 'staging/b/tofu-db', '2' * 64), note='automatic')
    assert 'evidence' not in second
    assert 'storage_contract_sha256' not in second
    assert second['certification'] == 'automatic'
