"""Release and cached wheels skip resolution without weakening admission."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import zipfile

import pytest

from bootstrap_pkg import install_artifacts, python_wheels as wheels

pytestmark = pytest.mark.unit

RUNTIME = {'python': 'cpython-312', 'platform': 'linux', 'machine': 'x86_64',
           'tags': ['py3-none-any', 'cp312-cp312-manylinux_2_17_x86_64']}


def wheel(path, version='1.0', tag='py3-none-any'):
    path.mkdir(parents=True, exist_ok=True)
    artifact = path / f'demo-{version}-{tag}.whl'
    with zipfile.ZipFile(artifact, 'w') as archive:
        archive.writestr(f'demo-{version}.dist-info/METADATA',
                         f'Metadata-Version: 2.1\nName: demo\nVersion: {version}\n')
        archive.writestr(f'demo-{version}.dist-info/WHEEL',
                         f'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n')
        archive.writestr(f'demo-{version}.dist-info/RECORD', '')
    return artifact


def house(root, requirements, version='1.0', tag='py3-none-any'):
    stage = root / 'stage'
    artifact = wheel(stage, version, tag)
    (stage / 'install.lock').write_text(f'demo=={version} --hash=sha256:{wheels.digest(artifact)}\n')
    return wheels.publish(stage, root / 'house', requirements, RUNTIME).parent


def test_warm_cache_has_zero_resolver_or_download_calls(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements)
    monkeypatch.setattr(wheels, 'identity', lambda python: RUNTIME)
    def unexpected(*args, **kwargs):
        pytest.fail('A verified warm cache must not acquire artifacts')
    monkeypatch.setattr(wheels, 'release_bundle', unexpected)
    monkeypatch.setattr(install_artifacts, '_acquire_python_wheels', unexpected)
    assert install_artifacts.prepare_python_wheels('unused', requirements, cache) == cache / 'install.lock'


@pytest.mark.parametrize('changed', ['wheel', 'lock', 'extra', 'symlink'])
def test_corruption_fails_before_candidate_install(tmp_path, changed):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements)
    artifact = next(cache.glob('*.whl'))
    if changed == 'wheel':
        artifact.write_bytes(b'corrupt')
    elif changed == 'lock':
        (cache / 'install.lock').write_text('changed')
    elif changed == 'extra':
        wheel(cache, '2.0')
    else:
        original = artifact.read_bytes()
        artifact.unlink()
        target = tmp_path / 'unrelated.whl'
        target.write_bytes(original)
        artifact.symlink_to(target)
    with pytest.raises(ValueError, match='hash mismatch|Undeclared'):
        wheels.reuse(cache, requirements, RUNTIME)
    assert cache.exists()


def test_bundles_survive_checkout_relocation_and_install_without_network(tmp_path, monkeypatch):
    source = tmp_path / 'builder'
    source.mkdir()
    artifact = wheel(source / 'first party')
    requirements = source / 'requirements.txt'
    requirements.write_text(str(artifact) + '\n')
    cache = house(source, requirements)
    archive = wheels.package(cache, requirements, RUNTIME, '0.18.0', tmp_path / 'release')
    target = tmp_path / '用户 installation'
    target.mkdir()
    local = target / 'first party' / artifact.name
    local.parent.mkdir()
    local.write_bytes(artifact.read_bytes())
    new_requirements = target / 'requirements.txt'
    # pip reads requirements files as UTF-8 whatever the locale (Windows: cp1252).
    new_requirements.write_text(str(local) + '\n', encoding='utf-8')
    assert wheels.requirements_digest(requirements) == wheels.requirements_digest(new_requirements)
    monkeypatch.setenv('TOFU_PYTHON_BUNDLE', str(archive))
    monkeypatch.setattr(wheels, 'identity', lambda python: RUNTIME)
    monkeypatch.setattr(install_artifacts, '_acquire_python_wheels',
                        lambda *args: pytest.fail('Explicit bundle must install offline'))
    lock = install_artifacts.prepare_python_wheels('unused', new_requirements, target / 'house')
    assert 'demo==1.0' in lock.read_text()


def test_incompatible_wheel_tags_and_source_changes_decline_reuse(tmp_path):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements, tag='cp312-cp312-manylinux_2_17_x86_64')
    assert wheels.reuse(cache, requirements, dict(RUNTIME, tags=['py3-none-any'])) is None
    assert wheels.reuse(cache, requirements, dict(RUNTIME, machine='aarch64')) is None
    requirements.write_text('demo>=2\n')
    assert wheels.reuse(cache, requirements, RUNTIME) is None


def test_failed_rebuild_preserves_old_house(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements)
    before = (cache / 'manifest.json').read_bytes()
    requirements.write_text('demo>=2\n')
    monkeypatch.setattr(wheels, 'identity', lambda python: RUNTIME)
    monkeypatch.setattr(wheels, 'release_bundle', lambda *args: None)
    def fail(*args):
        raise RuntimeError('interrupted acquisition')
    monkeypatch.setattr(install_artifacts, '_acquire_python_wheels', fail)
    with pytest.raises(RuntimeError, match='interrupted'):
        install_artifacts.prepare_python_wheels('unused', requirements, cache)
    assert (cache / 'manifest.json').read_bytes() == before
    assert not list(tmp_path.glob('.prepare-wheels-*'))


@pytest.mark.parametrize('member', ['../escape.whl', '/escape.whl', 'duplicate', 'link'])
def test_unsafe_archive_never_replaces_previous_cache(tmp_path, member):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements)
    before = (cache / 'manifest.json').read_bytes()
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        entry = tarfile.TarInfo('demo.whl' if member in ('duplicate', 'link') else member)
        if member == 'link':
            entry.type = tarfile.SYMTYPE
            entry.linkname = 'unrelated'
            bundle.addfile(entry)
        else:
            entry.size = 1
            bundle.addfile(entry, io.BytesIO(b'x'))
            if member == 'duplicate':
                bundle.addfile(entry, io.BytesIO(b'x'))
    checksum = archive.with_name(archive.name + '.sha256')
    checksum.write_text(f'{wheels.digest(archive)}  {archive.name}\n')
    with pytest.raises(ValueError, match='Unsafe'):
        wheels.import_bundle(archive, checksum, cache, requirements, RUNTIME)
    assert (cache / 'manifest.json').read_bytes() == before


def test_uv_resolver_and_real_local_wheel_acquisition(tmp_path, monkeypatch):
    if install_artifacts.shutil.which('uv') is None:
        pytest.skip('uv is unavailable')
    # The target identity probe reads pip's tag table, as in the pip-seeded
    # environments install.sh creates; a venv built without --seed has none.
    if subprocess.run([sys.executable, '-I', '-c', 'import pip._vendor.packaging.tags'],
                      capture_output=True, timeout=60).returncode:
        pytest.skip('this interpreter has no pip to report its wheel tags')
    artifact = wheel(tmp_path / 'local')
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text(str(artifact) + '\n')
    monkeypatch.setenv('TOFU_PYTHON_BUNDLE_NO_DOWNLOAD', '1')
    monkeypatch.delenv('TOFU_PYTHON_BUNDLE', raising=False)
    monkeypatch.setenv('UV_OFFLINE', '1')
    monkeypatch.setenv('PIP_NO_INDEX', '1')
    start = time.monotonic()
    lock = install_artifacts.prepare_python_wheels(sys.executable, requirements, tmp_path / 'house')
    cold = time.monotonic() - start
    start = time.monotonic()
    assert install_artifacts.prepare_python_wheels(sys.executable, requirements, lock.parent) == lock
    print(f'local wheel admission cold={cold:.3f}s warm={time.monotonic() - start:.3f}s')
    assert 'demo==1.0' in lock.read_text()


def test_publication_failure_restores_previous_cache(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements)
    before = (cache / 'manifest.json').read_bytes()
    stage = tmp_path / 'replacement'
    stage.mkdir()
    rename = Path.rename
    def fail(source, dest):
        if source == stage:
            raise OSError('publication failure')
        return rename(source, dest)
    monkeypatch.setattr(Path, 'rename', fail)
    with pytest.raises(OSError, match='publication failure'):
        wheels._replace(stage, cache)
    assert (cache / 'manifest.json').read_bytes() == before


def test_release_and_lock_changes_invalidate_cached_resolution(tmp_path):
    (tmp_path / 'install.sh').write_text('source installer')
    (tmp_path / 'VERSION').write_text('0.18.0')
    (tmp_path / 'uv.lock').write_text('locked release inputs')
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = house(tmp_path, requirements)
    (tmp_path / 'uv.lock').write_text('new locked versions')
    assert wheels.reuse(cache, requirements, RUNTIME) is None


def test_incomplete_uv_acquisition_never_publishes(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    monkeypatch.setattr(install_artifacts.shutil, 'which', lambda name: 'uv')
    monkeypatch.setattr(wheels, 'identity', lambda python: RUNTIME)
    monkeypatch.setattr(wheels, 'release_bundle', lambda *args: None)
    def run(command, **kwargs):
        if 'compile' in command:
            Path(command[command.index('--output-file') + 1]).write_text('demo==1.0\n')
        # A fault-injected successful acquisition with an OMITTED wheel.
    monkeypatch.setattr(install_artifacts.subprocess, 'run', run)
    with pytest.raises(ValueError, match='incomplete'):
        install_artifacts.prepare_python_wheels('unused', requirements, tmp_path / 'house')
    assert not (tmp_path / 'house').exists()


def test_unowned_directory_is_preserved_on_publication(tmp_path):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    cache = tmp_path / 'house'
    cache.mkdir()
    (cache / 'user-file.txt').write_text('keep')
    stage = tmp_path / 'stage'
    artifact = wheel(stage)
    (stage / 'install.lock').write_text(f'demo==1.0 --hash=sha256:{wheels.digest(artifact)}\n')
    with pytest.raises(ValueError, match='unowned'):
        wheels.publish(stage, cache, requirements, RUNTIME)
    assert (cache / 'user-file.txt').read_text() == 'keep'


def test_pip_fallback_obeys_release_constraints(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('demo>=1\n')
    constraints = tmp_path / 'install-constraints.txt'
    constraints.write_text('demo==1.0\n')
    monkeypatch.setattr(install_artifacts.shutil, 'which', lambda name: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if '--dry-run' in command:
            report = Path(command[command.index('--report') + 1])
            report.write_text(json.dumps({'install': [{
                'metadata': {'name': 'demo', 'version': '1.0'},
                'download_info': {'url': 'https://example.invalid/demo.whl',
                                  'archive_info': {'hashes': {'sha256': 'a' * 64}}}}]}))
        else:
            wheel(Path(command[command.index('--wheel-dir') + 1]))

    monkeypatch.setattr(install_artifacts.subprocess, 'run', run)
    install_artifacts._acquire_python_wheels('python', requirements, tmp_path / 'house')
    resolution = calls[0]
    assert '--constraint' in resolution
    assert resolution[resolution.index('--constraint') + 1] == str(constraints)


def test_search_wheel_build_pins_entry_timestamps_for_reproducible_bytes(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen['env'] = kwargs['env']
        (tmp_path / 'tofu_search-1.0.0-py3-none-any.whl').write_bytes(b'wheel')

    monkeypatch.setenv('SOURCE_DATE_EPOCH', '1700000000')
    monkeypatch.setattr(install_artifacts.subprocess, 'run', fake_run)
    wheel = install_artifacts.build_search_wheel(tmp_path, tmp_path, sys.executable)
    assert wheel.name == 'tofu_search-1.0.0-py3-none-any.whl'
    # File mtimes no longer reach the archive, so an export rebuild of the
    # released source reproduces the published wheel's bytes.
    assert seen['env']['SOURCE_DATE_EPOCH'] == '315532800'


def test_search_sdist_bytes_depend_only_on_member_content(tmp_path, monkeypatch):
    def raw_sdist(out_dir, stamp):
        artifact = Path(out_dir) / 'tofu_search-1.0.0.tar.gz'
        with tarfile.open(artifact, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
            for name, data in (('tofu_search-1.0.0/setup.cfg', b'[egg_info]\n'),
                               ('tofu_search-1.0.0/PKG-INFO', b'Name: tofu-search\n')):
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.uid, info.uname = len(data), stamp, 1234, 'builder'
                archive.addfile(info, io.BytesIO(data))
        return artifact

    built = []
    for index, stamp in enumerate((1794906132.9, 1794906146.3)):
        def fake_run(command, stamp=stamp, **kwargs):
            flag = '--out-dir' if '--out-dir' in command else '--outdir'
            raw_sdist(command[command.index(flag) + 1], stamp)

        monkeypatch.setattr(install_artifacts.subprocess, 'run', fake_run)
        out = tmp_path / f'out{index}'
        out.mkdir()
        built.append(install_artifacts.build_search_sdist(tmp_path, out, sys.executable))
        time.sleep(1.1)  # a gzip header would otherwise record a later build second
    first, second = (path.read_bytes() for path in built)
    assert built[0].name == 'tofu_search-1.0.0.tar.gz' and first == second
    with tarfile.open(built[0]) as archive:
        members = archive.getmembers()
    assert [item.name for item in members] == sorted(item.name for item in members)
    assert {(item.mtime, item.uid, item.uname, tuple(item.pax_headers)) for item in members} == {
        (install_artifacts.ZIP_DOS_EPOCH, 0, '', ())}
