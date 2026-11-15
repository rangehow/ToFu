"""Prepare the selected native engine before stopping a serving application.

Standard-library lifecycle boundary shared by CLI, GUI update and startup.
Identity comes from the compiled artifact, not deployment-time Git metadata.
Every door deploys only a certified engine (``engine_certification``): HEAD's
passing record deploys its exact bytes; without one the deployed engine is
kept when it shares HEAD's storage contract and refused otherwise. Only an
explicit, audited operator override (``UncertifiedEngineOverride``) builds or
downloads an engine, in private staging, before the existing deployment owner
publishes it. This module never opens an authority or restarts it.
"""
from __future__ import annotations

import argparse
import codecs
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from .restart_budget import (
    NATIVE_LOCK_WAIT_SECONDS as LOCK_WAIT_TIMEOUT_SECONDS,
    NATIVE_PREPARATION_SECONDS as PREPARATION_TOTAL_TIMEOUT_SECONDS,
    NATIVE_MANIFEST_PARSE_SECONDS as MANIFEST_PARSE_TIMEOUT_SECONDS,
)

SOURCE_PREPARATION_MAX_ATTEMPTS = 3
SOURCE_PREPARATION_SETTLE_SECONDS = 2.0
BUILD_FINALIZATION_RESERVE_SECONDS = 30
# A stable compiler profile is essential to dependency/incremental cache reuse.
# Headroom and CPU availability are not build inputs. Cargo owns its jobserver;
# explicit operator settings remain authoritative.
RELEASE_CODEGEN_UNITS = 64
MAX_LOG_BYTES = 8 * 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 5.0
PROGRESS_PREFIX = "TOFU_ENGINE_PROGRESS "
PROGRESS_MAX_LINE_CHARACTERS = 16 * 1024
# One JSON line naming what preparation decided (current, kept, refused,
# deployed-certified, deployed-uncertified); lifecycle owners parse it from
# the preparation child's combined output.
DECISION_PREFIX = "TOFU_ENGINE_DECISION "
STORAGE_CONTRACT_DIGEST_DOMAIN = b'tofu-storage-contract-v1\0'
# The application<->engine boundary documents the engine does not compile: the
# storage.v2 wire contract and the native host's connection handoff. Compiled
# contracts are the engine's Cargo external-inputs under contracts/.
APPLICATION_STORAGE_CONTRACTS = ('contracts/storage_v2.json', 'contracts/native_storage_connection_v2.json')
MANAGED_ENGINE_ROOT = 'packages/tofu-db/target'
_progress_observer: ContextVar[Callable[[str], None] | None] = ContextVar("engine_progress_observer", default=None)
_progress_phase: ContextVar[str | None] = ContextVar("engine_progress_phase", default=None)
_progress_recovering: ContextVar[bool] = ContextVar("engine_progress_recovering", default=False)
_compiler_probe_environment: ContextVar[dict | None] = ContextVar('native_compiler_probe_environment', default=None)
_deadline: ContextVar[float] = ContextVar('native_engine_preparation_deadline', default=float('inf'))

# Sentinel source path marking the synthetic workspace-root manifest entry in
# the snapshot copy list; its bytes are generated, never read from disk.
_SYNTHETIC_WORKSPACE_SENTINEL = '.workspace-root.sentinel'


class EnginePreparationError(RuntimeError):
    pass


class EnginePreparationCancelledError(RuntimeError):
    """Terminal cancellation, deliberately outside repairable artifact failures."""


class EngineCommandFailedError(EnginePreparationError):
    """A completed child refused its candidate; timeout/cancellation stay distinct."""


class EngineAdmissionRefusedError(EnginePreparationError):
    """The selected engine pair cannot serve this source; preparing a matching pair repairs it.

    Timeouts and failures of preparation commands keep the base type: they are
    failures of the work being prepared, not a missing engine prerequisite.
    """


class EngineSourceChangedError(EnginePreparationError):
    """Observed source drift; only prepublication preparation may retry it."""


class UncertifiedEngineRefusedError(EnginePreparationError):
    """HEAD's engine is uncertified and the deployed engine cannot serve HEAD.

    A typed refusal: lifecycle owners refuse the restart before stopping
    anything and never fall back to retaining the serving storage host.
    """

    def __init__(self, message: str, decision: dict):
        super().__init__(message)
        self.decision = decision


@dataclass(frozen=True)
class UncertifiedEngineOverride:
    """An operator's explicit decision to build and deploy HEAD's uncertified engine once.

    An authority decision, not a setting: it travels as an argument through
    one preparation, is audited where it is made and where it deploys, and is
    never read from the environment, a dotenv file or an approval token.
    """
    decision_id: str
    operator: str
    reason: str

    @classmethod
    def from_payload(cls, payload) -> 'UncertifiedEngineOverride':
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError('expected an object with decisionId, operator and reason')
        fields = {'decision_id': payload.get('decisionId'), 'operator': payload.get('operator'),
                  'reason': payload.get('reason')}
        for name, value in fields.items():
            if not isinstance(value, str) or not value.strip() or '\n' in value:
                raise ValueError(f'{name} must be a non-empty single line')
        if re.fullmatch('[0-9a-f]{32}', fields['decision_id']) is None:
            raise ValueError('decisionId must be 32 lowercase hexadecimal characters')
        return cls(**{name: value.strip() for name, value in fields.items()})

    def payload(self) -> dict:
        return {'decisionId': self.decision_id, 'operator': self.operator, 'reason': self.reason}

    def certification_note(self) -> str:
        return f'UNCERTIFIED operator override {self.decision_id} by {self.operator}: {self.reason}'


def _audit(event: str, **fields) -> None:
    """Append an authority decision to audit.log; lib.log owns format and rotation."""
    from lib.log import audit_log
    audit_log(event, **fields)


def record_uncertified_override(reason: str, *, door: str, terminal: str = '') -> UncertifiedEngineOverride:
    """Create and audit one operator decision to deploy HEAD's uncertified engine."""
    import getpass
    import uuid
    reason = ' '.join(str(reason or '').split())
    if not reason:
        raise ValueError('Deploying an uncertified storage engine needs a reason.')
    try:
        operator = getpass.getuser()
    except OSError:
        operator = 'unknown'
    override = UncertifiedEngineOverride(uuid.uuid4().hex, operator, reason)
    parent = ''
    try:
        parent = Path(f'/proc/{os.getppid()}/cmdline').read_bytes().replace(b'\0', b' ').decode(
            'utf-8', 'replace').strip()
    except OSError:
        pass  # Provenance detail only; the decision itself is recorded below.
    _audit('storage_engine.uncertified_override', decision_id=override.decision_id,
           operator=operator, reason=reason, door=door, tty=terminal,
           pid=os.getpid(), ppid=os.getppid(), parent_command=parent)
    return override


# Stdlib TOML extraction used both to resolve path dependency graphs and to
# classify crate manifests. Kept as a child program so one bounded parse
# failure cannot corrupt the serving process. After the requested manifest it
# parses that crate's path-dependency closure in _path_dependency_crates order,
# following only dependencies that walk accepts, so one operation starts one
# interpreter instead of one per crate. Each parse is one flushed
# [realpath, exact bytes, summary] line; the parent stays the validating
# authority and keys reuse by those bytes (hashlib would add an OpenSSL load
# to every child start).
_TOML_QUERY_PROGRAM = '''
import json, os, sys, tomllib
def summarize(path):
    with open(path, "rb") as handle:
        data = handle.read()
    manifest = tomllib.loads(data.decode())
    found = []
    def visit(section):
        if isinstance(section, dict):
            for name, entry in sorted(section.items()):
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    found.append(entry["path"])
    for key in ("dependencies", "build-dependencies", "dev-dependencies"):
        visit(manifest.get(key))
    for _, target in sorted(manifest.get("target", {}).items()):
        if isinstance(target, dict):
            for key in ("dependencies", "build-dependencies", "dev-dependencies"):
                visit(target.get(key))
    package = manifest.get("package", {})
    cargo_env = {}
    for name, entry in (manifest.get("env") or {}).items():
        if isinstance(entry, str):
            cargo_env[name] = entry
        elif isinstance(entry, dict) and isinstance(entry.get("value"), str):
            cargo_env[name] = entry["value"]
    return data, {
        "package": package.get("name", ""),
        "env": cargo_env,
        "has_workspace": "workspace" in manifest,
        "package_workspace": isinstance(package.get("workspace"), str),
        "package_publish": package.get("publish", True),
        "include": package.get("include"),
        "exclude": package.get("exclude"),
        "external_inputs": package.get("metadata", {}).get("tofu", {}).get("external-inputs", []),
        "dependencies": found,
    }
crates = set()
def parse(crate, path):
    data, summary = summarize(path)
    print(json.dumps([os.path.realpath(path), data.decode("latin-1"), summary]), flush=True)
    container = os.path.dirname(crate)
    for segments in summary["dependencies"]:
        dependency = os.path.realpath(os.path.join(crate, segments))
        manifest = os.path.join(dependency, "Cargo.toml")
        if (dependency not in crates and os.path.isfile(manifest)
                and os.path.commonpath([dependency, container]) == container):
            crates.add(dependency)
            parse(dependency, manifest)
crate = os.path.realpath(os.path.dirname(sys.argv[1]))
crates.add(crate)
parse(crate, sys.argv[1])
'''


def _parse_manifest_closure(manifest: Path) -> tuple[list[tuple[Path, str, dict]], Exception | None]:
    """(realpath, SHA-256 of the parsed bytes, summary) for each manifest one
    isolated child completed, ``manifest`` first, and the failure that stopped
    it before the rest of its closure, if any."""
    failure = None
    try:
        # This standard-library program must not import checkout modules or
        # execute site hooks. Keep the existing bounded child and joined wait.
        output = subprocess.run(
            [sys.executable, '-I', '-S', '-c', _TOML_QUERY_PROGRAM, str(manifest)],
            capture_output=True, check=True, text=True,
            timeout=stage_seconds(MANIFEST_PARSE_TIMEOUT_SECONDS)).stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        output, failure = exc.output, exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [], exc
    try:
        # POSIX timeouts keep partial output as bytes; a child stopped
        # mid-write leaves one unterminated line, which is not a result.
        if isinstance(output, bytes):
            output = output.decode()
        lines = (output or '').split('\n')[:-1]
        return [(Path(key), hashlib.sha256(data.encode('latin-1')).hexdigest(), summary)
                for key, data, summary in map(json.loads, lines)], failure
    except ValueError as exc:
        return [], exc


