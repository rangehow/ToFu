"""Contracts for choosing fast local storage for replicas and task checkouts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from lib.storage_authority_sql import storage_capabilities
from serverctl_pkg import local_placement as placement


pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != 'posix', reason='placement is POSIX-only'),
]

_GIB = 1024 ** 3


@pytest.fixture
def durable_tmp(monkeypatch):
    """Treat pytest's temporary tree as the durable volume it stands in for."""
    monkeypatch.setattr(placement, 'ephemeral_roots', lambda _environ: [])


def _mount(path: Path) -> str:
    return str(path.resolve()).replace(' ', r'\040')


def _mountinfo(source: Path, local: Path, *, source_fs: str = 'fuse.bgfuse',
               local_fs: str = 'ext4', local_options: str = 'rw',
               local_device: str = '8:2') -> str:
    return (
        '20 1 8:1 / / rw - ext4 /dev/root rw\n'
        f'21 20 0:60 / {_mount(source)} rw - {source_fs} remote rw\n'
        f'22 20 {local_device} / {_mount(local)} {local_options} - {local_fs} /dev/local rw\n'
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = tmp_path / 'network-volume'
    local = tmp_path / 'local-disk'
    (source / 'env' / 'bin').mkdir(parents=True)
    (local / 'home').mkdir(parents=True)
    return source, local, {'HOME': str(local / 'home')}


def _usage(free: int = 400 * _GIB, total: int = 500 * _GIB):
    return lambda _path: SimpleNamespace(free=free, total=total)


def _latency(source: Path, slow: float = 400.0, fast: float = 5.0):
    return lambda directory: slow if str(directory).startswith(str(source.resolve())) else fast


def _choose(source: Path, local: Path, environment: dict[str, str], purpose: str, **overrides):
    options = {
        'environ': environment,
        'mountinfo_text': _mountinfo(source, local),
        'latency_probe': _latency(source),
        'disk_usage': _usage(),
    }
    options.update(overrides)
    return placement.choose_local_root(source / 'env' / 'bin', purpose, **options)


def test_volume_description_matches_the_storage_authority_probe(tmp_path):
    """The stdlib manager-side classifier and the storage probe never drift."""
    filesystem_types = sorted({
        *placement._NETWORK_FILESYSTEMS, *placement._LOCAL_BLOCK_FILESYSTEMS,
        *placement._MEMORY_FILESYSTEMS, *placement._OVERLAY_FILESYSTEMS,
        *storage_capabilities._NETWORK_FILESYSTEMS,
        *storage_capabilities._LOCAL_BLOCK_FILESYSTEMS,
        *storage_capabilities._MEMORY_FILESYSTEMS,
        *storage_capabilities._OVERLAY_FILESYSTEMS,
        'fuse', 'fuse.bgfuse', 'fuse.sshfs', 'fuse.rclone', 'fuseblk', 'proc', 'squashfs',
    })
    (tmp_path / 'network volume' / 'deep').mkdir(parents=True)
    paths = [Path('/'), Path('/proc/self'), tmp_path / 'network volume' / 'deep']
    for filesystem_type in filesystem_types:
        mountinfo = (
            '20 1 8:1 / / rw - ext4 /dev/root rw\n'
            f'21 20 0:60 / {_mount(tmp_path)}/network\\040volume rw - '
            f'{filesystem_type} remote rw\n'
        )
        for path in paths:
            mirrored = placement.describe_volume(path, mountinfo_text=mountinfo)
            authority = storage_capabilities.describe_mount(path, mountinfo_text=mountinfo)
            assert (mirrored.filesystem_type, mirrored.mount_point, mirrored.storage_class,
                    mirrored.persistence) == (
                authority.filesystem_type, authority.mount_point,
                authority.storage_class, authority.persistence), (filesystem_type, path)


@pytest.mark.parametrize('purpose, leaf', [
    (placement.PURPOSE_RUNTIME, 'runtime-replicas'),
    (placement.PURPOSE_NATIVE_BUILD, 'native-builds'),
])
def test_runtime_caches_place_on_a_decisively_faster_private_local_root(
        tmp_path, durable_tmp, purpose, leaf):
    source, local, environment = _layout(tmp_path)

    decision = _choose(source, local, environment, purpose)

    assert decision.selected and decision.reason == 'selected'
    root = Path(decision.root)
    assert root == local / 'home' / '.cache' / 'tofu' / leaf
    assert root.is_dir() and (root.stat().st_mode & 0o777) == 0o700
    status = decision.as_status()
    assert (status['sourceClass'], status['speedup']) == ('network-filesystem', 80.0)


def test_runtime_placement_declines_without_a_real_benefit(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)

    local_source = _choose(source, local, environment, placement.PURPOSE_RUNTIME,
                           mountinfo_text=_mountinfo(source, local, source_fs='xfs'))
    marginal = _choose(source, local, environment, placement.PURPOSE_RUNTIME,
                       latency_probe=_latency(source, slow=12.0, fast=5.0))
    disabled = _choose(source, local, {**environment, 'TOFU_RUNTIME_REPLICA': 'off'},
                       placement.PURPOSE_RUNTIME)

    assert (local_source.selected, local_source.reason) == (False, 'source-local')
    assert (marginal.selected, marginal.reason) == (False, 'insufficient-speedup')
    assert (disabled.selected, disabled.reason) == (False, 'disabled')
    assert not (local / 'home' / '.cache').exists()


@pytest.mark.parametrize(('mountinfo_overrides', 'usage', 'reason'), [
    ({'local_fs': 'nfs4'}, _usage(), 'candidate-not-local'),
    ({'local_fs': 'tmpfs'}, _usage(), 'candidate-not-local'),
    ({'local_options': 'rw,nosuid,noexec'}, _usage(), 'candidate-noexec'),
    ({}, _usage(free=1 * _GIB, total=10 * _GIB), 'insufficient-space'),
    ({'local_device': '0:60'}, _usage(), 'candidate-on-source-volume'),
])
def test_runtime_placement_refuses_unsafe_targets(
        tmp_path, durable_tmp, mountinfo_overrides, usage, reason):
    source, local, environment = _layout(tmp_path)

    decision = _choose(source, local, environment, placement.PURPOSE_RUNTIME,
                       mountinfo_text=_mountinfo(source, local, **mountinfo_overrides),
                       disk_usage=usage)

    assert (decision.selected, decision.reason) == (False, reason)


def test_temporary_directories_hold_no_caches_and_no_checkouts_off_a_dedicated_disk(tmp_path):
    source, local, environment = _layout(tmp_path)

    pytest_tmp = _choose(source, local, {**environment, 'TOFU_RUNTIME_REPLICA': 'on'},
                         placement.PURPOSE_RUNTIME)
    age_cleaned = _choose(source, local, {**environment, 'TOFU_LOCAL_DIR': '/var/tmp/tofu'},
                          placement.PURPOSE_RUNTIME)
    lanes = _choose(source, local, environment, placement.PURPOSE_LANES)
    checkouts = _choose(source, local, environment, placement.PURPOSE_WORKSPACES)

    assert (pytest_tmp.selected, pytest_tmp.reason) == (False, 'candidate-persistence-ephemeral')
    assert (lanes.selected, lanes.reason) == (False, 'candidate-persistence-ephemeral')
    assert (checkouts.selected, checkouts.reason) == (False, 'candidate-persistence-ephemeral')
    assert (age_cleaned.selected, age_cleaned.reason) == (
        False, 'candidate-persistence-ephemeral')


@pytest.fixture
def container(tmp_path, monkeypatch):
    """This host's shape (2026-11-12): home on the root overlay, the temporary
    directory on its own local disk (a Kubernetes emptyDir on md0p1, xfs) that no
    age cleaner reaches, and the source on the network volume."""
    source = tmp_path / 'network-volume'
    overlay = tmp_path / 'overlay-root'
    temporary = tmp_path / 'scratch-disk'
    (source / 'env' / 'bin').mkdir(parents=True)
    (overlay / 'home').mkdir(parents=True)
    temporary.mkdir()
    mountinfo = (f'20 1 0:50 / {_mount(overlay)} rw - overlay overlay rw\n'
                 f'21 20 0:60 / {_mount(source)} rw - fuse.bgfuse remote rw\n'
                 f'22 20 9:1 / {_mount(temporary)} rw - xfs /dev/md0p1 rw\n')
    monkeypatch.setattr(placement, 'temporary_root', lambda: str(temporary.resolve()),
                        raising=False)
    monkeypatch.setattr(placement, 'ephemeral_roots', lambda _environ: [str(temporary.resolve())])
    monkeypatch.setattr(placement, 'age_cleaned', lambda path: False, raising=False)
    return source, overlay, temporary, mountinfo


def _space(temporary: Path, temporary_free: int, overlay_free: int = 49 * _GIB):
    def usage(path):
        if str(path).startswith(str(temporary)):
            return SimpleNamespace(free=temporary_free, total=15_000 * _GIB)
        return SimpleNamespace(free=overlay_free, total=252 * _GIB)
    return usage


def _choose_in(container, purpose, environment=None, usage=None, mountinfo=None):
    source, overlay, temporary, container_mounts = container
    environment = {'HOME': str(overlay / 'home'), **(environment or {})}
    return placement.choose_local_root(
        source / 'env' / 'bin', purpose, environ=environment,
        mountinfo_text=mountinfo or container_mounts, latency_probe=_latency(source),
        disk_usage=usage or _space(temporary, 13_000 * _GIB))


def test_lanes_and_checkouts_leave_the_root_overlay_for_a_local_temporary_disk(container):
    """Owner decision 2026-11-12 (option A): new lanes and task checkouts (which
    have no switch) go to the local temporary disk, never the root overlay that
    ran out of room."""
    _source, overlay, temporary, _mountinfo_text = container
    uid = os.getuid()

    lanes = _choose_in(container, placement.PURPOSE_LANES)
    checkouts = _choose_in(container, placement.PURPOSE_WORKSPACES)

    assert lanes.selected and Path(lanes.root) == temporary / f'tofu-{uid}-lanes'
    assert checkouts.selected and Path(checkouts.root) == temporary / f'tofu-{uid}-workspaces'
    assert (Path(lanes.root).lstat().st_mode & 0o777) == 0o700

    # A temporary directory that is only a directory of the root overlay is that
    # overlay: lanes stay with its ordinary candidate.
    source = container[0]
    shared_overlay = (f'20 1 0:50 / {_mount(temporary.parent)} rw - overlay overlay rw\n'
                      f'21 20 0:60 / {_mount(source)} rw - fuse.bgfuse remote rw\n')
    undedicated = placement.choose_local_root(
        source / 'env' / 'bin', placement.PURPOSE_LANES,
        environ={'HOME': str(overlay / 'home')}, mountinfo_text=shared_overlay,
        latency_probe=_latency(source), disk_usage=_space(temporary, 13_000 * _GIB))
    assert Path(undedicated.root) == overlay / 'home' / '.local' / 'state' / 'tofu' / 'lanes'
    assert not any(Path(root).is_relative_to(temporary) for root in placement.candidate_roots(
        placement.PURPOSE_NATIVE_AUTHORITY, {'HOME': str(overlay / 'home')}))


def test_beside_a_temporary_disk_rebuilt_trees_never_use_the_root_overlay(container):
    """Never `/`: a full temporary disk sends lanes to the caller's data-volume
    fallback, not onto the overlay the image, runtimes and caches share."""
    _source, _overlay, temporary, _mountinfo_text = container
    uid = os.getuid()

    roomier_overlay = _choose_in(container, placement.PURPOSE_LANES,
                                 usage=_space(temporary, 20 * _GIB, overlay_free=49 * _GIB))
    full_disk = _choose_in(container, placement.PURPOSE_LANES, usage=_space(temporary, 1 * _GIB))

    assert roomier_overlay.selected and Path(roomier_overlay.root) == temporary / f'tofu-{uid}-lanes'
    assert (full_disk.selected, full_disk.reason) == (
        False, 'candidate-overlay-beside-temporary-disk')


def test_caches_leave_the_root_overlay_for_a_local_temporary_disk(container):
    """Runtime replicas, compiler caches and indexes follow lanes onto the local
    scratch disk (2026-11-12: with lanes they held 125 GiB of the full overlay). A
    full scratch disk leaves them with their source, never on the overlay."""
    _source, _overlay, temporary, _mountinfo_text = container
    uid = os.getuid()

    for purpose, leaf in ((placement.PURPOSE_RUNTIME, 'runtime-replicas'),
                          (placement.PURPOSE_NATIVE_BUILD, 'native-builds'),
                          (placement.PURPOSE_INDEXES, 'indexes')):
        placed = _choose_in(container, purpose)
        full_disk = _choose_in(container, purpose, usage=_space(temporary, 1 * _GIB))

        assert placed.selected and Path(placed.root) == temporary / f'tofu-{uid}-{leaf}', purpose
        assert (full_disk.selected, full_disk.reason) == (
            False, 'candidate-overlay-beside-temporary-disk'), purpose


def test_the_scratch_disk_is_decided_by_the_host_not_by_tmpdir_or_tempfiles_fallback(
        monkeypatch, tmp_path):
    """Neither a redirected TMPDIR (builds and drills ran with it on RAM-backed
    /dev/shm, which held 27.8 GiB on 2026-11-12) nor tempfile's /var/tmp fallback
    after a failed write probe of /tmp (test runs left 24.55 GB on the root overlay
    on 2026-11-08) moves the host's scratch disk."""
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'redirected'))
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'fallback'))

    assert placement.temporary_root() == str(Path('/tmp').resolve())


