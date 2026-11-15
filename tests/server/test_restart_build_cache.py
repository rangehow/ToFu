"""Restart compilation reuses a stable profile and private disposable cache."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import shutil
import subprocess

import pytest

from serverctl_pkg import local_placement, native_engine as engines
from tests.server.test_native_engine_update import OVERRIDE, binary, project as project

pytestmark = pytest.mark.unit


def test_build_profile_does_not_change_with_host_headroom(project, tmp_path, monkeypatch):
    import runtime_guards

    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw))
    for memory in (1024, 8192):
        monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
            effective_cpu_count=16, effective_memory_available_mb=memory))
        engines._build(project, tmp_path, {'CARGO_LOG': 'info'}, build_root=tmp_path / 'build')
    assert children[0]['environment'] == children[1]['environment']
    assert 'CARGO_BUILD_JOBS' not in children[0]['environment']
    assert int(children[0]['environment']['CARGO_PROFILE_RELEASE_CODEGEN_UNITS']) > 1
    assert children[0]['environment']['CARGO_PROFILE_RELEASE_LTO'] == 'false'
    assert children[0]['environment']['CARGO_PROFILE_RELEASE_INCREMENTAL'] == 'true'


def test_restart_profile_preserves_explicit_operator_optimization(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw))
    overrides = {'CARGO_PROFILE_RELEASE_LTO': 'thin',
                 'CARGO_PROFILE_RELEASE_CODEGEN_UNITS': '8',
                 'CARGO_PROFILE_RELEASE_INCREMENTAL': 'false',
                 'CARGO_PROFILE_RELEASE_OPT_LEVEL': '2'}
    engines._build(project, tmp_path, overrides, build_root=tmp_path / 'build')
    assert {key: children[0]['environment'][key] for key in overrides} == overrides


@pytest.mark.parametrize('diagnostics', [False, True])
def test_build_does_not_depend_on_diagnostic_probe(project, tmp_path, monkeypatch, diagnostics):
    import runtime_guards
    def unavailable(*_):
        assert diagnostics, 'ordinary restarts must not probe host capacity'
        raise OSError('probe unavailable')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', unavailable)
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw))
    environment = {'CARGO_LOG': 'info'} if diagnostics else {}
    engines._build(project, tmp_path, environment, build_root=tmp_path / 'build')
    assert len(children) == 1
    assert 'CARGO_BUILD_JOBS' not in children[0]['environment']


def test_toolchain_directory_alias_reuses_cache_without_renaming_cargo_proxy(project, tmp_path, monkeypatch):
    installation = tmp_path / 'installation'
    (installation / 'bin').mkdir(parents=True)
    (installation / 'bin/rustup').write_text('fixture proxy')
    (installation / 'bin/cargo').symlink_to('rustup')
    (installation / 'bin/cc').write_text('fixture compiler')
    alias = tmp_path / 'alias'
    alias.symlink_to(installation, target_is_directory=True)
    monkeypatch.setattr(engines.shutil, 'which', lambda name, path: str(Path(path.split(os.pathsep)[0]) / name))
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    calls = []
    monkeypatch.setattr(engines, '_run', lambda command, **kw: calls.append((command, kw)))
    for directory in (installation, alias):
        engines._build(project, tmp_path, {'PATH': str(directory / 'bin')}, build_root=tmp_path / 'build')
    assert calls[0][1]['environment']['CARGO_TARGET_DIR'] == calls[1][1]['environment']['CARGO_TARGET_DIR']
    assert all(Path(command[0]).name == 'cargo' for command, _ in calls)
    assert all(Path(command[0]).resolve().name == 'rustup' for command, _ in calls)


def test_real_cargo_keeps_alias_build_fresh_and_rebuilds_changed_source(project, tmp_path, monkeypatch):
    cargo_home = Path(os.environ.get('CARGO_HOME') or Path.home() / '.cargo')
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(cargo_home / 'bin'))
    rustc = shutil.which('rustc') or shutil.which('rustc', path=str(cargo_home / 'bin'))
    compiler = shutil.which('cc')
    if not cargo or not rustc or not compiler:
        pytest.skip('requires the local Rust and C toolchains')
    installation = tmp_path / 'toolchain'
    (installation / 'bin').mkdir(parents=True)
    (installation / 'bin/cargo').symlink_to(cargo)
    (installation / 'bin/rustc').symlink_to(rustc)
    alias = tmp_path / 'toolchain-alias'
    alias.symlink_to(installation, target_is_directory=True)
    crate = project / 'packages/tofu-db'
    (crate / 'Cargo.toml').write_text(
        '[package]\nname="tofu-db"\nversion="0.2.0"\nedition="2021"\n'
        '[features]\nembedded-mimalloc=[]\n')
    (crate / 'Cargo.lock').write_text('version=4\n[[package]]\nname="tofu-db"\nversion="0.2.0"\n')
    (crate / 'build.rs').write_text('fn main() {}\n')
    (crate / 'src/lib.rs').write_text('pub fn fixture() {}\n')
    source = crate / 'src/main.rs'
    source.write_text('fn main() { println!("first"); }\n')
    fresh = []

    def compile(command, *, cwd, environment, timeout, progress):
        result = subprocess.run(
            [*command, '--offline', '--message-format=json'], cwd=cwd, env=environment,
            capture_output=True, text=True, timeout=min(120, timeout))
        assert result.returncode == 0, result.stderr
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        binaries = [item for item in messages if item.get('reason') == 'compiler-artifact'
                    and 'bin' in item['target']['kind']]
        assert len(binaries) == 1
        assert binaries[0]['profile']['opt_level'] == '3'
        assert not binaries[0]['profile']['debug_assertions']
        fresh.append(binaries[0]['fresh'])
        return result.stdout

    monkeypatch.setattr(engines, '_run', compile)
    outputs = []
    for index, directory in enumerate((installation, alias, alias)):
        if index == 2:
            source.write_text('fn main() { println!("changed"); }\n')
        inherited = {key: value for key, value in os.environ.items()
                     if not key.startswith('CARGO_PROFILE_')}
        environment = {**inherited, 'CC': compiler,
                       'PATH': str(directory / 'bin') + os.pathsep + os.environ.get('PATH', os.defpath)}
        outputs.append(engines._build(project, tmp_path, environment, build_root=tmp_path / 'build'))
    assert len(set(outputs)) == 1
    assert fresh == [False, True, False]


def test_equivalent_library_path_aliases_reuse_cache_without_losing_link_isolation(project, tmp_path, monkeypatch):
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('cargo', 'cc'):
        (tools / name).write_text('fixture executable')
    library = tmp_path / 'library'
    library.mkdir()
    alias = tmp_path / 'library-alias'
    alias.symlink_to(library, target_is_directory=True)
    monkeypatch.setattr(engines.shutil, 'which', lambda name, path: str(tools / name))
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    outputs = []
    monkeypatch.setattr(engines, '_run', lambda command, **kw: outputs.append(kw))
    values = [
        os.pathsep.join((str(library), str(alias), '', 'relative')),
        os.pathsep.join((str(alias), str(library), '', 'relative')),
        str(library),
    ]
    for value in values:
        environment = {'PATH': str(tools), 'LD_LIBRARY_PATH': value,
                       'LIBRARY_PATH': value, 'RUSTFLAGS': '-C target-cpu=native'}
        engines._build(project, tmp_path, environment, build_root=tmp_path / 'build')
    targets = [call['environment']['CARGO_TARGET_DIR'] for call in outputs]
    assert targets[0] == targets[1]
    assert targets[0] != targets[2]
    assert all(call['environment']['LD_LIBRARY_PATH'] == value
               for call, value in zip(outputs, values))


def test_real_cargo_reuses_cache_across_library_aliases_and_runtime_loader_changes(project, tmp_path, monkeypatch):
    cargo_home = Path(os.environ.get('CARGO_HOME') or Path.home() / '.cargo')
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(cargo_home / 'bin'))
    compiler = shutil.which('cc')
    if not cargo or not compiler:
        pytest.skip('requires the local Rust and C toolchains')
    crate = project / 'packages/tofu-db'
    (crate / 'Cargo.toml').write_text(
        '[package]\nname="tofu-db"\nversion="0.2.0"\nedition="2021"\n'
        '[features]\nembedded-mimalloc=[]\n')
    (crate / 'Cargo.lock').write_text('version=4\n[[package]]\nname="tofu-db"\nversion="0.2.0"\n')
    (crate / 'build.rs').write_text('fn main() {}\n')
    (crate / 'src/lib.rs').write_text('pub fn fixture() {}\n')
    (crate / 'src/main.rs').write_text('fn main() { println!("first"); }\n')
    library = tmp_path / 'library'
    library.mkdir()
    alias = tmp_path / 'library-alias'
    alias.symlink_to(library, target_is_directory=True)
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: tmp_path / 'cache')
    inherited = {key: value for key, value in os.environ.items()
                 if not key.startswith('CARGO_PROFILE_')}
    environments = []
    for value in (str(library), str(alias)):
        from tofu_dotenv import update_dotenv_file
        update_dotenv_file(project / '.env', {'LD_LIBRARY_PATH': value})
        environments.append({**inherited, 'CC': compiler, 'LD_LIBRARY_PATH': str(tmp_path / 'runtime')})
    from tofu_dotenv import update_dotenv_file
    update_dotenv_file(project / '.env', {}, remove=('LD_LIBRARY_PATH',))
    freshness = []
    outputs = []

    def compile(command, *, cwd, environment, timeout, progress):
        result = subprocess.run(
            [*command, '--offline', '--message-format=json'], cwd=cwd, env=environment,
            capture_output=True, text=True, timeout=min(120, timeout))
        assert result.returncode == 0, result.stderr
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        artifacts = [item for item in messages if item.get('reason') == 'compiler-artifact']
        assert artifacts
        freshness.append(all(item['fresh'] for item in artifacts))
        return result.stdout

    monkeypatch.setattr(engines, '_run', compile)
    for environment in environments:
        outputs.append(engines._build(project, tmp_path, environment))
    assert len(set(outputs)) == 1
    assert freshness == [False, True]


def test_cache_keeps_distinct_compilers_and_link_flags_separate(project, tmp_path, monkeypatch):
    installations = [tmp_path / name for name in ('first', 'second')]
    for directory in installations:
        directory.mkdir()
        for name in ('cargo', 'cc'):
            (directory / name).write_text('fixture executable')
    monkeypatch.setattr(engines.shutil, 'which', lambda name, path: str(Path(path.split(os.pathsep)[0]) / name))
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    targets = []
    monkeypatch.setattr(engines, '_run', lambda command, **kw: targets.append(kw['environment']['CARGO_TARGET_DIR']))
    for directory, flags in ((installations[0], ''), (installations[1], ''), (installations[0], '-C target-cpu=native')):
        engines._build(project, tmp_path, {'PATH': str(directory), 'RUSTFLAGS': flags}, build_root=tmp_path / 'build')
    assert len(set(targets)) == 3


def test_runtime_loader_path_does_not_leak_into_managed_builds(project, tmp_path, monkeypatch):
    from tofu_dotenv import update_dotenv_file

    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('cargo', 'cc'):
        (tools / name).write_text('fixture executable')
    runtime_library = tmp_path / 'runtime-library'
    runtime_library.mkdir()
    declared_library = tmp_path / 'declared-library'
    declared_library.mkdir()
    monkeypatch.setattr(engines.shutil, 'which', lambda name, path: str(tools / name))
    probes = []
    def probe(compiler):
        probes.append((compiler, engines._compiler_probe_environment.get()))
        return True

    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', probe)
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: tmp_path / 'cache')
    children = []
    monkeypatch.setattr(engines, '_run', lambda *args, **kwargs: children.append(kwargs))
    environment = {'PATH': str(tools), 'LD_LIBRARY_PATH': str(runtime_library),
                   'RUSTFLAGS': 'private-build-flags'}

    engines._build(project, tmp_path, environment)
    update_dotenv_file(project / '.env', {'LD_LIBRARY_PATH': str(declared_library)})
    engines._build(project, tmp_path, environment)
    engines._build(project, tmp_path, environment, build_root=tmp_path / 'isolated')

    managed, declared, isolated = (call['environment'] for call in children)
    assert 'LD_LIBRARY_PATH' not in managed
    assert declared['LD_LIBRARY_PATH'] == str(declared_library)
    assert isolated['LD_LIBRARY_PATH'] == str(runtime_library)
    assert managed['RUSTFLAGS'] == 'private-build-flags'
    assert all(probe_environment.get('LD_LIBRARY_PATH') == expected
               for (_, probe_environment), expected in zip(probes, (None, str(declared_library), str(runtime_library))))
    assert environment['LD_LIBRARY_PATH'] == str(runtime_library)


def test_local_cache_uses_shared_placement_and_canonical_project_identity(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    calls = []

    def placement(source, purpose, **kwargs):
        calls.append((source, purpose, kwargs['environ']))
        cache.mkdir(mode=0o700, exist_ok=True)
        return local_placement.Placement(True, purpose, 'auto', 'selected', root=str(cache))

    monkeypatch.setattr(local_placement, 'choose_local_root', placement)
    alias = tmp_path / 'alias'
    alias.symlink_to(project, target_is_directory=True)
    environment = {'TOFU_LOCAL_DIR': str(tmp_path)}
    first = engines._build_cache_root(project, environment)
    assert first == engines._build_cache_root(alias, environment)
    assert first.is_relative_to(cache)
    assert first.stat().st_mode & 0o077 == 0
    assert calls[0][2] == environment
    other = tmp_path / 'other'; other.mkdir()
    assert first != engines._build_cache_root(other, environment)


@pytest.mark.parametrize('unavailable', [False, True])
def test_unavailable_local_cache_keeps_build_admitted(project, monkeypatch, unavailable):
    def placement(*args, **kwargs):
        if unavailable:
            raise OSError('local cache unavailable')
        return local_placement.Placement(False, 'native-build', 'auto', 'source-local')

    monkeypatch.setattr(local_placement, 'choose_local_root', placement)
    assert engines._build_cache_root(project, {}) == project / 'packages/tofu-db/target'


def test_cache_directory_symlink_cannot_escape_private_placement(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'; cache.mkdir(mode=0o700)
    monkeypatch.setattr(local_placement, 'choose_local_root', lambda *a, **k:
        local_placement.Placement(True, 'native-build', 'auto', 'selected', root=str(cache)))
    selected = engines._build_cache_root(project, {})
    shutil.rmtree(selected)
    outside = tmp_path / 'outside'; outside.mkdir()
    selected.symlink_to(outside, target_is_directory=True)
    assert engines._build_cache_root(project, {}) == project / 'packages/tofu-db/target'
    assert not list(outside.iterdir())


_CACHEDIR_SIGNATURE = 'Signature: 8a477f597d28d172789f06886806bc55'


@pytest.mark.parametrize('placed', [True, False])
def test_checkout_cargo_targets_follow_the_compiler_cache_and_carry_a_cache_tag(
        project, tmp_path, monkeypatch, placed):
    """Test engines and benchmarks compile where the checkout's compiler cache
    lives: the local scratch disk when placement selects it, else the checkout.
    Cargo tags only target directories it creates; these the harness creates."""
    cache = tmp_path / 'scratch' / 'native-builds'

    def placement(source, purpose, **kwargs):
        if not placed:
            return local_placement.Placement(False, purpose, 'auto', 'source-local')
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        return local_placement.Placement(True, purpose, 'auto', 'selected', root=str(cache))

    monkeypatch.setattr(local_placement, 'choose_local_root', placement)
    target = engines.cargo_target_directory(project, 'target-differential', {})

    if placed:
        assert target == engines._build_cache_root(project, {}) / 'target-differential'
        assert target.is_relative_to(cache)
    else:
        assert target == project / 'packages/tofu-db/target-differential'
    tag = target / 'CACHEDIR.TAG'
    assert tag.read_text().startswith(_CACHEDIR_SIGNATURE + '\n')
    tag.write_text(_CACHEDIR_SIGNATURE + '\n# written by cargo\n')
    assert engines.cargo_target_directory(project, 'target-differential', {}) == target
    assert tag.read_text() == _CACHEDIR_SIGNATURE + '\n# written by cargo\n'


def _git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    subprocess.run(['git', '-C', str(cwd), *args], env=env, check=True, capture_output=True)


def _reflinks(directory: Path) -> bool:
    (directory / 'probe').write_bytes(b'x' * 4096)
    copied = subprocess.run(['cp', '--reflink=always', str(directory / 'probe'),
                             str(directory / 'probe.clone')], capture_output=True)
    return copied.returncode == 0


@pytest.mark.parametrize('sibling_building', [False, True])
def test_a_new_checkout_target_is_seeded_from_a_sibling_by_copy_on_write(
        tmp_path, monkeypatch, sibling_building):
    """Local checkouts no longer each compile every dependency: the first target
    of a checkout clones a sibling checkout's (never its stable engine copy), and
    a sibling whose build holds the lock, or a volume without reflinks, seeds
    nothing and waits for nothing."""
    import fcntl

    main, second = tmp_path / 'main', tmp_path / 'second'
    main.mkdir()
    _git(main, 'init', '-q', '-b', 'main')
    _git(main, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
         'commit', '-q', '--allow-empty', '-m', 'base')
    _git(main, 'worktree', 'add', '-q', '--detach', str(second))
    built = main / 'packages/tofu-db/target-differential'
    (built / 'debug/build/dep-1').mkdir(parents=True)
    (built / 'debug/build/dep-1/out.rlib').write_bytes(b'artifact' * 1024)
    (built / 'stable').mkdir()
    (built / 'stable/tofu-db').write_bytes(b'sibling engine')
    monkeypatch.setattr(local_placement, 'choose_local_root',
                        lambda source, purpose, **kwargs: local_placement.Placement(
                            False, purpose, 'auto', 'source-local'))
    lock = open(built / '.build.lock', 'w')
    if sibling_building:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

    target = engines.cargo_target_directory(second, 'target-differential', {})
    lock.close()

    assert target == second.resolve() / 'packages/tofu-db/target-differential'
    assert not (target / 'stable').exists() and not (target / '.build.lock').exists()
    seeded = (target / 'debug/build/dep-1/out.rlib').exists()
    assert seeded == (not sibling_building and _reflinks(tmp_path))
    if seeded:
        assert (target / 'debug/build/dep-1/out.rlib').read_bytes() == b'artifact' * 1024
    assert (target / 'CACHEDIR.TAG').read_text().startswith(_CACHEDIR_SIGNATURE)


def test_a_build_preparation_removes_caches_of_checkouts_that_are_gone(tmp_path, monkeypatch):
    cache_root = tmp_path / 'native-builds'
    monkeypatch.setattr(local_placement, 'choose_local_root',
                        lambda source, purpose, **kwargs: local_placement.Placement(
                            True, purpose, 'auto', 'selected', root=str(cache_root)))
    kept, gone, current = tmp_path / 'kept', tmp_path / 'gone', tmp_path / 'current'
    kept.mkdir()
    gone.mkdir()
    current.mkdir()
    kept_cache = engines._build_cache_root(kept, {})
    gone_cache = engines._build_cache_root(gone, {})
    (gone_cache / 'release').mkdir()
    unnamed = cache_root / ('0' * 64)
    unnamed.mkdir()
    gone.rmdir()

    assert engines._build_cache_root(current, {}).is_dir()

    assert kept_cache.is_dir() and unnamed.is_dir()
    assert not gone_cache.exists()
    assert (kept_cache / 'CHECKOUT').read_text() == str(kept.resolve())


def test_matching_local_engine_is_reused_before_network_or_compiler(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    binary(cache / 'update-build-host/release/tofu-db', engines.source_fingerprint(project))
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: cache)
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('local candidate needs no download'))
    monkeypatch.setattr(engines, '_build', lambda *_: pytest.fail('local candidate needs no compile'))
    assert engines.prepare(project, {}, uncertified_override=OVERRIDE)
    engines.verify_current(project / 'packages/tofu-db/target/release/tofu-db', project)


def test_isolated_build_does_not_select_managed_cache(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: pytest.fail('private build owns its cache'))
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    calls = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: calls.append(kw))
    private = tmp_path / 'private'
    candidate = engines._build(project, tmp_path, {}, build_root=private)
    assert candidate.is_relative_to(private)
    assert Path(calls[0]['environment']['CARGO_TARGET_DIR']).is_relative_to(private)


def test_optional_watched_directories_survive_snapshot_refresh(project, tmp_path):
    snapshot = tmp_path / 'snapshot/packages/tofu-db'
    crate = project / 'packages/tofu-db'
    engines._sync_build_snapshot(project, crate, snapshot)
    watched = snapshot / '.cargo'
    assert watched.is_dir()
    stamp = watched.stat().st_mtime_ns
    engines._sync_build_snapshot(project, crate, snapshot)
    assert watched.stat().st_mtime_ns == stamp
    (crate / '.cargo').mkdir()
    config = crate / '.cargo/config.toml'
    config.write_text('[build]\nincremental = true\n')
    engines._sync_build_snapshot(project, crate, snapshot)
    assert (watched / config.name).read_bytes() == config.read_bytes()
    config.unlink()
    engines._sync_build_snapshot(project, crate, snapshot)
    assert watched.is_dir() and not list(watched.iterdir())


def test_stale_cached_candidates_do_not_rescan_source_per_candidate(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    for index in range(4):
        binary(cache / f'update-build-{index}/release/tofu-db', '0' * 64)
    expected = engines.source_fingerprint(project)
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: cache)
    original = engines.source_fingerprint
    scans = []
    def fingerprint(root):
        scans.append(root)
        return original(root)
    monkeypatch.setattr(engines, 'source_fingerprint', fingerprint)
    def download(root, stage, env):
        # Capture + initial deployed candidate check only; scanning obsolete
        # binaries must not multiply full source-tree reads.
        assert len(scans) <= 2
        return binary(stage / 'tofu-db', expected)
    monkeypatch.setattr(engines, '_prebuilt', download)
    assert engines.prepare(project, {}, uncertified_override=OVERRIDE)