def _content_digest(path: Path) -> str | None:
    """Streamed SHA-256 of a regular file's current bytes; None leaves the read,
    and the report of why it fails, to the isolated parser."""
    try:
        # O_NONBLOCK keeps a FIFO from blocking this process; regular files ignore it.
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        with open(os.open(path, flags), 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            return hashlib.file_digest(handle, 'sha256').hexdigest()
    except OSError:
        return None


# Summary JSON by SHA-256 of the exact bytes the isolated parser read. Parsing
# is a pure function of those bytes and every read hashes the file first, so an
# edit always reaches the parser; each hit decodes fresh objects, so operations
# never share mutable state. Each operation that starts a parser replaces the
# map with its own manifests, which bounds it to one source tree's closure.
_parsed_manifests: dict[str, str] = {}


def _manifest_error(manifest: Path, failure: Exception) -> EnginePreparationError:
    if isinstance(failure, subprocess.TimeoutExpired):
        reason = f': isolated parser timed out after {failure.timeout:g}s.'
    elif isinstance(failure, subprocess.CalledProcessError):
        reason = f': isolated parser exited with code {failure.returncode}.'
    else:
        reason = f' ({type(failure).__name__}).'
    return EnginePreparationError(f'Cannot read the storage engine source manifest {manifest}{reason}')


def _manifest_reader() -> Callable[[Path], dict]:
    """One operation's parsed manifests; every operation reads disk again.

    Dependency discovery, input selection and external declarations use the
    same successful parse. Each first read in an operation hashes the file and
    reuses a summary only for byte-identical content. Otherwise one isolated
    child parses the file and its crate's dependency closure, so an operation
    starts at most one interpreter for an unchanged tree. A child that stopped
    early has its failure reported once, at the operation's next miss: the
    manifest it was parsing. Nothing is retried, and failed reads are never
    published into this local memo or the content map.
    """
    manifests: dict[Path, dict] = {}
    contents: dict[str, str] = {}
    stopped: Exception | None = None

    def read(manifest: Path) -> dict:
        nonlocal stopped
        global _parsed_manifests
        key = Path(os.path.realpath(manifest))
        if key not in manifests:
            digest = _content_digest(manifest)
            text = _parsed_manifests.get(digest)
            if text is not None:
                contents[digest] = text
                manifests[key] = json.loads(text)
                return manifests[key]
            if stopped is not None:
                failure, stopped = stopped, None
                raise _manifest_error(manifest, failure) from failure
            parsed, failure = _parse_manifest_closure(manifest)
            if not parsed:
                raise _manifest_error(manifest, failure) from failure
            for path, parsed_digest, result in parsed:
                contents[parsed_digest] = json.dumps(result)
                manifests[path] = result
            _parsed_manifests = dict(contents)
            # The first summary answers this read even if the path resolved
            # differently in the child after a concurrent rename.
            manifests[key], stopped = parsed[0][2], failure
        return manifests[key]

    return read


def _read_manifest(manifest: Path) -> dict:
    return _manifest_reader()(manifest)


# Names Cargo always includes in a packaged crate regardless of include rules.
_CARGO_ALWAYS_INCLUDE = ('Cargo.toml', 'Cargo.toml.orig', 'Cargo.lock')


def _crate_source_paths(crate: Path, manifest: dict) -> list[Path]:
    """Retained build inputs of one crate, mirroring Cargo's package-include
    semantics: explicit include rules win for workspace crates, otherwise
    Cargo.toml[.orig]/Cargo.lock/build.rs plus every rust source."""
    publish = manifest.get('package_publish', True)
    include = manifest.get('include') if publish is not False else None
    if include:
        selected = [crate / pattern.rstrip('/')
                    for pattern in (*include, *_CARGO_ALWAYS_INCLUDE)]
        paths = [path for path in dict.fromkeys(selected) if path.is_file()]
    else:
        paths = [crate / name for name in ('Cargo.toml', 'Cargo.toml.orig', 'Cargo.lock', 'build.rs')
                 if (crate / name).is_file()]
        for directory, folders, names in os.walk(crate / 'src'):
            folders.sort()
            paths.extend(Path(directory) / name for name in names if name.endswith('.rs'))
    # .cargo/config.toml configures the build but is never a packaged input.
    configuration = crate / '.cargo'
    if configuration.is_dir():
        for directory, folders, names in os.walk(configuration):
            folders.sort()
            paths.extend(Path(directory) / name for name in names)
    return paths


def _normalized(relative: PurePosixPath) -> PurePosixPath:
    """Resolve '.'/'..' segments of a snapshot-relative crate location without
    touching the filesystem, mirroring packages/tofu-db/build.rs::normalized."""
    parts: list[str] = []
    for part in relative.parts:
        if part in ('', '.'):
            continue
        if part == '..':
            # The root crate's own '..' escapes its update-source dir into the
            # snapshot parent (target/); with nothing accumulated it maps to a
            # direct child of the snapshot parent. A nested dependency's '..'
            # pops its accumulated in-snapshot component.
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath('.')


def _path_dependency_crates(crate: Path, discovered: dict[Path, PurePosixPath] | None = None,
                            relative: PurePosixPath | None = None, *,
                            read_manifest: Callable[[Path], dict] | None = None) -> dict[Path, PurePosixPath]:
    """Map every transitive path-dependency crate to its snapshot-relative dir.

    A path dependency contributes real build inputs but lives outside the
    root crate; omitting it leaves the source snapshot uncompilable and lets
    sibling edits escape the compiled source identity. The relative location
    preserves the manifest's path segments so the snapshot reproduces the
    exact Cargo-relative layout the compiler resolves. Locations are
    canonicalized (symlinks resolved) exactly like build.rs::closure so the
    Python and embedded identities agree byte for byte.
    """
    if relative is None:
        relative = PurePosixPath('.')
    if discovered is None:
        discovered = {}
    if read_manifest is None:
        read_manifest = _read_manifest
    anchor = Path(os.path.realpath(crate))
    if anchor in discovered:
        return discovered
    manifest_path = anchor / 'Cargo.toml'
    manifest = read_manifest(manifest_path)
    if not manifest.get('package'):
        raise EnginePreparationError(f'The storage engine source manifest {manifest_path} names no package.')
    discovered[anchor] = relative
    for segments in manifest['dependencies']:
        dependency = Path(os.path.realpath(manifest_path.parent / segments))
        if not dependency.is_relative_to(anchor.parent):
            raise EnginePreparationError(
                'The storage engine path dependency escapes its packages directory.')
        if not (dependency / 'Cargo.toml').is_file():
            raise EnginePreparationError(
                f'The storage engine path dependency has no manifest at {dependency}.')
        child = _normalized(PurePosixPath(*relative.parts, *PurePosixPath(segments).parts))
        _path_dependency_crates(dependency, discovered, child, read_manifest=read_manifest)
    return discovered


def source_layout(root: Path) -> list[tuple[Path, PurePosixPath, list[Path], bool]]:
    """(location, snapshot-relative crate dir, retained source paths,
    uses-workspace-inheritance) for the engine and each path dependency."""
    return _source_layout(root, _manifest_reader())


def _source_layout(root: Path, read_manifest: Callable[[Path], dict]):
    crate = root / 'packages/tofu-db'
    layout = []
    for location, relative in _path_dependency_crates(crate, read_manifest=read_manifest).items():
        manifest = read_manifest(location / 'Cargo.toml')
        inherits = not manifest['has_workspace'] and not manifest['package_workspace']
        layout.append((location, relative, sorted(_crate_source_paths(location, manifest)), inherits))
    return layout


def external_source_inputs(root: Path) -> list[tuple[str, Path]]:
    """Declared project-root inputs shared with the native build script."""
    return _external_source_inputs(root, _read_manifest)


def _external_source_inputs(root: Path, read_manifest: Callable[[Path], dict]):
    values = read_manifest(root / 'packages/tofu-db/Cargo.toml')['external_inputs']
    if not isinstance(values, list):
        raise EnginePreparationError('External native inputs must be a path list.')
    entries = []
    for value in values:
        if not isinstance(value, str):
            raise EnginePreparationError('External native input must be a relative path.')
        relative = PurePosixPath(value)
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                or relative.as_posix() != value or not (value == 'VERSION' or value.startswith('contracts/'))):
            raise EnginePreparationError('External native input escapes its declared source namespace.')
        path = root / value
        if not path.is_file() or path.resolve() != root.resolve() / value:
            raise EnginePreparationError('External native input is missing or uses a symlink.')
        entries.append((value, path))
    if len({key for key, _ in entries}) != len(entries):
        raise EnginePreparationError('External native input paths collide.')
    return entries


def _update_source_digest(digest, key: str, path: Path) -> None:
    """Keep the v2 length-prefixed byte protocol without retaining whole files."""
    try:
        handle = path.open('rb')
    except FileNotFoundError as exc:
        raise EngineSourceChangedError(f'Storage source disappeared while fingerprinting {key}.') from exc
    with handle:
        before = os.fstat(handle.fileno())
        remaining = before.st_size
        digest.update(key.encode() + b'\0')
        digest.update(remaining.to_bytes(8, 'little'))
        while remaining:
            content = handle.read(min(remaining, 1024 * 1024))
            if not content:
                raise EngineSourceChangedError(f'Storage source changed while fingerprinting {key}.')
            digest.update(content)
            remaining -= len(content)
        extra = handle.read(1)
        after = os.fstat(handle.fileno())
        if (extra or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise EngineSourceChangedError(f'Storage source changed while fingerprinting {key}.')


def source_fingerprint(root: Path) -> str | None:
    crate = root / 'packages/tofu-db'
    if not (crate / 'Cargo.toml').is_file():
        return None  # Frozen distributions validate their packaged artifact pin.
    digest = hashlib.sha256(b'tofu-db-source-v2\0')
    entries = []
    read_manifest = _manifest_reader()
    for location, relative, paths, _inherits in _source_layout(root, read_manifest):
        # Logical keys survive relocation into update-source. Never include
        # the synthetic workspace manifest or the staging directory basename.
        prefix = PurePosixPath('tofu-db') if str(relative) == '.' else relative
        for path in paths:
            entries.append(((prefix / path.relative_to(location).as_posix()).as_posix(), path))
    entries.extend(('@external/' + key, path) for key, path in _external_source_inputs(root, read_manifest))
    if len({key for key, _ in entries}) != len(entries):
        raise EnginePreparationError('The storage engine source identity has colliding paths.')
    for key, path in sorted(entries):
        _update_source_digest(digest, key, path)
    return digest.hexdigest()


def storage_contract_sha256(root: Path) -> str:
    """Identity of the storage contract an application and its engine must share.

    The application-side boundary documents plus the contracts the engine
    compiles (its Cargo external-inputs under contracts/), in the source
    identity's length-prefixed framing. VERSION and unrelated contracts are
    not part of it.
    """
    root = Path(root)
    entries = {relative: root / relative for relative in APPLICATION_STORAGE_CONTRACTS}
    entries.update((key, path) for key, path in external_source_inputs(root)
                   if key.startswith('contracts/'))
    digest = hashlib.sha256(STORAGE_CONTRACT_DIGEST_DOMAIN)
    for key in sorted(entries):
        if not entries[key].is_file():
            raise EnginePreparationError(f'The storage contract {key} is missing from {root}.')
        _update_source_digest(digest, key, entries[key])
    return digest.hexdigest()


@contextmanager
def observe_preparation(callback: Callable[[dict], None]) -> Iterator[None]:
    """Scope structured child progress to one restart request; never relay logs."""
    pending = ''
    def receive(chunk: str) -> None:
        nonlocal pending
        pending = (pending + chunk)[-PROGRESS_MAX_LINE_CHARACTERS:]
        lines = pending.split('\n')
        pending = lines.pop()
        for line in lines:
            if line.startswith(PROGRESS_PREFIX):
                try:
                    callback(json.loads(line[len(PROGRESS_PREFIX):]))
                except (ValueError, TypeError, OSError):
                    pass  # Progress cannot prevent cancellation or artifact repair.
    token = _progress_observer.set(receive)
    phase_token = _progress_phase.set(None)
    try:
        yield
    finally:
        _progress_observer.reset(token)
        _progress_phase.reset(phase_token)


def _report_stage(phase: str, message: str) -> None:
    _progress_phase.set(phase)
    _report_progress(message)


def _report_progress(message: str, *, end: str = '\n') -> None:
    try:
        print(message, file=sys.stderr, end=end, flush=True)
        observer = _progress_observer.get()
        if observer is not None:
            observer(message + end)
        phase = _progress_phase.get()
        if phase and end == '\n':
            remaining = max(0, _deadline.get() - time.monotonic())
            frame = {'phase': phase}
            if _progress_recovering.get():
                frame['recovering'] = True
            if remaining != float('inf'):
                frame['remainingSeconds'] = round(remaining)
            encoded = PROGRESS_PREFIX + json.dumps(frame)
            print(encoded, file=sys.stderr, flush=True)
            if observer is not None:
                observer(encoded + '\n')
    except (OSError, ValueError):
        pass  # A closed progress sink cannot break preparation or its cleanup.


def _stop_command(child: subprocess.Popen, grace: float) -> None:
    """Reap the leader and kill remaining group members even if it exits first."""
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                           capture_output=True, timeout=10, check=False)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # The entire group has already exited.
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass  # Escalation below also covers descendants ignoring SIGTERM.
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The entire group exited during graceful termination.
        child.wait(timeout=10)