def test_the_scratch_disk_is_the_temporary_directory_on_its_own_uncleaned_local_disk(
        monkeypatch, tmp_path):
    temporary = tmp_path / 'scratch'
    temporary.mkdir()
    monkeypatch.setattr(placement, 'temporary_root', lambda: str(temporary.resolve()))
    monkeypatch.setattr(placement, 'age_cleaned', lambda path: False)
    root = '20 1 0:50 / / rw - overlay overlay rw\n'
    dedicated = root + f'22 20 9:1 / {_mount(temporary)} rw - xfs /dev/md0p1 rw\n'
    memory = root + f'22 20 0:9 / {_mount(temporary)} rw - tmpfs tmpfs rw\n'

    assert placement.scratch_disk(mountinfo_text=dedicated) == str(temporary.resolve())
    assert placement.scratch_disk(mountinfo_text=root) is None
    assert placement.scratch_disk(mountinfo_text=memory) is None
    monkeypatch.setattr(placement, 'age_cleaned', lambda path: True)
    assert placement.scratch_disk(mountinfo_text=dedicated) is None


def test_pinning_keeps_temporary_files_on_the_scratch_disk(monkeypatch, tmp_path):
    """Pinned, a failing scratch disk fails loudly instead of filling the overlay."""
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', '/var/tmp')
    monkeypatch.setattr(placement, 'scratch_disk', lambda **_options: str(scratch))
    environment = {}

    assert placement.pin_temporary_directory(environment) == str(scratch)
    assert environment == {'TMPDIR': str(scratch)}
    assert tempfile.gettempdir() == str(scratch)

    monkeypatch.setattr(placement, 'scratch_disk', lambda **_options: None)
    untouched = {}
    assert placement.pin_temporary_directory(untouched) is None
    assert untouched == {}


