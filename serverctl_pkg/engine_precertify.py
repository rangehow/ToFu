"""Certify HEAD's storage engine in the background, ahead of the next restart.

Responsibility: once an engine-input change lands, run the very gate a
restart would run lazily (``native_engine.certify_head_before_start``: the
same ``scripts/engine_certify.py`` run, fixture choice and records) in a
detached child, so the restart finds HEAD's passing record instead of
blocking on the gate. One gate runs per checkout (``.gate.lock``); a restart
that arrives meanwhile waits for that run and reuses its record. A run
certifies the HEAD at its start; when HEAD moves during a run, the next
trigger after it finishes certifies the new HEAD (no queue).

Entry points: :func:`pending_certification` (cheap: HEAD's engine identity
is cached per engine-input tree ids), :func:`start_background` (called by
the lifecycle manager's monitor when HEAD changes; never raises) and
``python -m serverctl_pkg.engine_precertify --run --project ROOT`` (the
child; without ``--run`` it prints what is pending and the last run's status).

Dependencies: ``native_engine`` (identity, gate lock, the gate itself),
``engine_certification`` (records; read-only), ``tofu_dotenv`` and
``runtime_guards`` (backend selection). Writes only reconstructible files:
``packages/tofu-db/target/engine-certifications/{background.json,
precertify-identity.json}`` and ``logs/engine-precertify.log``.
Contract: docs/NATIVE_STORAGE_LIFECYCLE.md, "Certifying ahead of the restart".
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from serverctl_pkg import native_engine

IDENTITY_CACHE = 'packages/tofu-db/target/engine-certifications/precertify-identity.json'
LOG_FILE = 'logs/engine-precertify.log'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _log(root: Path, message: str) -> None:
    path = root / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f'{_now()} [engine-precertify pid={os.getpid()}] {message}\n')


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f'.{path.name}.{os.getpid()}')
    staging.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(staging, path)


def _git(root: Path, *argv: str) -> str:
    return native_engine._run(['git', '-C', str(root), *argv], cwd=root, environment=dict(os.environ),
                              timeout=native_engine.stage_seconds(30), failure_subject=f'git {argv[0]}')


def _input_tree_key(root: Path, paths: list[str]) -> str:
    """Digest of HEAD's tree entries for the engine inputs: changes exactly when a committed input does."""
    return hashlib.sha256(_git(root, 'ls-tree', '--full-tree', 'HEAD', '--', *paths).encode()).hexdigest()


def _head_identity(root: Path) -> tuple[str, str, str] | None:
    """(HEAD commit, engine source fingerprint, storage contract), or None when engine inputs are uncommitted.

    The fingerprint is reused while HEAD's tree entries for the input paths
    are unchanged and the working copy matches them; a manifest edit that adds
    an input changes a listed crate's tree id, so the cache never goes stale.
    """
    commit = _git(root, 'rev-parse', '--verify', 'HEAD').strip()
    cache_path = root / IDENTITY_CACHE
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
        paths = cached['paths']
        if _input_tree_key(root, paths) == cached['key']:
            if native_engine.uncommitted_engine_inputs(root, paths):
                return None
            return commit, cached['sourceSha256'], cached['contractSha256']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    paths = native_engine.engine_input_paths(root)
    if native_engine.uncommitted_engine_inputs(root, paths):
        return None
    key = _input_tree_key(root, paths)
    source = native_engine.source_fingerprint(root)
    contract = native_engine.storage_contract_sha256(root)
    _write_json(cache_path, {'paths': paths, 'key': key, 'commit': commit,
                             'sourceSha256': source, 'contractSha256': contract})
    return commit, source, contract


