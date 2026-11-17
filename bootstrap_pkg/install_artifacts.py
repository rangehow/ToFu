"""Installer artifact admission before environment creation.

Entrypoints: prepare_search_wheel(root, python), build_search_wheel, build_search_sdist and CLI. Dependencies: stdlib;
wheel build uses pip in the supplied bootstrap interpreter, never the live env.
Content-addressed caches live beside the checkout and survive HOME changes.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import shutil
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile


# 1980-01-01T00:00:00Z: the earliest timestamp a ZIP (wheel) entry can record.
ZIP_DOS_EPOCH = 315532800


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError(f'Invalid wheel metadata: {wheel.name}')
        metadata = BytesParser().parsebytes(archive.read(names[0]))
        if metadata['Name'].lower().replace('_', '-') != 'tofu-search':
            raise ValueError(f'Unexpected package in {wheel.name}')
        return metadata['Version']


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', value):
        raise ValueError(f'Unsupported release version: {value}')
    return tuple(map(int, value.split('.')))


def prepare_search_wheel(root: Path, python: str) -> Path:
    root = root.resolve()
    requirement = re.search(r'^tofu-search>=([0-9.]+)', (root / 'requirements.txt').read_text(), re.M)
    if requirement is None:
        raise ValueError('No tofu-search compatibility floor declared')
    floor = requirement[1]
    member = root / 'packages/tofu-search'
    # Hash only retained package/build inputs, never dist or container caches.
    files = [member / name for name in ('pyproject.toml', 'README.md')]
    files += sorted((member / 'tofu_search').rglob('*.py'))
    source = hashlib.sha256()
    if (member / 'pyproject.toml').is_file():
        for path in files:
            source.update(path.relative_to(member).as_posix().encode() + b'\0')
            source.update(path.read_bytes())
    else:
        source.update((root / 'requirements.txt').read_bytes())
        for bundled in sorted((root / 'vendor').glob('tofu_search-*.whl')):
            source.update(bundled.read_bytes())
    release_manifest = root / 'vendor/install-manifest.json'
    if release_manifest.is_file():
        release = json.loads(release_manifest.read_text())
        if release.get('schema') != 1:
            raise ValueError('Unsupported release artifact manifest')
        declared = release['artifacts']['tofu_search']
        filename = declared['filename']
        if Path(filename).name != filename:
            raise ValueError('Unsafe release artifact filename')
        artifact = root / 'vendor' / filename
        if (wheel_version(artifact) != declared['version']
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != declared['sha256']):
            raise ValueError('Release search artifact hash/version mismatch; refusing installation')
        if _version(declared['version']) < _version(floor):
            raise ValueError('Release manifest search version is below application floor')
        source.update(release_manifest.read_bytes())
    for bundled in sorted((root / 'vendor').glob('tofu_search-*.whl')):
        source.update(bundled.read_bytes())
    cache = root / '.tofu-install-cache/search' / source.hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    receipt = cache / 'manifest.json'
    if receipt.is_file():
        record = json.loads(receipt.read_text())
        filename = record['filename']
        if Path(filename).name != filename:
            raise ValueError('Unsafe artifact cache filename')
        wheel = cache / filename
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Artifact hash mismatch; refusing installation. Remove the corrupt search cache and retry.')
        if _version(wheel_version(wheel)) < _version(floor):
            raise ValueError('Cached search wheel is below the required floor')
        return wheel
    with tempfile.TemporaryDirectory(prefix='.build-', dir=cache) as temporary:
        staging = Path(temporary)
        bundled = sorted((root / 'vendor').glob('tofu_search-*.whl'),
                         key=lambda path: _version(wheel_version(path)))
        suitable = ([artifact] if release_manifest.is_file() else
                    [wheel for wheel in bundled if _version(wheel_version(wheel)) >= _version(floor)])
        if suitable:
            candidate = suitable[-1]
        elif (member / 'pyproject.toml').is_file():
            candidate = build_search_wheel(member, staging, python)
        else:
            raise ValueError(f'RELEASE CONSISTENCY ERROR: requires tofu-search>={floor}; '
                             'no compatible bundled wheel or retained source. '
                             'Changing mirrors or retrying cannot repair a missing release artifact.')
        version = wheel_version(candidate)
        if _version(version) < _version(floor):
            raise ValueError(f'Search artifact {version} is below required {floor}')
        content = candidate.read_bytes()
        wheel = cache / candidate.name
        # No pointer is published until the complete wheel is durable.
        temporary_wheel = staging / ('verified-' + candidate.name)
        temporary_wheel.write_bytes(content)
        temporary_wheel.replace(wheel)
        record = {'schema': 1, 'version': version, 'filename': wheel.name,
                  'sha256': hashlib.sha256(content).hexdigest(), 'input_sha256': source.hexdigest()}
        pending = staging / 'manifest.json'
        pending.write_text(json.dumps(record, indent=2) + '\n')
        pending.replace(receipt)
    return wheel


def build_search_wheel(member: Path, out_dir: Path, python: str) -> Path:
    """Build exactly one tofu-search wheel from ``member`` into ``out_dir``.

    Shared by installer admission and export bundling so both produce the
    artifact with the same builder (uv when present, else pip in ``python``).
    """
    uv = shutil.which('uv')
    command = ([uv, 'build', str(member), '--wheel', '--out-dir', str(out_dir)] if uv else
               [python, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(out_dir), str(member)])
    # Entry timestamps otherwise come from file mtimes, so every build of one
    # source has new bytes and the bundled wheel can never equal the published
    # one. A fixed SOURCE_DATE_EPOCH makes equal sources build equal wheels.
    env = {**os.environ, 'SOURCE_DATE_EPOCH': str(ZIP_DOS_EPOCH)}
    subprocess.run(command, check=True, timeout=300, stdout=sys.stderr, env=env)
    candidates = list(out_dir.glob('tofu_search-*.whl'))
    if len(candidates) != 1:
        raise ValueError('Search build did not produce exactly one wheel')
    return candidates[0]


def build_search_sdist(member: Path, out_dir: Path, python: str) -> Path:
    """Build exactly one byte-reproducible tofu-search sdist into ``out_dir``.

    setuptools writes each member's file mtime and the build time into the
    tarball and gzip header regardless of SOURCE_DATE_EPOCH, so the archive is
    rewritten with sorted members, ZIP_DOS_EPOCH mtimes, neutral ownership and
    a zero gzip timestamp. Equal sources then give equal published bytes.
    """
    with tempfile.TemporaryDirectory() as raw:
        uv = shutil.which('uv')
        command = ([uv, 'build', str(member), '--sdist', '--out-dir', raw] if uv else
                   [python, '-m', 'build', '--sdist', '--outdir', raw, str(member)])
        env = {**os.environ, 'SOURCE_DATE_EPOCH': str(ZIP_DOS_EPOCH)}
        subprocess.run(command, check=True, timeout=300, stdout=sys.stderr, env=env)
        candidates = list(Path(raw).glob('tofu_search-*.tar.gz'))
        if len(candidates) != 1:
            raise ValueError('Search build did not produce exactly one sdist')
        target = out_dir / candidates[0].name
        _write_normalized_sdist(candidates[0], target)
    return target


def _write_normalized_sdist(source: Path, target: Path) -> None:
    with tarfile.open(source, 'r:gz') as archive:
        members = sorted(archive.getmembers(), key=lambda item: item.name)
        entries = []
        for item in members:
            if not (item.isfile() or item.isdir()):
                raise ValueError(f'Unexpected sdist member type: {item.name}')
            data = archive.extractfile(item).read() if item.isfile() else None
            entries.append((item, data))
    with open(target, 'wb') as handle, \
            gzip.GzipFile(filename='', mode='wb', fileobj=handle, mtime=0) as stream, \
            tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as output:
        for item, data in entries:
            info = tarfile.TarInfo(item.name)
            info.type = item.type
            info.mode = 0o755 if item.isdir() or item.mode & 0o111 else 0o644
            info.mtime = ZIP_DOS_EPOCH
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            if data is None:
                output.addfile(info)
            else:
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    print(prepare_search_wheel(args.root, args.python))


def _compiled_versions(lock: Path) -> dict[str, str]:
    """Require every resolved package, including local first-party wheels."""
    expected = {}
    for raw in lock.read_text(encoding='utf-8').splitlines():
        line = raw.strip().rstrip('\\').strip()
        if not line or line.startswith(('#', '--')):
            continue
        match = re.match(r'([A-Za-z0-9._-]+)==([^\s;]+)', line)
        if match:
            name, version = match.groups()
        else:
            url = line.split(' @ ', 1)[-1]
            path = Path(unquote(urlsplit(url).path)) if url.startswith('file:') else Path(url)
            if not path.is_file() or path.suffix != '.whl':
                raise ValueError('Resolved Python artifact is not exact-pinned or a local wheel')
            with zipfile.ZipFile(path) as archive:
                names = [entry for entry in archive.namelist() if entry.endswith('.dist-info/METADATA')]
                if len(names) != 1:
                    raise ValueError('Invalid resolved wheel metadata')
                metadata = BytesParser().parsebytes(archive.read(names[0]))
                name, version = metadata['Name'], metadata['Version']
        package = name.lower().replace('_', '-')
        if package in expected and expected[package] != version:
            raise ValueError('Conflicting resolved Python artifacts')
        expected[package] = version
    return expected


def _acquire_python_wheels(python: str, requirements: Path, wheelhouse: Path) -> Path:
    """Resolve against staging, build/download everything, then lock wheel hashes.

    No package installs happen here. The real installation consumes only the
    returned exact, hashed lock and this local wheelhouse with no index access.
    """
    wheelhouse.mkdir(parents=True, exist_ok=True)
    report = wheelhouse / 'resolution.json'
    environment = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1')
    environment.pop('PIP_USER', None)
    environment.pop('PYTHONUSERBASE', None)
    acquisition = wheelhouse / 'acquisition.lock'
    uv = shutil.which('uv')
    expected = None
    constraints = requirements.parent / 'install-constraints.txt'
    constraint_args = ['--constraint', str(constraints)] if constraints.is_file() else []
    if uv:
        # Resolve once using uv; no pip dry-run download before wheel acquisition.
        subprocess.run([uv, 'pip', 'compile', '--no-config', '--python', python,
                        '--generate-hashes', '--output-file', str(acquisition),
                        *constraint_args, str(requirements)], check=True, timeout=900,
                       env=environment, stdout=sys.stderr)
        expected = _compiled_versions(acquisition)
    else:
        subprocess.run([python, '-m', 'pip', 'install', '--dry-run', '--ignore-installed', '--upgrade', '--no-user',
                        '--report', str(report), *constraint_args, '-r', str(requirements)],
                       check=True, timeout=900, env=environment, stdout=sys.stderr)
        records = json.loads(report.read_text())['install']
        if not records:
            lock = wheelhouse / 'install.lock'
            lock.write_text('')
            return lock
        acquisition = wheelhouse / 'acquisition.lock'
        lines = []
        for record in records:
            info = record['download_info']
            digest = info['archive_info']['hashes']['sha256']
            # Local first-party wheels need no index entry. Preserve their URI.
            spec = (info['url'] if info['url'].startswith('file:') else
                    record['metadata']['name'] + '==' + record['metadata']['version'])
            lines.append(spec + ' --hash=sha256:' + digest)
        acquisition.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        expected = {record['metadata']['name'].lower().replace('_', '-'):
                    record['metadata']['version'] for record in records}
    subprocess.run([python, '-m', 'pip', 'wheel', '--no-deps', '--require-hashes',
                    '--wheel-dir', str(wheelhouse), '-r', str(acquisition)],
                   check=True, timeout=900, env=environment, stdout=sys.stderr)
    admitted = {}
    for wheel in wheelhouse.glob('*.whl'):
        with zipfile.ZipFile(wheel) as archive:
            name = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
            metadata = BytesParser().parsebytes(archive.read(name))
        package = metadata['Name'].lower().replace('_', '-')
        if package in admitted:
            raise ValueError('Duplicate package in Python wheelhouse')
        if expected is None or expected.get(package) == metadata['Version']:
            admitted[package] = (f"{package}=={metadata['Version']} --hash=sha256:"
                                 + hashlib.sha256(wheel.read_bytes()).hexdigest())
    if expected is not None and set(admitted) != set(expected):
        raise ValueError('Required Python artifacts are incomplete; no environment commit permitted')
    lock = wheelhouse / 'install.lock'
    lock.write_text('\n'.join(admitted[key] for key in sorted(admitted)) + '\n', encoding='utf-8')
    return lock


def prepare_python_wheels(python: str, requirements: Path, wheelhouse: Path) -> Path:
    """Reuse verified artifacts or stage a complete wheelhouse before publication."""
    from . import python_wheels
    if wheelhouse.is_symlink():
        raise ValueError('Refusing a symlinked Python artifact cache')
    wheelhouse.parent.mkdir(parents=True, exist_ok=True)
    runtime = python_wheels.identity(python)
    lock = python_wheels.reuse(wheelhouse, requirements, runtime)
    if lock is not None:
        print('Reusing verified Python wheels; no dependency resolution or download.', file=sys.stderr)
        return lock
    lock = python_wheels.release_bundle(wheelhouse, requirements, runtime)
    if lock is not None:
        print('Using verified release Python wheels.', file=sys.stderr)
        return lock
    with tempfile.TemporaryDirectory(prefix='.prepare-wheels-', dir=wheelhouse.parent) as temporary:
        stage = Path(temporary) / 'wheelhouse'
        _acquire_python_wheels(python, requirements, stage)
        return python_wheels.publish(stage, wheelhouse, requirements, runtime)


if __name__ == '__main__':
    main()