def test_this_test_session_keeps_its_temporary_files_on_the_scratch_disk():
    """tests/conftest.py pins test temp before the suite creates its first file."""
    scratch = placement.scratch_disk()
    if scratch is None:
        pytest.skip('this host has no dedicated local scratch disk')
    assert tempfile.gettempdir() == scratch
    assert os.environ.get('TMPDIR') == scratch


def test_durable_placement_takes_the_disk_with_the_most_free_space(container):
    _source, overlay, temporary, mountinfo = container
    home_disk = mountinfo.replace(' - overlay overlay rw', ' - ext4 /dev/sda1 rw')
    home_lanes = overlay / 'home' / '.local' / 'state' / 'tofu' / 'lanes'

    roomier_home = _choose_in(container, placement.PURPOSE_LANES, mountinfo=home_disk,
                              usage=_space(temporary, 20 * _GIB, overlay_free=49 * _GIB))
    full_temporary = _choose_in(container, placement.PURPOSE_LANES, mountinfo=home_disk,
                                usage=_space(temporary, 1 * _GIB))
    roomier_temporary = _choose_in(container, placement.PURPOSE_LANES, mountinfo=home_disk)

    assert roomier_home.selected and Path(roomier_home.root) == home_lanes
    assert full_temporary.selected and Path(full_temporary.root) == home_lanes
    assert Path(roomier_temporary.root) == temporary / f'tofu-{os.getuid()}-lanes'