def pending_certification(root: Path) -> dict | None:
    """What HEAD's engine needs certified before a restart could deploy or keep one, else None.

    None exactly when a background run has nothing to certify: not TofuDB, no
    engine crate, an unmanaged engine, the deployed engine is HEAD's, HEAD has
    a passing record, or engine inputs are uncommitted. A deployed engine that
    a start would keep for HEAD's contract still leaves HEAD pending: without
    HEAD's record no start ever deploys HEAD's engine.
    """
    from tofu_dotenv import read_dotenv_values
    from runtime_guards import load_deployment_configuration
    from serverctl_pkg.engine_certification import passing_record
    root = Path(root).resolve()
    environment = {**read_dotenv_values(root / '.env'), **os.environ}
    if (load_deployment_configuration(environment).storage_backend != 'tofudb'
            or not (root / 'packages/tofu-db/Cargo.toml').is_file()):
        return None
    dest = native_engine.managed_engine_destination(root, environment)
    if not dest.is_relative_to(root / native_engine.MANAGED_ENGINE_ROOT):
        return None
    identity = _head_identity(root)
    if identity is None or identity[1] is None:
        return None
    commit, source, contract = identity
    try:
        if native_engine.build_identity(dest)['source_sha256'] == source:
            return None
    except (OSError, native_engine.EnginePreparationError):
        pass
    if passing_record(root, source, contract)[0] is not None:
        return None
    return {'commit': commit, 'sourceSha256': source, 'contractSha256': contract}


def start_background(root: Path, python: str) -> dict:
    """Launch a detached background gate run when HEAD's engine needs one; never raises."""
    root = Path(root).resolve()
    try:
        with native_engine.certification_gate_lock(root, blocking=False) as acquired:
            if not acquired:
                return {'started': False, 'reason': 'an engine gate run is already in progress',
                        'status': native_engine.background_certification_status(root)}
        pending = pending_certification(root)
        if pending is None:
            return {'started': False, 'reason': "HEAD's storage engine needs no certification"}
        log = root / LOG_FILE
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('ab') as output:
            child = subprocess.Popen(
                [python, '-m', 'serverctl_pkg.engine_precertify', '--run', '--project', str(root)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
        return {'started': True, 'reason': f"certifying HEAD's storage engine {pending['sourceSha256'][:12]}",
                'pid': child.pid, 'log': str(log), **pending}
    except Exception as exc:  # The manager's monitor must never fail on an optimization.
        reason = f'cannot start background engine certification: {type(exc).__name__}: {exc}'
        try:
            _log(root, reason)
        except OSError:
            pass
        return {'started': False, 'reason': reason}


def run(root: Path) -> int:
    """The child: certify HEAD under the gate lock and record the outcome in background.json."""
    from serverctl_pkg.engine_certification import passing_record
    root = Path(root).resolve()
    with native_engine.certification_gate_lock(root, blocking=False) as acquired:
        if not acquired:
            _log(root, 'another engine gate run holds the lock; nothing to do')
            return 0
        pending = pending_certification(root)
        if pending is None:
            _log(root, "HEAD's storage engine needs no certification; nothing to do")
            return 0
        status_path = root / native_engine.BACKGROUND_CERTIFICATION_STATUS
        status = {'commit': pending['commit'], 'sourceSha256': pending['sourceSha256'],
                  'contractSha256': pending['contractSha256'], 'startedAt': _now(), 'finishedAt': None,
                  'ok': None, 'message': 'running', 'pid': os.getpid()}
        _write_json(status_path, status)
        _log(root, f"start: certifying engine {pending['sourceSha256'][:12]} commit {pending['commit'][:12]} "
                   f"contract {pending['contractSha256'][:12]}")
        try:
            native_engine.certify_head_before_start(root, certify_kept_engine=True)
            record, problem = passing_record(root, pending['sourceSha256'], pending['contractSha256'])
            ok = record is not None
            message = record.summary() if ok else f'the gate finished but {problem}'
        except Exception as exc:  # background.json must never claim a finished run is still running
            ok, message = False, f'{type(exc).__name__}: {exc}'
        status.update(finishedAt=_now(), ok=ok, message=message)
        _write_json(status_path, status)
        _log(root, f'end: ok={ok} {message}')
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--run', action='store_true', help='run the gate now under the gate lock (the child)')
    args = parser.parse_args()
    if args.run:
        return run(args.project)
    print(json.dumps({'pending': pending_certification(args.project),
                      'background': native_engine.background_certification_status(args.project)},
                     indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