def _run(command: list[str], *, cwd: Path, environment: dict, timeout: float,
         progress: str | None = None, termination_grace: float = 5,
         failure_subject: str = 'Storage engine preparation') -> str:
    """Bound output/lifetime; optionally relay output live with idle heartbeats.

    Separate reader/writer offsets prevent progress reads from corrupting the
    child's log. No reader thread, pipe backpressure or persistent log is added.
    Machine-readable build identity remains capture-only.
    """
    started = time.monotonic()
    deadline = min(_deadline.get(), started + timeout)
    if started >= deadline:
        raise EnginePreparationError(f'{failure_subject} timed out (time budget exhausted).')
    with tempfile.TemporaryDirectory(prefix='tofu-engine-command-') as temporary, \
            (Path(temporary) / 'output.log').open('wb') as log, \
            (Path(temporary) / 'output.log').open('rb') as reader:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
        next_progress = started + PROGRESS_INTERVAL_SECONDS
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        failure = ''
        try:
            while True:
                now = time.monotonic()
                size = os.fstat(log.fileno()).st_size
                if size > MAX_LOG_BYTES:
                    failure = f'{failure_subject} exceeded its log budget.'
                    break
                if progress:
                    while reader.tell() < size:
                        data = reader.read(min(65536, size - reader.tell()))
                        _report_progress(decoder.decode(data), end='')
                        next_progress = now + PROGRESS_INTERVAL_SECONDS
                if child.poll() is not None:
                    # One more read below includes output written during poll.
                    break
                if now >= deadline:
                    failure = f'{failure_subject} timed out after {now - started:.1f}s (time budget exhausted).'
                    break
                if progress and now >= next_progress:
                    _report_progress(f'{progress}: still running ({now - started:.0f}s elapsed, '
                                     f'{max(0, deadline - now):.0f}s remaining).')
                    next_progress = now + PROGRESS_INTERVAL_SECONDS
                try:
                    child.wait(timeout=min(0.1, max(0.001, deadline - now)))
                except subprocess.TimeoutExpired:
                    pass  # Poll output and the shared deadline on the next tick.
        except BaseException:
            _stop_command(child, termination_grace)
            raise
        if failure or child.returncode:
            _stop_command(child, termination_grace)
        size = os.fstat(log.fileno()).st_size
        if size > MAX_LOG_BYTES:
            failure = failure or f'{failure_subject} exceeded its log budget.'
        if progress and not failure:
            _report_progress(decoder.decode(reader.read(MAX_LOG_BYTES), final=True), end='')
        output = _failure_output(reader, size)
        if failure:
            raise EnginePreparationError(f'{failure}\n{output}' if output else failure)
        if child.returncode < 0:
            raise EnginePreparationCancelledError(
                f'{failure_subject} was terminated by signal {-child.returncode}; '
                f'preparation was cancelled. {output}')
        if child.returncode:
            raise EngineCommandFailedError(f'{failure_subject} failed (exit {child.returncode}): {output}')
        return output


def _failure_output(reader, size: int, head_bytes: int = 4096, tail_bytes: int = 12288) -> str:
    """Head + tail of the child log for an error report.

    A cargo refusal usually fails EARLY (denied lint, missing feature) while
    the tail alone used to leave the operator guessing; the 8KB tail-only
    capture routinely cut the actual ``error:`` line out of the report.
    """
    if size <= head_bytes + tail_bytes:
        reader.seek(0)
        return reader.read().decode('utf-8', errors='replace').strip()
    reader.seek(0)
    head = reader.read(head_bytes).decode('utf-8', errors='replace')
    reader.seek(size - tail_bytes)
    tail = reader.read(tail_bytes).decode('utf-8', errors='replace')
    return (head.rstrip() +
            f'\n… [log truncated: {size - head_bytes - tail_bytes} bytes omitted] …\n' +
            tail.lstrip()).strip()


def build_identity(binary: Path) -> dict:
    try:
        output = _run([str(binary), '--build-info'], cwd=binary.parent,
                      environment=dict(os.environ), timeout=stage_seconds(10))
    except EnginePreparationError as exc:
        # The selected engine could not report its identity, so it is not admitted.
        raise EngineAdmissionRefusedError(str(exc)) from exc
    try:
        info = json.loads(output)
        if (info['format'] != 'tofu-db-build.v1'
                or re.fullmatch('[0-9a-f]{64}', info['source_sha256']) is None):
            raise ValueError('invalid engine identity')
        return info
    except (ValueError, KeyError, TypeError) as exc:
        raise EngineAdmissionRefusedError('Storage engine has no verifiable source identity; an engine update is required.') from exc


def verify_current(binary: Path, root: Path) -> None:
    expected = source_fingerprint(root)
    if expected is not None and build_identity(binary)['source_sha256'] != expected:
        raise EngineAdmissionRefusedError('Storage engine does not match this application. Use Update or Restart to prepare the matching engine; the existing server has not been stopped.')


def verify_admissible(binary: Path, root: Path) -> None:
    """Admit HEAD's engine, or the deployed engine kept for an uncertified HEAD.

    The keep rule is restart preparation's: a managed deployment whose bytes
    a deployment receipt or certification record binds to HEAD's storage
    contract. Every other mismatch keeps the existing admission refusal.
    """
    expected = source_fingerprint(root)
    if expected is None:
        return
    identity = build_identity(binary)['source_sha256']
    if identity == expected:
        return
    root, binary = Path(root).resolve(), Path(binary).resolve()
    if binary.is_relative_to(root / MANAGED_ENGINE_ROOT):
        try:
            from serverctl_pkg.engine_certification import sha256_file
            contract = storage_contract_sha256(root)
            bound, _where, _commit = _contract_binding(root, binary, identity, sha256_file(binary), contract)
            if bound == contract:
                return
        except (OSError, EnginePreparationError):
            pass  # An unverifiable binding is no binding; the refusal below applies.
    raise EngineAdmissionRefusedError(
        f'Storage engine {identity[:12]} does not match this application ({expected[:12]}) and is not '
        'bound to its storage contract. Restart to deploy the certified engine for this source '
        '(scripts/engine_certify.py certifies it); the existing server has not been stopped.')


def retention_refusal(root: Path, plan: dict) -> str:
    """'' when a retained storage host's engine shares HEAD's storage contract, else why not.

    The lifecycle manager retains a ready storage host when engine preparation
    fails; the replaced application must still share that engine's contract.
    """
    binary_sha256 = str((plan or {}).get('binarySha256') or '')
    root = Path(root).resolve()
    if not binary_sha256 or not (root / 'packages/tofu-db/Cargo.toml').is_file():
        return ''  # No native engine retained, or a packaged install's bundled pair.
    try:
        contract = storage_contract_sha256(root)
    except (OSError, EnginePreparationError) as exc:
        return f'the storage contract of this source could not be read: {exc}'
    from serverctl_pkg import engine_certification
    bound = None
    binary = Path(str(plan.get('binary') or ''))
    if binary.name and binary.resolve().is_relative_to(root / MANAGED_ENGINE_ROOT):
        receipt = _deployment_receipt(binary.resolve())
        if (receipt.get('binary_sha256') == binary_sha256
                and engine_certification.is_sha256(receipt.get('storage_contract_sha256'))):
            bound = receipt['storage_contract_sha256']
    if bound is None and engine_certification.record_for_binary(root, binary_sha256, contract) is not None:
        bound = contract
    if bound is None:
        return (f'the retained storage host runs engine {binary_sha256[:12]}, whose storage contract '
                f'is not recorded; this source needs storage contract {contract[:12]}')
    if bound != contract:
        return (f'the retained storage host runs engine {binary_sha256[:12]} with storage contract '
                f'{bound[:12]}; this source needs storage contract {contract[:12]}')
    return ''


