#!/usr/bin/env python3
"""Lint gates for changed files: ruff for Python, rustfmt and clippy for gated Rust packages.

Entry point: main ([PATH ...] | --base REF). Paths are repository-relative
changed files; --base REF compares REF...HEAD. scripts/test_select.py --run
calls this before its tests, so the configured integration gate
(TOFU_INTEGRATION_TEST_CMD) and the linear checkpoint gate refuse lint drift;
`make static-check` runs it by hand. Ruff runs at the uv.lock pin with the
repository's ruff.toml (a checkout whose uv.lock pins no ruff has no Python
lint to apply). A Rust package in RUST_GATED_PACKAGES gets
`cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` whenever
one of its sources changes; Cargo resolves through
serverctl_pkg.native_engine.resolve_cargo. A missing tool fails the gate: an
unchecked file is reported, never passed. Policy: docs/TESTING_STRATEGY.md,
"Lint gates".
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUST_GATED_PACKAGES = ('packages/tofu-db', 'packages/tofu-contracts', 'packages/tofu-front-rs/push',
                       'packages/tofu-mcp', 'packages/tofu-channel-stdio',
                       'packages/tofu-manager', 'packages/tofu-search/rust',
                       'packages/tofu-xtask', 'packages/tofu-storage-client', 'packages/tofu-node',
                       'packages/tofu-sql-authority')
_RUST_INPUT_NAMES = frozenset({'Cargo.toml', 'Cargo.lock', 'build.rs', 'clippy.toml',
                               'rustfmt.toml', '.rustfmt.toml'})


class GateUnavailable(RuntimeError):
    pass


def changed_since(base: str) -> list[str]:
    revision = subprocess.run(
        ['git', 'rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}'],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    out = subprocess.run(['git', 'diff', '--name-only', '--no-renames', '-z', f'{revision}...HEAD', '--'],
                         cwd=ROOT, capture_output=True, text=True, check=True, timeout=30).stdout
    return sorted(set(filter(None, out.split('\0'))))


def python_targets(changed: list[str]) -> list[str]:
    return [path for path in changed if path.endswith('.py') and (ROOT / path).is_file()]


def rust_targets(changed: list[str]) -> list[str]:
    selected = []
    for package in RUST_GATED_PACKAGES:
        for path in changed:
            if path.startswith(package + '/') and (
                    path.endswith('.rs') or Path(path).name in _RUST_INPUT_NAMES):
                selected.append(package)
                break
    return selected


def ruff_pin(root: Path) -> str | None:
    """The ruff version uv.lock pins; None when the checkout declares no ruff."""
    lock = root / 'uv.lock'
    if not lock.is_file():
        return None
    match = re.search(r'^name = "ruff"\nversion = "([^"]+)"$', lock.read_text(encoding='utf-8'),
                      re.MULTILINE)
    return match.group(1) if match else None


def _reports_version(command: list[str], version: str) -> bool:
    try:
        out = subprocess.run([*command, '--version'], capture_output=True, text=True, timeout=60)
    except OSError:
        return False
    return out.returncode == 0 and out.stdout.split()[-1:] == [version]


def ruff_command(version: str) -> list[str]:
    """The pinned ruff: rule sets differ between releases, so another version proves nothing."""
    for command in ([sys.executable, '-m', 'ruff'], [shutil.which('ruff') or 'ruff']):
        if _reports_version(command, version):
            return command
    uv = shutil.which('uv')
    if uv:
        return [uv, 'tool', 'run', '--from', f'ruff=={version}', 'ruff']
    raise GateUnavailable(f'ruff {version} is unavailable: install the dev extra or uv')


def common_checkout(root: Path) -> Path:
    """The checkout owning this worktree's Git common directory (itself when it is the main one)."""
    out = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                         cwd=root, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    common = Path(out)
    return common.parent if common.name == '.git' else root


def _run(label: str, command: list[str], **kwargs) -> bool:
    print(f'[static-gates] $ {" ".join(command)}', flush=True)
    code = subprocess.run(command, **kwargs).returncode
    print(f'[static-gates] {label}: {"passed" if code == 0 else "FAILED"}', flush=True)
    return code == 0


def run_gates(changed: list[str]) -> int:
    python_files = python_targets(changed)
    packages = rust_targets(changed)
    if not python_files and not packages:
        print('[static-gates] no Python or gated Rust sources changed')
        return 0
    passed = True
    try:
        version = ruff_pin(ROOT) if python_files else None
        if python_files and version is None:
            print('[static-gates] uv.lock declares no ruff: Python lint does not apply')
        elif python_files:
            ruff = ruff_command(version)
            passed &= _run(f'ruff ({len(python_files)} file(s))',
                           [*ruff, 'check', '--force-exclude', '--', *python_files], cwd=ROOT)
        if packages:
            sys.path.insert(0, str(ROOT))
            from serverctl_pkg.native_engine import cargo_target_directory, resolve_cargo
            # Task and gate worktrees lack the deployment's ignored .env; the
            # shared checkout declares the toolchain and keeps the cache warm.
            shared = common_checkout(ROOT)
            cargo, environment = resolve_cargo(os.environ, root=shared)
            if cargo is None:
                raise GateUnavailable('cargo is unavailable for ' + ', '.join(packages))
            environment['CARGO_TARGET_DIR'] = str(
                cargo_target_directory(shared, 'target-clippy', environment))
            for package in packages:
                manifest = str(ROOT / package / 'Cargo.toml')
                passed &= _run(f'rustfmt {package}',
                               [cargo, 'fmt', '--check', '--manifest-path', manifest],
                               cwd=ROOT, env=environment)
                passed &= _run(f'clippy {package}',
                               [cargo, 'clippy', '--locked', '--all-targets', '--manifest-path',
                                manifest, '--', '-D', 'warnings'],
                               cwd=ROOT, env=environment)
    except GateUnavailable as error:
        print(f'[static-gates] FAILED: {error}', flush=True)
        return 2
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args(argv)
    if args.base and args.paths:
        parser.error('give --base or paths, not both')
    return run_gates(changed_since(args.base) if args.base else sorted(set(args.paths)))


if __name__ == '__main__':
    raise SystemExit(main())
