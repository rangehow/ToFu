"""Native storage policy on the shared resident-process lifecycle owner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from serverctl_pkg.restart_budget import (
    NATIVE_GENERATION_PROBE_SECONDS, NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS,
)
from serverctl_pkg import resident_service as resident
from serverctl_pkg.resident_service import (
    ResidentService, acquire_service_lock, process_matches, write_record,
)

HOST_MODULE = 'serverctl_pkg.native_storage_host'
RECORD_FORMAT = 'tofu.native-storage-service/v1'
# Set by the manager on application processes when it kept the serving host
# across their restart: they attach to the engine that host admitted (this
# digest) instead of admitting the checkout's current engine artifact.
RETAINED_ENGINE_ENVIRONMENT = 'TOFU_STORAGE_RETAINED_ENGINE_SHA256'

__all__ = ['NativeStorageService', 'acquire_service_lock', 'process_matches',
           'write_record', 'read_record', 'host_matches', 'serves_web_owner',
           'failed_host_verdict', 'RETAINED_ENGINE_ENVIRONMENT']


def read_record(path: Path) -> dict:
    return resident.read_record(path, record_format=RECORD_FORMAT)


def host_matches(record: dict) -> bool:
    return resident.host_matches(record, host_module=HOST_MODULE)


def serves_web_owner(state_path: Path, service_id: str, host_pid: int, web_pid: int) -> bool:
    """Fence native respawn against a Web owner of another service/project."""
    record = read_record(state_path)
    if (record.get('serviceId') != service_id or record.get('pid') != host_pid
            or not host_matches(record)):
        return False
    if not process_matches(record.get('managerPid'), record.get('managerStartTime')):
        return False
    try:
        raw = Path(f'/proc/{web_pid}/environ').read_bytes()
        env = dict(part.split(b'=', 1) for part in raw.split(b'\0') if b'=' in part)
        return (env.get(b'TOFU_STORAGE_CONNECTION_FILE') == record['connectionFile'].encode()
                and env.get(b'TOFU_PROJECT_PATH') == record['projectPath'].encode())
    except (OSError, KeyError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def failed_host_verdict(project: str) -> dict | None:
    """The resident host's own terminal verdict for ``project``, or None.

    An attached application process never latches: its attachment keeps
    retrying after the host's restart breaker stopped recovering. Only this
    record says that storage needs an explicit restart.
    """
    record = NativeStorageService(project, sys.executable).record()
    if record.get('state') != 'failed':
        return None
    return {'state': 'failed', 'error': str(record.get('error') or '')[:300]}


class NativeStorageService(ResidentService):
    service_name = 'native storage'
    state_name = 'native-storage-service'
    record_format = RECORD_FORMAT
    host_module = HOST_MODULE

    def __init__(self, project: str, python: str):
        super().__init__(project, python)
        self.connection_path = self.project / 'data/native-storage-connection.json'

    def identity_fields(self) -> dict:
        return {'connectionFile': str(self.connection_path)}

    def result_path(self) -> str:
        return str(self.connection_path)

    def host_matches(self, record: dict) -> bool:
        return host_matches(record)

    def process_environment(self, environment: dict[str, str]) -> dict[str, str]:
        env = dict(environment)
        env.pop('TOFU_STORAGE_CONNECTION_FILE', None)
        return env

    def shared_environment(self, environment: dict[str, str]) -> dict[str, str]:
        """Independent managed clients need the selected authority's RPC owner.

        This changes only automatic native transport selection. Explicit
        embedded topology and the configured storage backend remain intact.
        """
        env = dict(environment)
        if (env.get('TOFUDB_TRANSPORT') or 'auto').strip().lower() == 'auto':
            env['TOFUDB_TRANSPORT'] = 'daemon'
        return env

    def stop(self, timeout: float = NATIVE_SERVICE_STOP_SECONDS, *,
             retire_failed_close: bool = False) -> str:
        return super().stop(timeout, retire_failed_close=retire_failed_close)

    def prepare(self, environment: dict[str, str]) -> dict | None:
        # Other projects and explicitly supplied co-containers keep their owner.
        if (not sys.platform.startswith('linux') or environment.get('TOFU_STORAGE_CONNECTION_FILE')
                or not (self.project / 'contracts/native_storage_connection_v2.json').is_file()):
            return None
        from serverctl_pkg.native_engine import _run
        # _run owns bounded descendant cancellation and a combined diagnostic
        # stream. Keep the structured result separate from those diagnostics.
        with tempfile.TemporaryDirectory(prefix='tofu-native-plan-') as directory:
            result = Path(directory) / 'plan.json'
            _run([self.python, '-m', self.host_module, '--plan', '--plan-output', str(result)],
                 cwd=self.project, environment=environment, timeout=NATIVE_SERVICE_PREPARATION_SECONDS)
            plan = json.loads(result.read_text(encoding='utf-8'))
        return plan or None

    def retained_plan(self) -> dict | None:
        """This manager's ready host's own plan, or None when it cannot be kept.

        Retention skips storage preparation entirely, so an engine that fails
        to build (or was deliberately not rebuilt) never blocks replacing the
        application processes, and the authority is not reopened.
        """
        record = self.record()
        if (record.get('state') != 'ready' or not isinstance(record.get('plan'), dict)
                or not host_matches(record) or record.get('managerPid') != os.getpid()
                or not process_matches(record['managerPid'], record.get('managerStartTime'))):
            return None
        return dict(record['plan'])

    def probe_generation(self, plan: dict | None, environment: dict[str, str]) -> None:
        """Start a new engine on a disposable authority, health handshake included, before drain.

        The live authority stays leased by the serving host, so this is the
        closest pre-shutdown proof that the new generation's engine starts and
        answers. Raises when it does not; the serving worker is then retained.
        """
        serving = self.record().get('plan') or {}
        if (not plan or not plan.get('binary')
                or plan.get('binarySha256') == serving.get('binarySha256')):
            return
        from serverctl_pkg.native_engine import _run
        _run([self.python, str(self.project / 'scripts/check_tofudb_engine_drift.py'),
              '--binary', str(plan['binary']), '--json'], cwd=self.project,
             environment=environment, timeout=NATIVE_GENERATION_PROBE_SECONDS,
             progress='Starting the new storage engine on a disposable authority')

    # --- last known-good engine (docs/NATIVE_STORAGE_LIFECYCLE.md,
    # "Rolling back a generation that never served") ---------------------

    def known_good_directory(self) -> Path:
        return self.project / 'data' / 'native-storage-known-good'

    def engine_contract(self, plan: dict) -> str:
        """The storage contract the engine of ``plan`` is bound to for this source, or ''."""
        from serverctl_pkg.native_engine import retention_refusal, storage_contract_sha256
        if retention_refusal(self.project, plan):
            return ''
        return storage_contract_sha256(self.project)

    def current_contract(self) -> str:
        from serverctl_pkg.native_engine import storage_contract_sha256
        return storage_contract_sha256(self.project)

    def deploy_engine(self, copy: Path, dest: Path, contract: str, note: str) -> None:
        """Publish ``copy`` at ``dest`` through the one supported engine deployment owner."""
        from scripts import deploy_tofudb_engine
        deploy_tofudb_engine.deploy(argparse.Namespace(
            binary=str(copy), dest=str(dest), evidence=None, note=note,
            env_file=str(self.project / '.env'), no_env_sync=False, no_smoke=False,
            dry_run=False), storage_contract_sha256=contract)

    def retain_known_good(self) -> dict:
        """Keep a private copy of the ready host's engine; it is what a failed successor rolls back to.

        Only the newest known-good engine is kept (one reconstructible copy).
        ``{}`` when the ready host runs no managed engine or its contract is unknown.
        """
        record = self.record()
        plan = record.get('plan') if record.get('state') == 'ready' else None
        if not isinstance(plan, dict) or not plan.get('binarySha256') or not plan.get('binary'):
            return {}
        digest, binary = str(plan['binarySha256']), Path(str(plan['binary']))
        contract = self.engine_contract(plan)
        if not contract:
            return {}
        root = self.known_good_directory()
        target = root / digest
        copy = target / binary.name
        if not copy.is_file():
            root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
            try:
                shutil.copy2(binary, staging / binary.name)
                if (binary.parent / 'libtofu_db.so').is_file():
                    shutil.copy2(binary.parent / 'libtofu_db.so', staging / 'libtofu_db.so')
                if _sha256(staging / binary.name) != digest:
                    raise RuntimeError(f'{binary} changed while it was copied; not retained')
                shutil.rmtree(target, ignore_errors=True)
                os.replace(staging, target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        for other in root.iterdir():
            if other != target:
                shutil.rmtree(other, ignore_errors=True)
        return {'binarySha256': digest, 'binary': str(binary), 'copy': str(copy),
                'storageContractSha256': contract}

    def restore_known_good(self, known_good: dict) -> str:
        """Redeploy the known-good engine when the current plan runs another one.

        Returns what was rolled back ('' when the engine already is the known-good
        one, so only the host needs replacing). Raises when a rollback is needed
        but not allowed: the copy is gone or altered, or this source needs another
        storage contract (the keep rule of restart preparation). The deployment
        owner also refuses an engine that cannot read the store's features.
        """
        current = self.record().get('plan') or {}
        digest = str(known_good.get('binarySha256') or '')
        if not digest or current.get('binarySha256') in (None, '', digest):
            return ''
        copy = Path(str(known_good.get('copy') or ''))
        if not copy.is_file() or _sha256(copy) != digest:
            raise RuntimeError(f'the known-good engine copy {copy} is missing or altered')
        contract = self.current_contract()
        if known_good.get('storageContractSha256') != contract:
            raise RuntimeError(
                f'the known-good engine {digest[:12]} serves storage contract '
                f'{str(known_good.get("storageContractSha256"))[:12]}; this source needs {contract[:12]}')
        dest = Path(str(current.get('binary') or known_good.get('binary')))
        failed = str(current.get('binarySha256'))
        self.deploy_engine(copy, dest, contract,
                           f'automatic rollback from {failed[:12]}, whose first start never served')
        return f'storage engine rolled back from {failed[:12]} to the last known-good {digest[:12]}'

    def owns_lease(self, lease: dict) -> bool:
        record = self.record()
        if (not lease.get('held') or not lease.get('holderVerified')
                or lease.get('kind') != 'storage_authority_sql' or not host_matches(record)):
            return False
        if (record.get('managerPid') != os.getpid()
                or not process_matches(record['managerPid'], record.get('managerStartTime'))):
            return False
        try:
            raw = Path(f'/proc/{int(lease["pid"])}/stat').read_text()
            parent = int(raw[raw.rfind(')') + 2:].split()[1])
            return parent == record['pid']
        except (OSError, KeyError, ValueError, IndexError, TypeError):
            return False