def _deployment_receipt(binary: Path) -> dict:
    try:
        receipt = json.loads((binary.parent / 'build.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return receipt if isinstance(receipt, dict) else {}


def _contract_binding(root: Path, binary: Path, source_sha256: str, binary_sha256: str,
                      contract: str) -> tuple[str | None, str, str]:
    """(storage contract these engine bytes are bound to, where, deployment commit).

    The deployment receipt binds the exact bytes it installed to the contract
    of the tree it prepared; failing that, a passing certification record of
    the engine source those bytes report binds them to ``contract``.
    """
    from serverctl_pkg import engine_certification
    receipt = _deployment_receipt(binary)
    commit = str(receipt.get('deployment_checkout_head') or '')
    if (receipt.get('binary_sha256') == binary_sha256 and receipt.get('source_sha256') == source_sha256
            and engine_certification.is_sha256(receipt.get('storage_contract_sha256'))
            and receipt['storage_contract_sha256'] == contract):
        return contract, str(binary.parent / 'build.json'), commit
    record, _problem = engine_certification.passing_record(root, source_sha256, contract)
    if record is not None:
        return contract, str(record.certification_file), commit or record.commit
    recorded = receipt.get('storage_contract_sha256') if receipt.get('binary_sha256') == binary_sha256 else None
    return (recorded if engine_certification.is_sha256(recorded) else None), '', commit


def parse_engine_decision(text: str) -> dict | None:
    """The last preparation decision reported in a child's combined output."""
    found = None
    for line in str(text or '').splitlines():
        if not line.startswith(DECISION_PREFIX):
            continue
        try:
            value = json.loads(line[len(DECISION_PREFIX):])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('decision'), str):
            found = value
    return found


def _report_decision(decision: dict, *, announce: bool = True) -> None:
    if announce:
        _report_progress(decision['message'])
    _report_progress(DECISION_PREFIX + json.dumps(decision, sort_keys=True, separators=(',', ':')))


def _engine_input_commits(root: Path, base: str) -> list[str] | None:
    """Engine-input commits after BASE through HEAD, or None when Git cannot tell."""
    base = base.removesuffix('-dirty')
    if re.fullmatch('[0-9a-f]{7,64}', base) is None:
        return None
    try:
        paths = {location.resolve().relative_to(root).as_posix()
                 for location, _relative, _paths, _inherits in source_layout(root)}
        paths.update(key for key, _path in external_source_inputs(root))
        output = _run(['git', '-C', str(root), 'log', '--format=%h', f'{base}..HEAD', '--', *sorted(paths)],
                      cwd=root, environment=dict(os.environ), timeout=stage_seconds(30),
                      failure_subject='Listing engine-input commits')
    except (OSError, ValueError, EnginePreparationError):
        return None
    return output.split()


def _commits_text(commits: list[str] | None, base: str) -> str:
    if commits is None:
        return ('engine-input commits unknown (the deployed engine records no source commit)' if not base
                else f'engine-input commits since {base} unknown (Git could not list them)')
    if not commits:
        return f'no engine-input commits since {base} (uncommitted engine edits)'
    return 'commits ' + ', '.join(commits)


def _next_steps(root: Path, *, deployed_commit: str = '') -> str:
    steps = [f'Next steps (from {root}):',
             '  certify HEAD, then retry: .venv/bin/python3 scripts/engine_certify.py '
             '--fixture <settled TofuDB authority snapshot>']
    if deployed_commit:
        steps.append('  or certify the deployed engine\'s source so it can be kept: .venv/bin/python3 '
                     f'scripts/engine_certify.py --commit {deployed_commit} --fixture <settled TofuDB authority snapshot>')
    steps.append('  or deploy HEAD\'s engine uncertified (interactive terminal, audited): '
                 '.venv/bin/python3 serverctl.py restart --deploy-uncertified-engine "<reason>"')
    return '\n'.join(steps)


def _uncertified_head_decision(root: Path, environment: dict, dest: Path, expected: str,
                               contract: str, record_problem: str) -> dict:
    """Keep the deployed engine for an uncertified HEAD, or say exactly why not."""
    from serverctl_pkg.engine_certification import passing_record, sha256_file
    decision = {'decision': 'refused', 'headSourceSha256': expected,
                'headStorageContractSha256': contract, 'headRecordProblem': record_problem,
                'deployedEngine': str(dest)}
    commit = ''
    if not dest.is_file():
        reason = f'no storage engine is deployed at {dest}'
        commits = None
    else:
        try:
            identity = build_identity(dest)['source_sha256']
            _verify_pin(dest, environment)
            if (environment.get('TOFUDB_TRANSPORT') == 'embedded'
                    or (environment.get('TOFUDB_TRANSPORT', 'auto') == 'auto'
                        and (dest.parent / 'libtofu_db.so').exists())):
                verify_library(dest)
            binary_sha256 = sha256_file(dest)
        except (OSError, EnginePreparationError) as exc:
            reason = f'the deployed engine {dest} cannot be kept: {exc}'
            commits = None
        else:
            deployed, bound_by, commit = _contract_binding(root, dest, identity, binary_sha256, contract)
            commits = _engine_input_commits(root, commit) if commit else None
            certified, _problem = passing_record(root, identity, contract)
            decision.update(deployedSourceSha256=identity, deployedBinarySha256=binary_sha256,
                            deployedStorageContractSha256=deployed, deployedContractRecordedIn=bound_by,
                            deployedCertified=certified is not None,
                            deployedCommit=commit, commits=commits)
            head = f'engine at HEAD is uncertified: {expected[:12]}, {_commits_text(commits, commit)}'
            if deployed == contract:
                decision['decision'] = 'kept'
                decision['message'] = (
                    f'{head}; kept {identity[:12]}. Its storage contract {contract[:12]} is unchanged; '
                    f'the kept engine is {"certified" if certified is not None else "itself uncertified"}. '
                    'Certify HEAD (scripts/engine_certify.py) to deploy its engine.')
                return decision
            reason = (f'the deployed engine {identity[:12]} is bound to no storage contract: its deployment '
                      'receipt records none and no certification record certifies it against HEAD\'s '
                      f'storage contract {contract[:12]}' if deployed is None else
                      f'the deployed engine {identity[:12]} serves storage contract {deployed[:12]}; '
                      f'HEAD needs {contract[:12]}')
    head = f'engine at HEAD is uncertified: {expected[:12]}, {_commits_text(commits, commit)}'
    # Certifying the deployed engine's own commit can make it keepable only when
    # its contract is unknown and that commit is exactly what it was built from.
    recertifiable = ('deployedSourceSha256' in decision and decision['deployedStorageContractSha256'] is None
                     and commit and not commit.endswith('-dirty'))
    decision['message'] = (
        f'{head}; refused: {reason}. Nothing was built, replaced or stopped.\n'
        + _next_steps(root, deployed_commit=commit if recertifiable else ''))
    return decision


def verify_library(binary: Path) -> None:
    """Require the embedded library to belong to the same deployed pair."""
    try:
        record = json.loads((binary.parent / 'build.json').read_text())
        expected = record['library_sha256']
        digest = hashlib.sha256()
        with (binary.parent / 'libtofu_db.so').open('rb') as handle:
            for data in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(data)
        info = build_identity(binary)
        selected_allocator = 'mimalloc' if sys.platform == 'linux' else 'system'
        if (digest.hexdigest() != expected
                or record['source_sha256'] != info['source_sha256']
                or info.get('embedded_allocator') != selected_allocator):
            raise ValueError('engine pair differs')
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise EngineAdmissionRefusedError('The embedded storage engine pair is incomplete or mismatched. Use Update to prepare it.') from exc


def _cached_library_matches_source(binary: Path, expected: str) -> bool:
    """Attest a cached library in a disposable process, without opening data.

    Cargo's --bin builds can leave an older sibling library behind. File
    presence alone is insufficient to reuse an embedded engine pair.
    """
    try:
        _verify_library_source(binary, expected)
        return True
    except (OSError, EnginePreparationError):
        return False


def _verify_library_source(binary: Path, expected: str) -> None:
    """Attest the library without data access and preserve the refusal reason."""
    library = binary.parent / 'libtofu_db.so'
    if not library.is_file():
        raise EnginePreparationError('The candidate embedded library is missing; publication was cancelled.')
    program = (
        'import ctypes,sys; library=ctypes.CDLL(sys.argv[1]); '
        'identity=library.tofudb_embed_source_sha256; '
        'identity.argtypes=[ctypes.c_void_p,ctypes.c_size_t]; '
        'identity.restype=ctypes.c_ssize_t; '
        'output=ctypes.create_string_buffer(65); '
        'size=identity(output,65); '
        'allocator=library.tofudb_embed_allocator_kind; allocator.restype=ctypes.c_uint32; '
        'print(output.value.decode("ascii")+":"+str(allocator()) if size == 64 else "invalid")'
    )
    try:
        reported = _run([sys.executable, '-c', program, str(library)],
                        cwd=library.parent, environment=dict(os.environ), timeout=stage_seconds(10))
    except (OSError, EnginePreparationError) as exc:
        raise EnginePreparationError(
            f'The candidate embedded library could not be attested; publication was cancelled. {exc}') from exc
    match = re.fullmatch(r'([0-9a-f]{64}):([0-9]+)', reported)
    if match is None:
        raise EnginePreparationError(
            'The candidate embedded library returned invalid build metadata; publication was cancelled.')
    actual_source, allocator = match.groups()
    if actual_source != expected:
        raise EnginePreparationError(
            f'The candidate embedded library source identity differs from the binary '
            f'(expected {expected}, got {actual_source}); publication was cancelled.')
    selected_allocator = int(sys.platform == 'linux')
    if allocator != str(selected_allocator):
        raise EnginePreparationError(
            f'The candidate embedded library allocator is incompatible '
            f'(expected {selected_allocator}, got {allocator}); publication was cancelled.')


def _verify_pin(binary: Path, environment: dict) -> None:
    pin = environment.get('TOFUDB_EXPECTED_SHA256', '').strip().lower()
    sidecar = binary.with_name(binary.name + '.sha256')
    if not pin and sidecar.exists():
        fields = sidecar.read_text().split()
        pin = fields[0].lower() if fields else 'invalid'
    if pin:
        digest = hashlib.sha256()
        with binary.open('rb') as handle:
            for data in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(data)
        if pin != digest.hexdigest():
            raise EnginePreparationError('The storage engine content pin does not match its artifact.')


def reconcile_project_pin(root: Path, project: dict, environment: dict) -> bool:
    """Carry an inherited, previously managed pin across a verified deployment.

    Old serving processes loaded dotenv before the updater replaced it. The
    deployment owner's previous/current receipt distinguishes that case from
    an unrelated explicit override; the new on-disk content is checked too.
    """
    inherited = environment.get('TOFUDB_EXPECTED_SHA256')
    current = project.get('TOFUDB_EXPECTED_SHA256')
    if not inherited or not current or inherited == current:
        return False
    binary = Path(environment.get('TOFUDB_BIN') or root / 'packages/tofu-db/target/release/tofu-db').expanduser().resolve()
    if not binary.is_relative_to(root.resolve() / 'packages/tofu-db/target'):
        return False
    try:
        receipt = json.loads((binary.parent / 'build.json').read_text())
        if (receipt.get('previous_binary_sha256') != inherited
                or receipt.get('binary_sha256') != current):
            return False
        _verify_pin(binary, {'TOFUDB_EXPECTED_SHA256': current})
    except (OSError, ValueError, EnginePreparationError):
        return False
    environment['TOFUDB_EXPECTED_SHA256'] = current
    return True


@contextmanager
def _preparation_budget(seconds: float) -> Iterator[None]:
    """Scope a stage deadline without extending the caller's total deadline."""
    token = _deadline.set(min(_deadline.get(), time.monotonic() + seconds))
    try:
        yield
    finally:
        _deadline.reset(token)


@contextmanager
def _prepare_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
        handle.flush()
        deadline = min(_deadline.get(), time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS)
        started = time.monotonic()
        next_progress = started
        # Waiting has its own bounded budget and progress countdown. Release
        # that scope before yielding so a late acquisition cannot starve work.
        with _preparation_budget(LOCK_WAIT_TIMEOUT_SECONDS):
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise EnginePreparationError('Another storage engine update is still running. Retry after it finishes.')
                    if time.monotonic() >= next_progress:
                        _report_stage('waiting', f'Waiting for another storage engine preparation '
                                         f'({time.monotonic() - started:.0f}s elapsed).')
                        next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
                    time.sleep(0.2)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _download(url: str, target: Path, maximum: int) -> None:
    if not url.startswith('https://'):
        raise EnginePreparationError('Engine downloads require HTTPS.')
    deadline = min(_deadline.get(), time.monotonic() + 90)
    with urllib.request.urlopen(url, timeout=20) as response, target.open('xb') as output:
        if not response.geturl().startswith('https://'):
            raise EnginePreparationError('Engine download redirected away from HTTPS.')
        total = 0
        while data := response.read(65536):
            total += len(data)
            if total > maximum or time.monotonic() > deadline:
                raise EnginePreparationError('Engine download exceeded its size or time budget.')
            output.write(data)


def _prebuilt(root: Path, staging: Path, environment: dict) -> Path | None:
    if environment.get('TOFU_TOFUDB_NO_DOWNLOAD') == '1':
        return None
    machine = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(platform.machine().lower(), platform.machine().lower())
    darwin_machine = 'arm64' if machine == 'aarch64' else machine
    triple = {'Linux': f'{machine}-unknown-linux-musl',
              'Darwin': f'{darwin_machine}-apple-darwin'}.get(platform.system())
    if triple is None or machine not in {'x86_64', 'aarch64'}:
        return None
    version = (root / 'VERSION').read_text().strip()
    if re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,79}', version) is None:
        raise EnginePreparationError('Invalid application release version.')
    name = f'tofu-db-{version}-{triple}.tar.gz'
    base = environment.get('TOFU_TOFUDB_PREBUILT_BASE_URL') or f'https://github.com/rangehow/ToFu/releases/download/v{version}'
    archive, checksum = staging / name, staging / (name + '.sha256')
    _report_stage('downloading', f'Checking for a matching prebuilt storage engine ({version}, {triple}).')
    _download(base.rstrip('/') + '/' + name, archive, 512 * 1024 * 1024)
    _download(base.rstrip('/') + '/' + checksum.name, checksum, 4096)
    from scripts.install_tofudb_prebuilt import stage_verified_archive
    return stage_verified_archive(archive, checksum, staging / 'prebuilt')