def test_a_temporary_disk_an_age_cleaner_reaches_is_never_a_lane_root(container, monkeypatch):
    _source, overlay, temporary, _mountinfo_text = container
    monkeypatch.setattr(placement, 'age_cleaned',
                        lambda path: Path(path).is_relative_to(temporary), raising=False)

    lanes = _choose_in(container, placement.PURPOSE_LANES)

    assert Path(lanes.root) == overlay / 'home' / '.local' / 'state' / 'tofu' / 'lanes'


@pytest.mark.parametrize('purpose, leaf', [
    (placement.PURPOSE_LANES, 'lanes'),
    (placement.PURPOSE_WORKSPACES, 'workspaces'),
    (placement.PURPOSE_RUNTIME, 'runtime-replicas'),
])
def test_a_temporary_candidate_is_judged_by_its_own_volume(container, purpose, leaf):
    """Durable trees go on the temporary directory only when the candidate itself is
    on that local disk (owner decision 2026-11-12, option A). A directory mounted
    there from another volume is not the scratch disk, whatever /tmp is."""
    _source, _overlay, temporary, mountinfo = container
    candidate = temporary / f'tofu-{os.getuid()}-{leaf}'
    candidate.mkdir(mode=0o700)
    candidate.chmod(0o700)
    nested = mountinfo + f'23 22 0:51 / {_mount(candidate)} rw - overlay overlay rw\n'

    decision = _choose_in(container, purpose, mountinfo=nested)

    assert (decision.selected, decision.reason) == (
        False, 'candidate-overlay-beside-temporary-disk')


