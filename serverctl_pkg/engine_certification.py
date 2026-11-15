"""Read and verify storage engine certification records.

A record certifies one engine source identity (the ``source_sha256`` that
``tofu-db --build-info`` reports) against one storage contract identity
(``native_engine.storage_contract_sha256``): its exact binary (and embedded
library) bytes passed every check of the engine gate that
``scripts/engine_certify.py`` runs, with the application of a tree carrying
that contract. The contract covers files the engine fingerprint does not, so
one engine source may be certified against several contracts. That command is
the only writer. It publishes ``packages/tofu-db/target/engine-certifications/
<source_sha256>/<storage_contract_sha256>/`` once, by an atomic directory
rename after every check passed, and never replaces an existing record.

Restart preparation (``native_engine``) deploys only bytes a record verifies
here, and keeps a deployed engine for an uncertified HEAD only when a record
or the deployment receipt binds that engine to HEAD's storage contract.

Standard library only. Reading never changes a record or a deployment.
Contract: docs/NATIVE_STORAGE_LIFECYCLE.md, "Engine certification gate".
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

RECORD_FORMAT = 'tofu.engine-certification.v1'
RECORD_FILE = 'certification.json'
BINARY_NAME = 'tofu-db'
LIBRARY_NAME = 'libtofu_db.so'
# The engine gate (GATE-PUBLISH-CARRY's manual gate, made one command): a
# record certifies its engine only when every one of these checks passed.
REQUIRED_CHECKS = (
    'build', 'certify', 'certify-no-online-background', 'cargo-lib',
    'cargo-fastpath', 'ff-native', 'load', 'rollback',
)
_SHA256 = re.compile('[0-9a-f]{64}')


class CertificationRecordInvalid(ValueError):
    """A record directory exists but does not certify its engine."""


@dataclass(frozen=True)
class CertificationRecord:
    directory: Path
    source_sha256: str
    binary: Path
    binary_sha256: str
    library_sha256: str | None
    storage_contract_sha256: str
    commit: str
    certified_utc: str
    operator: str

    @property
    def certification_file(self) -> Path:
        return self.directory / RECORD_FILE

    def summary(self) -> str:
        return (f'certified engine {self.source_sha256[:12]} (commit {self.commit[:10] or "unknown"}, '
                f'certified {self.certified_utc or "at an unknown time"} by {self.operator or "an unknown operator"})')


def is_sha256(value) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def certifications_root(root: Path) -> Path:
    return Path(root) / 'packages/tofu-db/target/engine-certifications'


def record_directory(root: Path, source_sha256: str, storage_contract_sha256: str) -> Path:
    if not is_sha256(source_sha256) or not is_sha256(storage_contract_sha256):
        raise ValueError('Engine source and storage contract identities are lowercase SHA-256 digests.')
    return certifications_root(root) / source_sha256 / storage_contract_sha256


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_record(directory: Path) -> CertificationRecord:
    """Verify one record against its own bytes, or raise CertificationRecordInvalid."""
    from serverctl_pkg.native_engine import EnginePreparationError, build_identity
    directory = Path(directory)
    try:
        document = json.loads((directory / RECORD_FILE).read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise CertificationRecordInvalid(f'it has no {RECORD_FILE}') from exc
    except (OSError, ValueError) as exc:
        raise CertificationRecordInvalid(f'its {RECORD_FILE} is unreadable: {exc}') from exc
    if not isinstance(document, dict) or document.get('format') != RECORD_FORMAT:
        raise CertificationRecordInvalid(f'it is not a {RECORD_FORMAT} record')
    source = document.get('source_sha256')
    contract = document.get('storage_contract_sha256')
    if (not is_sha256(source) or not is_sha256(contract)
            or (directory.parent.name, directory.name) != (source, contract)):
        raise CertificationRecordInvalid('its source and storage contract identities do not name its directory')
    if document.get('verdict') != 'pass':
        raise CertificationRecordInvalid(f"its verdict is {document.get('verdict')!r}")
    verdicts = {check.get('id'): check.get('verdict')
                for check in document.get('checks') or [] if isinstance(check, dict)}
    failed = [check for check in REQUIRED_CHECKS if verdicts.get(check) != 'pass']
    if failed:
        raise CertificationRecordInvalid(f'required checks did not pass: {", ".join(failed)}')
    binary = directory / BINARY_NAME
    library_sha256 = document.get('library_sha256')
    try:
        if sha256_file(binary) != document.get('binary_sha256'):
            raise CertificationRecordInvalid('its engine binary does not match the recorded SHA-256')
        library = directory / LIBRARY_NAME
        if library_sha256 is not None:
            if not library.is_file() or sha256_file(library) != library_sha256:
                raise CertificationRecordInvalid('its embedded library does not match the recorded SHA-256')
        elif library.exists():
            raise CertificationRecordInvalid('it holds an embedded library the record does not declare')
        if build_identity(binary)['source_sha256'] != source:
            raise CertificationRecordInvalid('its engine binary reports another source identity')
    except OSError as exc:
        raise CertificationRecordInvalid(f'its engine could not be read: {exc}') from exc
    except EnginePreparationError as exc:
        raise CertificationRecordInvalid(f'its engine could not identify itself: {exc}') from exc
    operator = document.get('operator')
    return CertificationRecord(
        directory=directory, source_sha256=source, binary=binary,
        binary_sha256=document['binary_sha256'], library_sha256=library_sha256,
        storage_contract_sha256=contract, commit=str(document.get('commit') or ''),
        certified_utc=str(document.get('certified_utc') or ''),
        operator=str(operator.get('user') or '') if isinstance(operator, dict) else str(operator or ''))


def passing_record(root: Path, source_sha256: str,
                   storage_contract_sha256: str) -> tuple[CertificationRecord | None, str]:
    """The verified record for this engine source and contract, or (None, why there is none)."""
    try:
        directory = record_directory(root, source_sha256, storage_contract_sha256)
    except ValueError as exc:
        return None, str(exc)
    if not directory.is_dir():
        return None, f'no certification record exists for it with storage contract {storage_contract_sha256[:12]}'
    try:
        return read_record(directory), ''
    except CertificationRecordInvalid as exc:
        return None, f'its certification record {directory} does not verify: {exc}'


def record_directories(root: Path, storage_contract_sha256: str = '*') -> list[Path]:
    """Record directories, optionally only those of one storage contract."""
    return sorted(path for path in certifications_root(root).glob(f'*/{storage_contract_sha256}')
                  if is_sha256(path.parent.name) and is_sha256(path.name) and path.is_dir())


def record_for_binary(root: Path, binary_sha256: str,
                      storage_contract_sha256: str) -> CertificationRecord | None:
    """The verified record holding exactly these engine bytes for this contract, if one exists."""
    for directory in record_directories(root, storage_contract_sha256):
        try:
            document = json.loads((directory / RECORD_FILE).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or document.get('binary_sha256') != binary_sha256:
            continue
        try:
            return read_record(directory)
        except CertificationRecordInvalid:
            continue
    return None