def _sync_build_snapshot(root: Path, crate: Path, snapshot: Path) -> None:
    """Refresh the one owned source copy without invalidating unchanged inputs.

    The preparation lock serializes this directory. Random source paths and
    rewritten mtimes make Cargo rebuild the engine on every retry even when
    the previously compiled inputs are identical. Path dependencies are
    mirrored as sibling crates below the snapshot so their Cargo-relative
    locations still resolve.
    """
    if snapshot.is_symlink():
        raise EnginePreparationError('The storage build snapshot must not be a symlink.')
    snapshot.mkdir(parents=True, exist_ok=True)
    read_manifest = _manifest_reader()
    layout = _source_layout(root, read_manifest)
    external = _external_source_inputs(root, read_manifest)
    copies: list[tuple[Path, Path]] = []
    target_crates: list[Path] = []
    member_dirs: list[str] = []
    needs_workspace = False
    for location, relative, paths, inherits in layout:
        target_crate = snapshot if str(relative) == '.' else snapshot.parent / relative
        target_crates.append(target_crate)
        if str(relative) != '.':
            member_dirs.append(relative.as_posix())
            needs_workspace = needs_workspace or inherits
        copies.extend((path, target_crate / path.relative_to(location)) for path in paths)
    # A mirrored crate that inherits package fields (edition.workspace = true)
    # from a workspace root that is not a build input cannot compile alone.
    # Materialize a minimal synthetic root declaring exactly the mirrored
    # members (by snapshot-relative path) so Cargo resolves the inheritance
    # inside the snapshot.
    workspace_manifest = ''
    if needs_workspace:
        listing = ', '.join(f'"{member}"' for member in sorted(member_dirs))
        workspace_manifest = ('[workspace]\nresolver = "2"\nmembers = [' + listing + ']\n'
                              # The root engine crate snapshot lives below the
                              # synthetic root too; it is not a workspace member.
                              f'exclude = ["{snapshot.name}"]\n'
                              '[workspace.package]\nedition = "2021"\nrust-version = "1.85"\nlicense = "Apache-2.0"\n')
        copies.append((snapshot.parent / _SYNTHETIC_WORKSPACE_SENTINEL,
                       snapshot.parent / 'Cargo.toml'))
    external_root = snapshot.parent.parent
    ledger = snapshot.parent / '.external-inputs.json'
    if ledger.is_symlink():
        raise EnginePreparationError('The external input ledger must not be a symlink.')
    previous = json.loads(ledger.read_text()) if ledger.exists() else []
    def safe_external_key(key):
        return (isinstance(key, str) and bool(key)
                and PurePosixPath(key).as_posix() == key
                and not PurePosixPath(key).is_absolute()
                and '..' not in PurePosixPath(key).parts
                and (key == 'VERSION' or key.startswith('contracts/')))
    if not isinstance(previous, list) or not all(safe_external_key(key) for key in previous):
        raise EnginePreparationError('The external input ledger contains unsafe paths.')
    for key in set(previous) | {key for key, _ in external}:
        target = external_root / key
        if target.resolve() != external_root.resolve() / key:
            raise EnginePreparationError('The external input snapshot contains an unsafe path.')
    for key, path in external:
        target = external_root / key
        if key not in previous and target.exists():
            raise EnginePreparationError('The external input target is not owned by this snapshot.')
    for key, path in external:
        target = external_root / key
        if target.resolve() != external_root.resolve() / key:
            raise EnginePreparationError('The external input snapshot contains an unsafe path.')
        target.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            target.write_bytes(content)
    for key in set(previous) - {key for key, _ in external}:
        target = external_root / key
        if target.resolve() != external_root.resolve() / key:
            raise EnginePreparationError('The external input snapshot contains an unsafe path.')
        target.unlink(missing_ok=True)
    ledger.write_text(json.dumps([key for key, _ in external]))
    retained = {target for _, target in copies}
    snapshot_parent = snapshot.parent.resolve()
    # build.rs watches these roots to detect new/deleted inputs, even when
    # they are empty. Missing watched paths make Cargo rerun the build script
    # on EVERY invocation. Normalize only this owned snapshot, never sources;
    # empty directories contribute no bytes to the compiled source identity.
    watched_directories = {location / name for location in target_crates
                           for name in ('src', '.cargo')}
    for directory in watched_directories:
        if not directory.resolve().is_relative_to(snapshot_parent) or directory.is_symlink():
            raise EnginePreparationError('The storage build snapshot contains an unsafe path.')
        directory.mkdir(parents=True, exist_ok=True)
    for path, target in copies:
        if not target.resolve().is_relative_to(snapshot_parent) or target.is_symlink():
            raise EnginePreparationError('The storage build snapshot contains an unsafe path.')
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.name == _SYNTHETIC_WORKSPACE_SENTINEL:
            content = workspace_manifest.encode()
        else:
            content = path.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            target.write_bytes(content)
    # Removed inputs must disappear as well; otherwise the embedded source
    # identity includes obsolete Rust files and can never pass admission. Only
    # the owned snapshot trees are pruned: sibling Cargo target directories
    # (release/, update-build-*/) are caches, never snapshot inputs. A sibling
    # crate directory is owned only when it mirrors a live path dependency or
    # is itself a Cargo crate left from a removed dependency (identified by a
    # manifest), so build caches are never mistaken for stale sources.
    if needs_workspace:
        retained.add(snapshot.parent / 'Cargo.toml')
    owned_roots = list(target_crates)
    for entry in snapshot.parent.iterdir():
        if entry == snapshot or not entry.is_dir() or entry.is_symlink():
            continue
        if (entry / 'Cargo.toml').is_file():
            if entry not in owned_roots:
                owned_roots.append(entry)
    member_set = {str(member) for member in member_dirs}
    for owned in owned_roots:
        if not owned.is_dir():
            continue
        for directory, folders, names in os.walk(owned, topdown=False):
            for name in names:
                target = Path(directory) / name
                if target not in retained:
                    target.unlink()
            for name in folders:
                folder = Path(directory) / name
                if folder.is_symlink():
                    raise EnginePreparationError('The storage build snapshot contains an unsafe path.')
                if folder not in watched_directories and not any(folder.iterdir()):
                    folder.rmdir()
        if owned != snapshot and owned.name not in member_set and not any(owned.iterdir()):
            owned.rmdir()
    # A stale synthetic workspace root must disappear once no mirrored crate
    # needs one, so a later snapshot cannot inherit a phantom workspace.
    stale_root = snapshot.parent / 'Cargo.toml'
    if not needs_workspace and stale_root.is_file() and stale_root not in retained:
        stale_root.unlink()


def _declared_cargo_compiler(crate: Path) -> str | None:
    """The compiler this crate's own Cargo config pins, if any.

    Cargo applies ``[env]`` only to variables absent from its environment, so a
    synthesized ``CC`` silently outranks the package's declared pin. The build
    snapshot mirrors this config, so the source crate is the same authority.
    """
    for name in ('config.toml', 'config'):
        config = crate / '.cargo' / name
        if config.is_file():
            return _read_manifest(config).get('env', {}).get('CC') or None
    return None


# cc-rs passes -Wno-error=date-time for libmimalloc-sys, which gcc 4.8 cannot
# parse. A compiler that only exists fails this deep inside the C build.
_COMPILER_CAPABILITY_FLAG = '-Wdate-time'


def _c_compiler_accepts_date_time(compiler: str) -> bool:
    """Probe the flag libmimalloc-sys needs instead of trusting a path."""
    with tempfile.TemporaryDirectory(prefix='tofu-cc-probe-') as temporary:
        source = Path(temporary) / 'probe.c'
        source.write_text('int main(void) { return 0; }\n')
        try:
            completed = subprocess.run(
                [compiler, _COMPILER_CAPABILITY_FLAG, '-c', str(source),
                 '-o', str(Path(temporary) / 'probe.o')],
                capture_output=True, timeout=30,
                env=_compiler_probe_environment.get() or os.environ)
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0


def _resolve_c_compiler(search_path: str, declared: str | None) -> str:
    """Choose the compiler cargo itself would use, then any that can compile.

    The declared pin leads because it is the tree's own toolchain decision; the
    capability probe keeps an older distribution compiler from shadowing it.
    """
    candidates = [declared] if declared else []
    candidates.extend(found for found in (shutil.which(name, path=search_path)
                                          for name in ('cc', 'gcc', 'clang')) if found)
    unique = list(dict.fromkeys(candidates))
    for compiler in unique:
        if _c_compiler_accepts_date_time(compiler):
            return compiler
    if unique:
        rejected = ', '.join(unique)
        raise EnginePreparationError(
            'Source storage build requires a C compiler that accepts '
            f'{_COMPILER_CAPABILITY_FLAG} (cc-rs passes -Wno-error=date-time for '
            f'libmimalloc-sys), but none of {rejected} does. Install gcc 4.9 or '
            'newer, or provide a verified matching engine artifact. '
            'The existing server remains running.')
    raise EnginePreparationError(
        'Source storage build requires a C compiler (cc, gcc or clang) '
        'on the service PATH; no usable compiler was found. '
        'Install a compiler or provide a verified matching engine artifact.')


def _placed_build_cache(root: Path, environment: dict) -> Path | None:
    """This checkout's private compiler cache on the native-build placement, if selected."""
    from .local_placement import (
        PURPOSE_NATIVE_BUILD, choose_local_root, ensure_private_directory,
    )
    try:
        placement = choose_local_root(root, PURPOSE_NATIVE_BUILD, environ=environment)
        if placement.selected:
            project_key = hashlib.sha256(os.fsencode(root)).hexdigest()
            selected = Path(placement.root) / project_key
            ensure_private_directory(selected)
            _record_build_cache_checkout(selected, root)
            _retire_orphaned_build_caches(Path(placement.root), selected)
            return selected
    except OSError as exc:
        _report_progress(f'Local compiler cache unavailable: {exc}; using the checkout cache.')
    return None


# Names the checkout a placed compiler cache belongs to; the directory name is
# only a hash of that path.
_BUILD_CACHE_CHECKOUT = 'CHECKOUT'


def _record_build_cache_checkout(cache: Path, root: Path) -> None:
    marker = cache / _BUILD_CACHE_CHECKOUT
    if not marker.exists():
        staged = cache / f'.{_BUILD_CACHE_CHECKOUT}.{os.getpid()}'
        staged.write_text(str(root), encoding='utf-8')
        os.replace(staged, marker)