@pytest.mark.parametrize('configuration, cleaned', [
    # This host: /tmp is emptied only when its volume is, and never aged.
    ('D /tmp 1777 root root -\nD! /tmp/.X11-unix 1777 root root 10d\n'
     'x /tmp/systemd-private-%b-*\nR! /tmp/systemd-private-*\n', False),
    ('q /tmp 1777 root root 10d\n', True),  # the usual distribution default
    ('D /tmp 1777 root root 30d\n', True),
    ('e /tmp - - - ~1d\n', True),
    ('d /tmp/tofu-* 0700 - - 2d\n', True),
    ('#q /tmp 1777 root root 10d\n\nQ /var/lib/machines 0700 - - -\n', False),
    ('v /tmp/other 1777 root root 10d\n', False),
])
def test_only_a_configured_age_cleaner_makes_a_temporary_disk_unsafe(
        monkeypatch, configuration, cleaned):
    monkeypatch.setattr(placement, 'temporary_root', lambda: '/tmp', raising=False)

    assert placement.age_cleaned('/tmp/tofu-1000-lanes', tmpfiles_text=configuration,
                                 cron_cleaners=()) is cleaned
    assert placement.age_cleaned('/tmp/tofu-1000-lanes', tmpfiles_text='',
                                 cron_cleaners=('tmpreaper',)) is True
    assert placement.age_cleaned('/var/tmp/tofu', tmpfiles_text='', cron_cleaners=()) is True
    assert placement.age_cleaned('/home/user/.local/state/tofu/lanes',
                                 tmpfiles_text=configuration, cron_cleaners=('tmpwatch',)) is False


