"""Container and frontend-CI build boundaries must fail loudly."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding='utf-8')


def test_frontend_ci_uses_lockfile_without_fallback():
    workflow = _read('.github/workflows/ci.yml')
    assert 'run: npm ci\n' in workflow
    assert 'npm ci || npm install' not in workflow


def _frontend_job() -> str:
    workflow = _read('.github/workflows/ci.yml')
    start = workflow.index('\n  frontend:')
    end = workflow.index('\n  supply-chain:')
    return workflow[start:end]


def test_frontend_ci_installs_python_tools_before_contract_checks():
    job = _frontend_job()
    sync = job.index('uv sync --frozen --extra test')
    path = job.index('GITHUB_PATH')
    check = job.index('npm run check:frontend')
    assert 0 <= sync < path < check


def _dependency_stage() -> str:
    return _read('Dockerfile').split(
        'FROM ${BASE_IMAGE} AS python-dependencies', 1)[1].split('FROM ', 1)[0]


def test_docker_copies_workspace_members_before_frozen_sync():
    deps = _dependency_stage()
    sync = deps.index('uv sync --frozen')
    assert 0 <= deps.index('COPY packages/tofu-search') < sync
    assert 0 <= deps.index('COPY plugins/tofu-trading') < sync


def test_docker_dependency_stage_installs_the_toolchain_the_kernel_builds_with():
    """`tofu-repo-kernel` compiles its Rust helpers, so the stage needs cargo.

    The kernel wheel is built in this stage, and its ``setup.py`` shells out to
    ``cargo build``. Its gix discovery crate declares a ``rust-version``, so the
    stage installs the exact toolchain pinned by the native release builds
    through a checksum-verified ``rustup-init`` instead of a distribution rustc.
    """
    import re
    import tomllib

    deps = _dependency_stage()
    packages = deps[deps.index('apk add'):deps.index('case ')]
    installed = set(packages.replace('\\', ' ').replace('&&', ' ').split())
    assert {'build-base', 'curl', 'ca-certificates-bundle', 'python-3.12'} <= installed
    assert 'rustc' not in installed and 'rust' not in installed

    hashes = re.findall(r'rustup_sha256=([0-9a-f]{64})', deps)
    assert len(hashes) == 2, 'amd64 and arm64 rustup-init must each be checksum-pinned'
    assert deps.index('sha256sum -c') < deps.index('/tmp/rustup-init -y')

    toolchain = re.search(r'--default-toolchain (\d+\.\d+\.\d+)', deps)
    assert toolchain, 'the stage must install an exact Rust toolchain'
    desktop = _read('.github/workflows/build-desktop.yml')
    assert f"RUSTUP_TOOLCHAIN: '{toolchain.group(1)}'" in desktop
    discovery = tomllib.loads(_read('packages/tofu-repo-kernel/rust/discovery/Cargo.toml'))
    required = tuple(int(part) for part in discovery['package']['rust-version'].split('.'))
    assert tuple(int(part) for part in toolchain.group(1).split('.')) >= required
    # The kernel is built in this stage, so the toolchain must precede it.
    assert deps.index('cargo --version') < deps.index('uv build --wheel --out-dir /native-wheelhouse')


def test_docker_uv_pin_tracks_the_committed_lock():
    """A stale uv pin re-installs the release-gated version into the image."""
    import re

    deps = _dependency_stage()
    pinned = re.search(r'pip install --no-cache-dir uv==(\d+\.\d+\.\d+)', deps)
    assert pinned, 'the dependency stage must pin uv'
    import tomllib

    packages = tomllib.loads(_read('uv.lock'))['package']
    resolved = {p['name']: p['version'] for p in packages if p['name'] == 'uv'}
    assert resolved, 'uv.lock must resolve uv'
    locked = resolved['uv']
    assert pinned.group(1) == locked, (
        f'Dockerfile pins uv {pinned.group(1)} but uv.lock resolves {locked}'
    )


def test_docker_playwright_install_fails_loudly():
    dockerfile = _read('Dockerfile')
    worker = dockerfile.split('FROM runtime-base AS worker', 1)[1].split('\nFROM ', 1)[0]
    install = worker[worker.index('RUN '):worker.index('USER ')]
    assert 'python -m playwright install chromium --only-shell' in install
    assert '|| true' not in install and '2>/dev/null' not in install
    # `--with-deps` drives apt-get, which the Wolfi base does not have.
    assert '--with-deps' not in install


def test_worker_installs_every_library_the_chromium_headless_shell_links():
    """The sonames are the ELF NEEDED set of Playwright's headless shell.

    Measured with ``readelf -d`` on chromium_headless_shell-1234 (Playwright
    1.62.0); the C runtime sonames come from glibc/libgcc already present.
    """
    dockerfile = _read('Dockerfile')
    worker = dockerfile.split('FROM runtime-base AS worker', 1)[1].split('\nFROM ', 1)[0]
    linked = {
        'libasound.so.2', 'libatk-1.0.so.0', 'libatk-bridge-2.0.so.0', 'libatspi.so.0',
        'libdbus-1.so.3', 'libexpat.so.1', 'libgbm.so.1', 'libgio-2.0.so.0',
        'libglib-2.0.so.0', 'libnspr4.so', 'libnss3.so', 'libudev.so.1', 'libX11.so.6',
        'libxcb.so.1', 'libXcomposite.so.1', 'libXdamage.so.1', 'libXext.so.6',
        'libXfixes.so.3', 'libxkbcommon.so.0', 'libXrandr.so.2',
    }
    requested = {word[3:] for word in worker.split() if word.startswith('so:')}
    assert linked <= requested
    assert 'fontconfig-config' in worker.split()
    assert worker.index('apk add') < worker.index('playwright install chromium')


def test_docker_has_pinned_rootless_api_worker_and_agent_targets():
    dockerfile = _read('Dockerfile')
    instructions = '\n'.join(
        line for line in dockerfile.splitlines()
        if not line.lstrip().startswith('#'))
    assert (
        'ARG BASE_IMAGE=cgr.dev/chainguard/wolfi-base@sha256:'
        '833c48f1faf32c90e6f9501a2410a9aa28ceb2c594102edd4470053c30efa089'
    ) in dockerfile
    assert 'FROM runtime-base AS api' in dockerfile
    assert 'FROM runtime-base AS worker' in dockerfile
    assert 'FROM ${BASE_IMAGE} AS agent' in dockerfile
    assert 'apt-get' not in instructions, 'the Wolfi base has no apt'
    assert dockerfile.count('USER tofu:tofu') == 3
    assert 'postgresql' not in instructions.lower()

    runtime = instructions.split('FROM ${BASE_IMAGE} AS runtime-base', 1)[1]
    api = runtime.split('FROM runtime-base AS api', 1)[1].split(
        'FROM runtime-base AS worker', 1)[0]
    assert 'gcc' not in runtime.split()  # libgcc-s1 is a runtime dependency
    assert 'g++' not in runtime
    assert 'playwright' not in api.lower()


def test_public_agent_image_contains_only_the_installed_runtime():
    dockerfile = _read('Dockerfile')
    agent = dockerfile.split('FROM ${BASE_IMAGE} AS agent', 1)[1]
    assert 'COPY --from=agent-builder' in agent
    assert 'COPY . .' not in agent
    assert 'playwright' not in agent.lower()


def test_docker_context_excludes_local_bulk_and_secrets():
    ignored = {
        line.strip() for line in _read('.dockerignore').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    }
    required = {
        '.git', '.env', 'data/', 'logs/', 'uploads/', 'tests/', 'debug/*',
        'benchmarks/', 'codex/', 'evaluations/', 'node_modules/', 'node_modules.*/',
        'swebench_*/', '*.log', '.coverage', '.venv/', 'venv/', '.conda/',
        '.tofu_env.json', '.migrate_backup/', '.update_backup/',
        '.credentials_vault.key', 'build/', 'dist/', '*.db-wal', '*.db-shm',
    }
    assert required <= ignored
    assert '!README.md' in ignored
    assert '!packages/tofu-search/README.md' in ignored
    assert '!plugins/tofu-trading/README.md' in ignored
    assert '!debug/inspect_conversation.py' in ignored