def _retire_orphaned_build_caches(placement_root: Path, current: Path) -> list[Path]:
    """Remove placed compiler caches whose checkout no longer exists.

    A checkout's cache is keyed by its path, so a removed checkout (a retired
    lane or data-root checkout) left its cache behind forever. Each cache names
    its checkout; the next build preparation of any checkout removes the caches
    of checkouts that are gone. A cache without that name (made before it was
    recorded) is kept, since nothing tells whose it is.
    """
    removed = []
    try:
        entries = list(placement_root.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry == current or not entry.is_dir() or entry.is_symlink():
            continue
        try:
            checkout = (entry / _BUILD_CACHE_CHECKOUT).read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if checkout and not os.path.lexists(checkout):
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    return removed


def _build_cache_root(root: Path, environment: dict) -> Path:
    """Place reconstructible compiler state through the common local policy.

    The checkout preparation lock owns this per-project tree. It contains no
    deployed engine or user database; a lost/unavailable cache only costs a
    rebuild. Isolated certification supplies its own build_root instead.
    """
    root = root.resolve()
    return _placed_build_cache(root, environment) or root / 'packages/tofu-db/target'


# Cache Directory Tagging Specification (https://bford.info/cachedir/): backup
# and cache tools skip a directory holding this file. Cargo writes one only into
# a target directory it creates itself.
CACHEDIR_TAG = ('Signature: 8a477f597d28d172789f06886806bc55\n'
                '# This file is a cache directory tag created by Tofu for a Cargo target.\n'
                '# For information about cache directory tags see https://bford.info/cachedir/\n')


def resolve_cargo(environment: Mapping[str, str], *,
                  root: Path | None = None) -> tuple[str | None, dict[str, str]]:
    """Locate Cargo; the single Rust toolchain discovery for Tofu-driven builds.

    Order: an explicit ``CARGO_HOME/bin`` (with its ``RUSTUP_HOME``), then
    ``cargo`` on ``PATH``, then ``HOME/.cargo/bin``. With ``root``, the
    checkout's ``.env`` supplies ``CARGO_HOME``/``RUSTUP_HOME`` that the
    environment does not already set (explicit process settings win). Returns
    the absolute cargo path (or None) and a copy of the environment carrying
    those toolchain roots, with the cargo directory prepended to ``PATH``.
    No host-specific installation path is ever searched (docs/DEFENSIVE_PATTERNS.md,
    "Artifact preparation resolves tools from the deployment environment").
    """
    resolved = dict(environment)
    if root is not None:
        from tofu_dotenv import read_dotenv_values
        declared = read_dotenv_values(Path(root) / '.env')
        for name in ('CARGO_HOME', 'RUSTUP_HOME'):
            if declared.get(name):
                resolved.setdefault(name, declared[name])
    search_path = resolved.get('PATH', os.defpath)
    user_home = Path(resolved.get('HOME') or resolved.get('USERPROFILE') or Path.home())
    cargo = None
    if resolved.get('CARGO_HOME'):
        cargo = shutil.which('cargo', path=str(Path(resolved['CARGO_HOME']).expanduser() / 'bin'))
    cargo = cargo or shutil.which('cargo', path=search_path) or shutil.which(
        'cargo', path=str(user_home / '.cargo' / 'bin'))
    if cargo:
        # Cargo invokes sibling rustc/rustup proxies by name, so a minimal
        # service PATH needs the proxy directory. Keep the proxy path (do not
        # resolve its symlink to rustup).
        cargo = os.path.abspath(cargo)
        resolved['PATH'] = os.pathsep.join((str(Path(cargo).parent), search_path))
    return cargo, resolved


def cargo_target_directory(root: Path, name: str,
                           environment: Mapping[str, str] | None = None) -> Path:
    """The Cargo target directory NAME for a checkout's own builds (test engines, benchmarks).

    It sits beside the checkout's placed compiler cache (:func:`_build_cache_root`,
    on the local scratch disk when the host has one), else in the checkout as
    ``packages/tofu-db/NAME``. The directory exists on return and carries a
    CACHEDIR.TAG, because the caller, not Cargo, creates it.
    """
    root = Path(root).resolve()
    placed = _placed_build_cache(root, dict(os.environ if environment is None else environment))
    target = placed / name if placed is not None else root / 'packages/tofu-db' / name
    if placed is None and not target.exists():
        _seed_cargo_target(root, name, target)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    tag = target / 'CACHEDIR.TAG'
    if not tag.exists():
        staged = target / f'.CACHEDIR.TAG.{os.getpid()}.new'
        staged.write_text(CACHEDIR_TAG, encoding='utf-8')
        os.replace(staged, tag)
    return target


# Not cloned into a seeded target: the checkout's own build writes its stable copy
# and build lock; a sibling's engine binary must never stand in for this checkout's.
_SEED_EXCLUDED_NAMES = frozenset({'stable', '.build.lock'})


def _sibling_cargo_targets(root: Path, name: str) -> list[Path]:
    """NAME targets of the other checkouts of ROOT's repository, most recently built first."""
    try:
        listing = subprocess.run(
            ['git', '-C', str(root), 'worktree', 'list', '--porcelain'],
            capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listing.stdout.splitlines():
        if not line.startswith('worktree '):
            continue
        checkout = Path(line[len('worktree '):])
        candidate = checkout / 'packages/tofu-db' / name
        try:
            if checkout.resolve() == root or not (candidate / 'debug').is_dir():
                continue
            found.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    return [candidate for _mtime, candidate in sorted(found, reverse=True)]


def _seed_cargo_target(root: Path, name: str, target: Path) -> bool:
    """Start a checkout's first build from a sibling checkout's target, sharing its extents.

    Every task checkout and lane on a local disk otherwise compiled the same
    dependencies into its own target (3.3 GB each). The seed is a copy-on-write
    clone (``cp --reflink=always``): it occupies no new blocks until a build
    rewrites them, and Cargo reuses each artifact whose fingerprint still
    holds. A volume without reflinks, or a sibling whose build lock is held
    (its files may be half written), leaves the target empty as before: the
    seed is never a full copy and never waits. Only the first creation seeds;
    the target then lives and goes with its checkout.
    """
    import fcntl

    for source in _sibling_cargo_targets(root, name):
        try:
            lock = open(source / '.build.lock', 'a+b')
        except OSError:
            continue
        with lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            staged = target.with_name(f'.{name}.seed-{os.getpid()}')
            shutil.rmtree(staged, ignore_errors=True)
            try:
                staged.mkdir(mode=0o700, parents=True)
                entries = [str(entry) for entry in source.iterdir()
                           if entry.name not in _SEED_EXCLUDED_NAMES]
                if entries:
                    subprocess.run(['cp', '-a', '--reflink=always', *entries, str(staged)],
                                   capture_output=True, check=True)
                os.rename(staged, target)
            except (OSError, subprocess.SubprocessError):
                shutil.rmtree(staged, ignore_errors=True)
                return False
            _report_progress(f'Cargo target {target} seeded from {source} (copy-on-write).')
            return True
    return False


def _cache_compiler_path(executable: str | None) -> str | None:
    """Unify directory aliases without renaming argv[0]-selected proxies.

    Rustup's cargo/rustc links must retain their names, and a distinct wrapper
    remains a distinct build input. Only existing executable parent aliases
    collapse; operator commands and unresolved paths keep their exact identity.
    """
    if not executable or not os.path.isabs(executable) or not os.path.isfile(executable):
        return executable
    path = Path(executable)
    return str(path.parent.resolve() / path.name)


def _cache_library_path(value: str) -> str:
    """Canonicalize directory aliases without changing linker search semantics."""
    entries = []
    for entry in value.split(os.pathsep):
        path = Path(entry)
        try:
            if path.is_absolute() and path.is_dir():
                entry = str(path.resolve())
        except OSError:
            pass
        entries.append(entry)
    return os.pathsep.join(entries)


def _build(root: Path, staging: Path, environment: dict, *, build_root: Path | None = None) -> Path:
    # A GUI/service launch need not inherit an interactive shell's PATH.
    # Resolve only from this deployment environment, including custom rustup
    # installations declared in .env; never borrow the caller's ambient PATH.
    build_environment = dict(environment)
    if build_root is None:
        # Browser/media setup augments the serving process's loader path. That
        # runtime search path is not a native toolchain declaration: it selects
        # incompatible host libraries and otherwise splits the compiler cache.
        # Resolve the declaration before probing the C compiler too, not only
        # before invoking Cargo. Isolated builds receive an explicit environment
        # and never read .env.
        from tofu_dotenv import read_dotenv_values
        declared = read_dotenv_values(root / '.env').get('LD_LIBRARY_PATH')
        inherited = build_environment.get('LD_LIBRARY_PATH')
        if declared is None:
            build_environment.pop('LD_LIBRARY_PATH', None)
        else:
            build_environment['LD_LIBRARY_PATH'] = declared
        if inherited != declared:
            _report_progress(
                'Using the project-declared native toolchain loader path instead of the '
                'serving runtime path. Set LD_LIBRARY_PATH in project .env to declare a '
                'custom linker environment.')
    cargo, toolchain_environment = resolve_cargo(environment)
    if not cargo:
        raise EnginePreparationError(
            'No matching engine download was available and Cargo was not found '
            'on the service PATH or in CARGO_HOME/bin (default: HOME/.cargo/bin). '
            'Configure CARGO_HOME and, for a relocated rustup installation, '
            'RUSTUP_HOME in the project .env, or install a matching release. '
            'The existing server remains running.')
    build_environment['PATH'] = toolchain_environment['PATH']
    # Containers can inherit compiler paths from a vanished host. Repair only
    # stale absolute overrides in the build child; retain usable user choices.
    for key in ('CC', 'CXX', 'HOST_CC', 'HOST_CXX'):
        value = build_environment.get(key, '')
        if os.path.isabs(value) and not (os.path.isfile(value) and os.access(value, os.X_OK)):
            _report_progress(f'Ignoring unavailable compiler override {key}={value}')
            build_environment.pop(key, None)
    if sys.platform == 'linux' and not build_environment.get('CC'):
        # A base image can ship gcc 4.8 beside the tree's declared newer pin.
        probe_token = _compiler_probe_environment.set(build_environment)
        try:
            build_environment['CC'] = _resolve_c_compiler(
                build_environment['PATH'], _declared_cargo_compiler(root / 'packages/tofu-db'))
        finally:
            _compiler_probe_environment.reset(probe_token)
    crate = root / 'packages/tofu-db'
    # Mirror the repository layout entirely inside the owned build tree.
    # External root inputs resolve two levels above the crate; putting the
    # crate directly in target would spill those inputs into retained source.
    target_root = build_root if build_root is not None else _build_cache_root(root, environment)
    snapshot_root = target_root / 'update-source-root'
    if snapshot_root.is_symlink() or (snapshot_root / 'packages').is_symlink():
        raise EnginePreparationError('The storage build snapshot must not use a symlink.')
    snapshot = snapshot_root / 'packages/tofu-db'
    _sync_build_snapshot(root, crate, snapshot)
    # Release optimization stays at opt-level=3. Cargo's false keeps local Thin
    # LTO across the fixed LLVM partitions without the cross-crate LTO pass.
    # Packaged release profiles remain owned by the package/release pipeline.
    # Explicit operator profile overrides remain authoritative; Cargo tracks
    # these settings in the stable host cache's own fingerprints.
    build_environment.setdefault('CARGO_PROFILE_RELEASE_CODEGEN_UNITS', str(RELEASE_CODEGEN_UNITS))
    build_environment.setdefault('CARGO_PROFILE_RELEASE_LTO', 'false')
    build_environment.setdefault('CARGO_PROFILE_RELEASE_INCREMENTAL', 'true')
    # Shared storage survives container replacement, compiled host programs do
    # not. Namespace Cargo cache by the current ABI and build environment.
    linker_identity = {
        key: (_cache_library_path(build_environment.get(key, ''))
              if key in ('LD_LIBRARY_PATH', 'LIBRARY_PATH')
              else build_environment.get(key, ''))
        for key in ('LD_LIBRARY_PATH', 'LIBRARY_PATH', 'RUSTFLAGS', 'CFLAGS', 'CXXFLAGS')
    }
    host_identity = json.dumps({
        'platform': platform.platform(), 'libc': platform.libc_ver(),
        'cargo': _cache_compiler_path(cargo),
        'compiler': _cache_compiler_path(build_environment.get('CC')),
        'linker_environment': linker_identity,
    }, sort_keys=True).encode()
    host_key = hashlib.sha256(host_identity).hexdigest()[:16]
    _report_progress(
        f'Storage engine compiler identity: cargo={_cache_compiler_path(cargo)}; '
        f'compiler={_cache_compiler_path(build_environment.get("CC"))}; '
        f'linkerSha256={hashlib.sha256(json.dumps(linker_identity, sort_keys=True).encode()).hexdigest()}')
    target_dir = target_root / f'update-build-{host_key}'
    build_environment['CARGO_TARGET_DIR'] = str(target_dir)
    # Explicit Cargo diagnostics can include host observations. Ordinary
    # restarts do not pay for a launch probe, especially on network volumes.
    # Observations never select jobs, codegen units or cache identity.
    if environment.get('CARGO_LOG') not in (None, '', 'off'):
        from runtime_guards import probe_system_resources
        try:
            observed_resources = probe_system_resources(environment)
            _report_progress(
                f'Compiler host observation: {observed_resources.effective_cpu_count} CPUs, '
                f'{observed_resources.effective_memory_available_mb} MiB available; '
                'release profile is independent of host capacity.')
        except (OSError, ValueError, RuntimeError) as exc:
            _report_progress(f'Compiler host observation unavailable: {exc}; continuing the build.')
    _report_stage('compiling', f'Compiling matching storage engine (build workers: {build_environment.get("CARGO_BUILD_JOBS", "Cargo default")}; '
                     f'cache: {target_dir}); '
                     'this may take several minutes. The running service has not been stopped.')
    command = [cargo, 'build', '--release', '--locked']
    if sys.platform == 'linux':
        command.extend(['--features', 'embedded-mimalloc'])
    # Spend the remaining preparation budget, retaining time for identity,
    # library verification and publication. Unused lock-wait time remains
    # available to compilation; neither acquisition nor work renews the clock.
    remaining = min(PREPARATION_TOTAL_TIMEOUT_SECONDS, _deadline.get() - time.monotonic())
    compile_timeout = max(0.0, remaining - BUILD_FINALIZATION_RESERVE_SECONDS)
    _run(command, cwd=snapshot,
         environment=build_environment, timeout=compile_timeout,
         progress='Compiling storage engine')
    return target_dir / 'release' / ('tofu-db.exe' if os.name == 'nt' else 'tofu-db')


def prepare_isolated(root: Path, output: Path, *, candidate: Path | None = None,
                     environment: dict | None = None) -> Path:
    """Certify into a new private directory without reading or syncing dotenv.

    No managed artifact/cache or user authority is touched. An explicit candidate
    fails closed; it is never silently replaced by a download or compilation.
    """
    root = root.resolve()
    output = output.absolute()
    if output.resolve().is_relative_to(root):
        raise EnginePreparationError('Isolated preparation output must be outside the source checkout.')
    expected = source_fingerprint(root)
    if expected is None:
        raise EnginePreparationError('Isolated preparation requires source certification.')
    # Exclusive creation refuses existing directories and symlinks, including
    # managed destinations. All persistent writes below belong to this call.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    environment = dict(os.environ if environment is None else environment)
    for key in ('TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256'):
        environment.pop(key, None)
    with _preparation_budget(PREPARATION_TOTAL_TIMEOUT_SECONDS):
        with tempfile.TemporaryDirectory(prefix='build-', dir=output) as temporary:
            staging = Path(temporary)
            selected = (candidate.expanduser().resolve() if candidate is not None
                        else _build(root, staging, environment, build_root=staging))
            verify_current(selected, root)
            if environment.get('TOFUDB_TRANSPORT') == 'embedded' and not (selected.parent / 'libtofu_db.so').is_file():
                raise EnginePreparationError('The selected embedded transport requires a matching engine library.')
            _validate_candidate(selected, root, environment)
            if source_fingerprint(root) != expected:
                raise EnginePreparationError('Engine source changed during preparation.')
            from scripts import deploy_tofudb_engine
            dest = output / ('tofu-db.exe' if os.name == 'nt' else 'tofu-db')
            deploy_tofudb_engine.deploy(argparse.Namespace(
                binary=str(selected), dest=str(dest), evidence=None,
                note='isolated source-identity and disposable semantic certification',
                env_file=None, no_env_sync=True, no_smoke=False, dry_run=False))
            verify_current(dest, root)
            return dest


def managed_engine_destination(root: Path, environment: dict) -> Path:
    """The deployed engine every preparation door decides about."""
    name = 'tofu-db.exe' if os.name == 'nt' else 'tofu-db'
    return Path(environment.get('TOFUDB_BIN')
                or Path(root) / f'packages/tofu-db/target/release/{name}').expanduser().resolve()


def prepare(root: Path, environment: dict | None = None, *,
            uncertified_override: UncertifiedEngineOverride | None = None) -> bool:
    """Return whether an artifact changed; reject failures before shutdown.

    Deploys HEAD's certified engine, keeps a contract-compatible deployed
    engine for an uncertified HEAD, or raises UncertifiedEngineRefusedError.
    Only ``uncertified_override`` builds or downloads an engine.
    """
    token = _deadline.set(min(_deadline.get(), time.monotonic() + PREPARATION_TOTAL_TIMEOUT_SECONDS))
    phase_token = _progress_phase.set(None)
    recovery_token = _progress_recovering.set(False)
    try:
        return _prepare(root, environment, uncertified_override)
    finally:
        _deadline.reset(token)
        _progress_phase.reset(phase_token)
        _progress_recovering.reset(recovery_token)


def _prepare(root: Path, environment: dict | None,
             uncertified_override: UncertifiedEngineOverride | None = None) -> bool:
    from tofu_dotenv import read_dotenv_values
    root = root.resolve()
    project_environment = read_dotenv_values(root / '.env')
    environment = {**project_environment, **(os.environ if environment is None else environment)}
    from runtime_guards import load_deployment_configuration
    if load_deployment_configuration(environment).storage_backend != 'tofudb':
        return False
    reconcile_project_pin(root, project_environment, environment)
    if not (root / 'packages/tofu-db/Cargo.toml').is_file():
        return False  # A packaged install's resolver verifies the bundled pin.
    name = 'tofu-db.exe' if os.name == 'nt' else 'tofu-db'
    dest = managed_engine_destination(root, environment)
    _report_stage('checking', 'Checking storage engine source identity and artifact pins.')
    # Lock wait consumes the total clock; unused queue time belongs to work.
    # Do not start a fresh work clock after acquisition.
    with _prepare_lock(root / 'packages/tofu-db/target/.engine-update.lock'):
        for attempt in range(1, SOURCE_PREPARATION_MAX_ATTEMPTS + 1):
            _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
            _require_storage_configuration(root, project_environment)
            try:
                # Queue time and previous attempts never authorize an older
                # generation. Keep the original configuration and total clock.
                expected = source_fingerprint(root)
                if expected is None:
                    raise EngineSourceChangedError('Storage source disappeared during preparation.')
                return _prepare_generation(root, environment, project_environment, dest, name, expected,
                                           uncertified_override)
            except EngineSourceChangedError as exc:
                _require_storage_configuration(root, project_environment)
                if attempt == SOURCE_PREPARATION_MAX_ATTEMPTS:
                    raise EnginePreparationError(
                        'Application files kept changing while restart was being prepared. '
                        'Automatic preparation retries were exhausted; restart was not started '
                        'and the current server is still running. Finish the application update '
                        'or source edits, then choose Restart again. No manual build or cache cleanup is needed.'
                    ) from exc
                _progress_recovering.set(True)
                _report_stage('retrying',
                    f'Application files changed during preparation. Automatically refreshing '
                    f'the engine (attempt {attempt + 1}/{SOURCE_PREPARATION_MAX_ATTEMPTS}); '
                    'the current server is still running. No action is needed.')
                time.sleep(_remaining_preparation_seconds(SOURCE_PREPARATION_SETTLE_SECONDS))


def _remaining_preparation_seconds(limit: float) -> float:
    remaining = _deadline.get() - time.monotonic()
    if remaining <= 0:
        raise EnginePreparationError(
            'Storage engine preparation timed out. Restart was not started and '
            'the current server is still running. Finish any application update '
            'or source edits, then choose Restart again; verified build caches are retained.')
    return min(limit, remaining)


def stage_seconds(standalone_seconds: float) -> float:
    """Bound one stage: the composed deadline inside a preparation, else its own limit.

    Child interpreters and engine processes start slowly on a loaded source
    volume; a private per-stage cap inside a preparation refused healthy
    engines while most of the composed restart budget remained. Standalone
    callers (boot admission, operator tools) have no outer deadline and keep
    their fixed liveness bound.
    """
    if _deadline.get() == float('inf'):
        return standalone_seconds
    return _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)


def _require_storage_configuration(root: Path, project_environment: dict) -> None:
    from tofu_dotenv import read_dotenv_values
    current = read_dotenv_values(root / '.env')
    if any(current.get(key) != project_environment.get(key) for key in (
            'TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256', 'TOFU_PERSONAL_STORAGE_BACKEND',
            'TOFU_DEPLOYMENT_MODE', 'TOFUDB_TRANSPORT')):
        raise EnginePreparationError(
            'Storage configuration changed during preparation. Restart was not started '
            'and the current server is still running. Review the new configuration, then choose Restart again.')


def _require_source_generation(root: Path, expected: str) -> None:
    if source_fingerprint(root) != expected:
        raise EngineSourceChangedError('Application source changed during engine preparation.')


def _verify_generation(binary: Path, root: Path, expected: str) -> None:
    # Compare with the captured generation, not an uncoordinated second hash.
    # Live-source drift is checked separately before any publication.
    if build_identity(binary)['source_sha256'] != expected:
        _require_source_generation(root, expected)
        raise EnginePreparationError(
            'Storage engine does not match the prepared application source. '
            'The current server is still running; candidate publication was cancelled.')


def _prepare_generation(root: Path, environment: dict, project_environment: dict,
                        dest: Path, name: str, expected: str,
                        uncertified_override: UncertifiedEngineOverride | None = None) -> bool:
    try:
        _verify_generation(dest, root, expected)
        _verify_pin(dest, environment)
        if (environment.get('TOFUDB_TRANSPORT') == 'embedded'
                or (environment.get('TOFUDB_TRANSPORT', 'auto') == 'auto'
                    and (dest.parent / 'libtofu_db.so').exists())):
            verify_library(dest)
    except EngineSourceChangedError:
        raise
    except (EnginePreparationError, OSError):
        pass  # Missing/old engines are the repair trigger, not success.
    else:
        _require_source_generation(root, expected)
        from serverctl_pkg import store_features
        store_features.require_readable(dest, store_features.data_dir_for(root), door='restart preparation')
        _require_storage_configuration(root, project_environment)
        _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
        _report_stage('cached', 'Storage engine already matches; no download or build needed.')
        _report_current_engine(root, expected)
        return False
    if not dest.is_relative_to(root / MANAGED_ENGINE_ROOT):
        raise EnginePreparationError('The explicitly configured external storage engine is incompatible. Select the managed engine in the application configuration before updating.')
    if (environment.get('TOFUDB_EXPECTED_SHA256')
            and environment['TOFUDB_EXPECTED_SHA256'] != project_environment.get('TOFUDB_EXPECTED_SHA256')):
        raise EnginePreparationError('An external storage engine pin overrides the application configuration. Update that override before preparing a different engine.')
    contract = storage_contract_sha256(root)
    from serverctl_pkg.engine_certification import passing_record
    record, record_problem = passing_record(root, expected, contract)
    if record is not None:
        return _deploy_certified(root, environment, project_environment, dest, expected, contract, record)
    if uncertified_override is None:
        decision = _uncertified_head_decision(root, environment, dest, expected, contract, record_problem)
        _require_source_generation(root, expected)
        _require_storage_configuration(root, project_environment)
        if decision['decision'] != 'kept':
            _report_decision(decision, announce=False)  # The raised refusal carries the message.
            raise UncertifiedEngineRefusedError(decision['message'], decision)
        _report_decision(decision)
        _report_stage('cached', 'Keeping the deployed storage engine; nothing was downloaded or built.')
        return False
    return _deploy_uncertified(root, environment, project_environment, dest, name, expected,
                               contract, uncertified_override)


def _report_current_engine(root: Path, expected: str) -> None:
    from serverctl_pkg.engine_certification import passing_record
    try:
        record, problem = passing_record(root, expected, storage_contract_sha256(root))
    except EnginePreparationError as exc:
        record, problem = None, f'its storage contract could not be read: {exc}'
    state = f'is the {record.summary()}' if record is not None else f'is uncertified: {problem}'
    _report_decision({'decision': 'current', 'headSourceSha256': expected, 'certified': record is not None,
                      'message': f"The deployed storage engine is HEAD's ({expected[:12]}) and {state}."})


def _finish_publication(root: Path, project_environment: dict, dest: Path, expected: str,
                        deployment: dict) -> None:
    try:
        _verify_generation(dest, root, expected)
        _require_source_generation(root, expected)
    except EngineSourceChangedError as exc:
        # Publication has already written managed pins. Never restart
        # the preparation loop with the original configuration now.
        raise EnginePreparationError(
            'Application files changed during final engine publication. '
            'Restart was not started and the current server is still running. '
            'The engine was published, but activation was cancelled. Finish the '
            'application update or source edits, then choose Restart again.'
        ) from exc
    published_environment = dict(project_environment)
    if 'TOFUDB_EXPECTED_SHA256' in published_environment and deployment['env_file']:
        published_environment['TOFUDB_EXPECTED_SHA256'] = deployment['binary_sha256']
    _require_storage_configuration(root, published_environment)
    _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)