def test_redirected_xdg_cache_falls_back_to_the_home_cache(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    redirected = {**environment, 'XDG_CACHE_HOME': str(source / 'tool-cache')}

    roots = placement.candidate_roots(placement.PURPOSE_RUNTIME, redirected)
    decision = _choose(source, local, redirected, placement.PURPOSE_RUNTIME)

    assert roots == [Path(placement.temporary_root()) / f'tofu-{os.getuid()}-runtime-replicas',
                     source / 'tool-cache' / 'tofu' / 'runtime-replicas',
                     local / 'home' / '.cache' / 'tofu' / 'runtime-replicas']
    assert decision.selected and Path(decision.root) == roots[2]
    assert placement.candidate_roots(
        placement.PURPOSE_RUNTIME, {**redirected, 'TOFU_LOCAL_DIR': str(local / 'x')},
    ) == [local / 'x' / 'runtime-replicas']


@pytest.mark.parametrize('purpose, leaf', [
    (placement.PURPOSE_LANES, 'lanes'),
    (placement.PURPOSE_WORKSPACES, 'workspaces'),
])
def test_lanes_and_task_checkouts_always_use_a_qualifying_local_root(
        tmp_path, durable_tmp, purpose, leaf):
    """Owner decision 2026-11-12: no switch, no speed bargain; only safety refuses."""
    source, local, environment = _layout(tmp_path)
    overlay = _mountinfo(source, local, local_fs='overlay')
    # The retired operator switch is ignored whatever it says.
    retired = {**environment, 'TOFU_LOCAL_WORKSPACES': 'off'}

    on_disk = _choose(source, local, retired, purpose)
    on_overlay = _choose(source, local, environment, purpose, mountinfo_text=overlay)
    marginal = _choose(source, local, environment, purpose,
                       latency_probe=_latency(source, slow=12.0, fast=5.0))
    same_volume = _choose(source, local, environment, purpose,
                          mountinfo_text=_mountinfo(source, local, local_device='0:60'))

    assert on_disk.selected and on_overlay.selected and marginal.selected
    assert {Path(on_disk.root), Path(on_overlay.root), Path(marginal.root)} == {
        local / 'home' / '.local' / 'state' / 'tofu' / leaf}
    assert placement.placement_mode(purpose, retired) == 'on'
    assert (same_volume.selected, same_volume.reason) == (False, 'candidate-on-source-volume')


def test_a_shared_existing_root_is_never_adopted(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir()
    shared.chmod(0o777)

    with pytest.raises(PermissionError):
        placement.ensure_private_directory(shared)


def test_native_authority_places_only_on_proven_persistent_local_disks(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    purpose = placement.PURPOSE_NATIVE_AUTHORITY

    decision = _choose(source, local, environment, purpose)
    assert decision.selected
    assert Path(decision.root) == local / 'home' / '.local' / 'state' / 'tofu' / 'native-authority'

    crowded = _choose(source, local, environment, purpose, required_bytes=450 * _GIB)
    assert (crowded.selected, crowded.reason) == (False, 'insufficient-space')

    overlay = _mountinfo(source, local, local_fs='overlay')
    refused = _choose(source, local, environment, purpose, mountinfo_text=overlay)
    assert (refused.selected, refused.reason) == (False, 'candidate-persistence-unknown')
    consented = _choose(source, local, {**environment, 'TOFU_DB_FASTPATH': 'on'}, purpose,
                        mountinfo_text=overlay)
    assert consented.selected
    assert placement.placement_mode(purpose, {'TOFU_DB_FASTPATH': 'off'}) == 'off'
    assert placement.placement_mode(purpose, {}) == 'auto'