def _deploy_certified(root: Path, environment: dict, project_environment: dict, dest: Path,
                      expected: str, contract: str, record) -> bool:
    """Publish exactly the certified bytes for HEAD's engine source; nothing is built."""
    if record.library_sha256 is None and (
            environment.get('TOFUDB_TRANSPORT') == 'embedded'
            or (environment.get('TOFUDB_TRANSPORT', 'auto') == 'auto'
                and (dest.parent / 'libtofu_db.so').is_file())):
        raise EnginePreparationError(
            f'The selected embedded transport requires an engine library, but the {record.summary()} '
            'has none. Certify an embedded engine pair; the current server is still running.')
    _require_source_generation(root, expected)
    _require_storage_configuration(root, project_environment)
    _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
    _report_stage('publishing', f'Deploying the {record.summary()}.')
    from scripts import deploy_tofudb_engine
    deployment = deploy_tofudb_engine.deploy(argparse.Namespace(
        binary=str(record.binary), dest=str(dest), evidence=str(record.certification_file),
        note=record.summary(), env_file=str(root / '.env'), no_env_sync=False, no_smoke=False,
        dry_run=False), storage_contract_sha256=contract)
    _finish_publication(root, project_environment, dest, expected, deployment)
    _report_decision({'decision': 'deployed-certified', 'headSourceSha256': expected,
                      'headStorageContractSha256': contract,
                      'deployedBinarySha256': deployment['binary_sha256'],
                      'certificationRecord': str(record.directory),
                      'message': f'Deployed the {record.summary()} for HEAD.'})
    _report_stage('prepared', 'Certified storage engine deployed; restart can now activate it.')
    return True


def _deploy_uncertified(root: Path, environment: dict, project_environment: dict, dest: Path,
                        name: str, expected: str, contract: str,
                        override: UncertifiedEngineOverride) -> bool:
    """The operator override: reuse, download or compile HEAD's engine, then publish it."""
    _report_progress(f'Operator override {override.decision_id} by {override.operator}: preparing '
                     f"HEAD's uncertified storage engine {expected[:12]} ({override.reason}).")
    with tempfile.TemporaryDirectory(prefix='.engine-update-', dir=dest.parent if dest.parent.exists() else root / 'packages/tofu-db/target') as temporary:
        staging = Path(temporary)
        candidate = None
        requires_library = (environment.get('TOFUDB_TRANSPORT') == 'embedded'
                            or (environment.get('TOFUDB_TRANSPORT', 'auto') == 'auto'
                                and (dest.parent / 'libtofu_db.so').is_file()))
        # A developer build or a timed-out publication may already have
        # produced the exact engine. Reuse it before any network/compiler
        # work, but retain source, semantic and deployment verification.
        cache_roots = dict.fromkeys((_build_cache_root(root, environment), root / 'packages/tofu-db/target'))
        host_cached = [candidate for cache in cache_roots
                       for candidate in sorted(cache.glob(f'update-build-*/release/{name}'))]
        for cached in (*host_cached,
                       root / f'packages/tofu-db/target/update-build/release/{name}',
                       root / f'packages/tofu-db/target/release/{name}'):
            if cached.resolve() == dest:
                continue
            try:
                # Obsolete cache entries are ordinary misses. Rehash the live
                # tree once after selection, not once for every old host cache.
                if build_identity(cached)['source_sha256'] != expected:
                    continue
                if requires_library and not _cached_library_matches_source(cached, expected):
                    continue
                candidate = cached
                _report_stage('cached', 'Reusing a matching locally compiled storage engine.')
                break
            except EngineSourceChangedError:
                raise
            except (OSError, ValueError, EnginePreparationError):
                continue
        if candidate is None:
            try:
                candidate = _prebuilt(root, staging, environment)
                if candidate is not None:
                    _verify_generation(candidate, root, expected)
                    if requires_library and not (candidate.parent / 'libtofu_db.so').is_file():
                        candidate = None
            except EngineSourceChangedError:
                raise
            except (OSError, ValueError, EnginePreparationError) as exc:
                _report_progress(f'Matching engine download unavailable: {exc}. Trying local build.')
                candidate = None
        _require_source_generation(root, expected)
        if candidate is None:
            candidate = _build(root, staging, environment)
        _require_storage_configuration(root, project_environment)
        _require_source_generation(root, expected)
        _verify_generation(candidate, root, expected)
        if (requires_library
                and not (candidate.parent / 'libtofu_db.so').is_file()):
            raise EnginePreparationError('The selected embedded transport requires a matching engine library; candidate publication was cancelled.')
        _report_stage('validating', 'Validating storage engine against a disposable database.')
        try:
            _validate_candidate(candidate, root, environment)
        except EngineCommandFailedError:
            # The probe's fresh supervisor also checks live source identity.
            # Recover an observed source change, not unchanged-source semantic
            # refusals, timeouts or cancellation (which have different types).
            _require_storage_configuration(root, project_environment)
            _require_source_generation(root, expected)
            raise
        _require_source_generation(root, expected)
        if storage_contract_sha256(root) != contract:
            raise EngineSourceChangedError('Storage contracts changed during engine preparation.')
        _require_storage_configuration(root, project_environment)
        _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
        # Existing owner retains pin publication, rollback binary and dotenv.
        _report_stage('publishing', 'Publishing the uncertified storage engine and artifact pins.')
        from scripts import deploy_tofudb_engine
        deployment = deploy_tofudb_engine.deploy(argparse.Namespace(
            binary=str(candidate), dest=str(dest), evidence=None,
            note=override.certification_note(),
            env_file=str(root / '.env'), no_env_sync=False, no_smoke=False, dry_run=False),
            storage_contract_sha256=contract)
        _finish_publication(root, project_environment, dest, expected, deployment)
        try:
            _audit('storage_engine.uncertified_deployed', decision_id=override.decision_id,
                   operator=override.operator, reason=override.reason, source_sha256=expected,
                   binary_sha256=deployment['binary_sha256'],
                   previous_binary_sha256=deployment.get('previous_binary_sha256'),
                   storage_contract_sha256=contract, dest=str(dest))
        except (ImportError, OSError, ValueError) as exc:
            _report_progress(f'WARNING: the uncertified deployment was not written to audit.log ({exc}); '
                             f'{dest.parent / "build.json"} records it.')
        _report_decision({'decision': 'deployed-uncertified', 'headSourceSha256': expected,
                          'headStorageContractSha256': contract,
                          'deployedBinarySha256': deployment['binary_sha256'],
                          'overrideDecisionId': override.decision_id,
                          'message': (f"Deployed HEAD's UNCERTIFIED storage engine {expected[:12]} by operator "
                                      f'override {override.decision_id} ({override.operator}).')})
        _report_stage('prepared', 'Uncertified storage engine deployed by operator override; restart can now activate it.')
        return True


def _validate_candidate(candidate: Path, root: Path, environment: dict) -> None:
    if (candidate.parent / 'libtofu_db.so').is_file():
        expected = build_identity(candidate)['source_sha256']
        _verify_library_source(candidate, expected)
    # Probe only a disposable authority. The probe explicitly clears inherited
    # connection locators and pins the exact candidate before launching it.
    # Its cold storage import and source rehash scale with source-volume load,
    # so only the composed preparation deadline bounds it (restart_budget.py).
    _run([sys.executable, str(root / 'scripts/check_tofudb_engine_drift.py'),
          '--binary', str(candidate), '--json'], cwd=root,
         environment=environment, timeout=PREPARATION_TOTAL_TIMEOUT_SECONDS,
         progress='Validating storage engine')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--prepare-only', type=Path, metavar='NEW_DIRECTORY',
                        help='prepare isolated test artifacts; never read or sync .env')
    parser.add_argument('--candidate', type=Path, help='certify this exact candidate without building')
    decision = parser.add_mutually_exclusive_group()
    decision.add_argument('--deploy-uncertified-engine', metavar='REASON',
                          help="operator decision, audited: download or build HEAD's uncertified "
                               'engine and deploy it this once')
    # The lifecycle manager's hop for a decision serverctl already recorded.
    decision.add_argument('--uncertified-engine-override', metavar='JSON', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.candidate and not args.prepare_only:
        parser.error('--candidate requires --prepare-only')
    if args.prepare_only and (args.deploy_uncertified_engine is not None or args.uncertified_engine_override):
        parser.error('isolated preparation never deploys, so it takes no uncertified engine override')
    if os.name != 'nt':
        def interrupted(_signal, _frame):
            raise EnginePreparationCancelledError('Storage engine preparation was cancelled.')
        signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.prepare_only:
            print(prepare_isolated(args.project, args.prepare_only, candidate=args.candidate))
        else:
            override = None
            if args.uncertified_engine_override:
                override = UncertifiedEngineOverride.from_payload(args.uncertified_engine_override)
            elif args.deploy_uncertified_engine is not None:
                override = record_uncertified_override(
                    args.deploy_uncertified_engine, door='serverctl_pkg.native_engine')
            prepare(args.project, uncertified_override=override)
        return 0
    except KeyboardInterrupt:
        _report_progress('Storage engine preparation cancelled; the running service has not been stopped.')
        return 130
    except (OSError, ValueError, EnginePreparationError, EnginePreparationCancelledError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
